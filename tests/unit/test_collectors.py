from collections import namedtuple
from types import SimpleNamespace as NS

import psutil
import pytest

from titan.core import collectors
from titan.core.collectors import Collector, counter_rate
from titan.core.models import Section


class Provider:
    def __init__(self):
        self.calls = {"system": 0, "storage": 0, "gpu": 0}

    def collect_system(self):
        self.calls["system"] += 1
        return Section(data={"cpu_model": "Test CPU", "cpu_manufacturer": "Test vendor"})

    def collect_storage(self):
        self.calls["storage"] += 1
        return Section(data={"drives": []})

    def collect_gpu(self):
        self.calls["gpu"] += 1
        return Section(data={"devices": []})

    def collect_battery_metadata(self):
        return {"batteries": []}


@pytest.fixture
def setup_collector(monkeypatch):
    clock = [100.0]
    backend = NS(
        AccessDenied=psutil.AccessDenied,
        NoSuchProcess=psutil.NoSuchProcess,
        ZombieProcess=psutil.ZombieProcess,
        cpu_percent=lambda interval=None, percpu=False: [20, 40] if percpu else 30,
        cpu_count=lambda logical=True: 2 if logical else 1,
        cpu_freq=lambda: NS(current=1500, min=0, max=3000),
        getloadavg=lambda: (1, 2, 3),
        virtual_memory=lambda: NS(total=1000, available=700, used=300, percent=30),
        swap_memory=lambda: NS(total=500, used=0, percent=0),
        process_iter=lambda **kwargs: [],
        disk_partitions=lambda all=False: [
            NS(device="disk", mountpoint="mount", fstype="test", opts="rw")
        ],
        disk_usage=lambda mount: NS(total=1000, used=400, free=600, percent=40),
        disk_io_counters=lambda **kwargs: NS(read_bytes=100, write_bytes=200),
        net_io_counters=lambda **kwargs: {"eth": NS(bytes_sent=100, bytes_recv=200)},
        net_if_addrs=lambda: {},
        net_if_stats=lambda: {},
        net_connections=lambda **kwargs: [],
        sensors_battery=lambda: None,
    )
    monkeypatch.setattr(
        collectors, "collect_network_metadata", lambda: {"dns": [], "gateway": None}
    )
    provider = Provider()
    collector = Collector(
        backend, provider=provider, monotonic=lambda: clock[0], wall_time=lambda: 123
    )
    return collector, backend, provider, clock


def test_first_cpu_sample_is_unavailable_then_measured(setup_collector):
    collector, _, _, clock = setup_collector
    first = collector.collect()
    assert first.sections["cpu"].data["percent"] is None
    assert first.sections["cpu"].data["per_core"] == [None, None]
    assert first.sections["cpu"].data["min_mhz"] is None
    clock[0] += 2
    assert collector.collect().sections["cpu"].data["percent"] == 30


def test_a_failed_section_does_not_hide_other_sections(setup_collector):
    collector, backend, _, _ = setup_collector

    def denied():
        raise psutil.AccessDenied()

    backend.virtual_memory = denied
    snapshot = collector.collect()
    assert "Permission denied" in snapshot.sections["memory"].error
    assert snapshot.sections["cpu"].data["model"] == "Test CPU"
    assert snapshot.sections["storage"].data["volumes"][0]["free"] == 600
    assert set(snapshot.sections) == {
        "cpu",
        "memory",
        "processes",
        "storage",
        "network",
        "battery",
        "gpu",
        "system",
        "thermal",
        "process_traffic",
    }


def test_slow_inventory_cache_expires_from_completion(setup_collector):
    collector, _, _, clock = setup_collector
    calls = []

    def slow():
        calls.append(1)
        clock[0] += 20
        return {"observed": True}

    collector._cached("slow", 10, slow)
    clock[0] += 2
    collector._cached("slow", 10, slow)
    assert len(calls) == 1


def test_unavailable_swap_does_not_hide_ram(setup_collector):
    collector, backend, _, _ = setup_collector

    def fail():
        raise RuntimeError("Performance counters inaccessible")

    backend.swap_memory = fail
    result = collector.collect().sections["memory"]
    assert result.error is None
    assert result.data["percent"] == 30
    assert result.data["swap_total"] is None
    assert result.unavailable


def test_unavailable_connections_do_not_hide_interfaces(setup_collector):
    collector, backend, _, _ = setup_collector

    def fail(**kwargs):
        raise RuntimeError("Connection table inaccessible")

    backend.net_connections = fail
    result = collector.collect().sections["network"]
    assert result.error is None
    assert result.data["interfaces"][0]["name"] == "eth"
    assert result.data["connections"] == []
    assert result.unavailable


def test_storage_inventory_failure_keeps_mounted_volumes(setup_collector):
    collector, _, provider, _ = setup_collector

    def fail():
        raise RuntimeError("Disconnected")

    provider.collect_storage = fail
    result = collector.collect().sections["storage"]
    assert result.error
    assert result.data["volumes"][0]["total"] == 1000


def test_rates_use_elapsed_time_and_reset_is_unavailable(setup_collector):
    collector, backend, _, clock = setup_collector
    first = collector.collect()
    assert first.sections["network"].data["upload_bps"] is None
    clock[0] += 2
    backend.net_io_counters = lambda **kwargs: {"eth": NS(bytes_sent=300, bytes_recv=600)}
    backend.disk_io_counters = lambda **kwargs: NS(read_bytes=700, write_bytes=1000)
    second = collector.collect()
    assert second.sections["network"].data["upload_bps"] == 100
    assert second.sections["network"].data["download_bps"] == 200
    assert second.sections["storage"].data["read_bps"] == 300
    clock[0] += 2
    backend.net_io_counters = lambda **kwargs: {"eth": NS(bytes_sent=3, bytes_recv=6)}
    assert collector.collect().sections["network"].data["upload_bps"] is None


def test_new_adapter_does_not_create_a_phantom_transfer_burst(setup_collector):
    collector, backend, _, clock = setup_collector
    collector.collect()
    clock[0] += 1
    backend.net_io_counters = lambda **kwargs: {
        "eth": NS(bytes_sent=110, bytes_recv=210),
        "new": NS(bytes_sent=1000000, bytes_recv=2000000),
    }
    result = collector.collect().sections["network"].data
    assert result["download_bps"] is None
    assert result["interfaces"][0]["download_bps"] == 10


def test_cpu_time_baseline_survives_worker_thread_changes(setup_collector):
    collector, backend, _, _ = setup_collector
    times = namedtuple("Times", "user system idle iowait guest guest_nice")
    sample = [times(100, 20, 100, 10, 10, 0)]
    backend.cpu_times = lambda percpu=False: [sample[0], sample[0]] if percpu else sample[0]
    collector.collect()
    # A fresh psutil thread returns zero, but our persistent CPU-time interval
    # has ten active and ten idle seconds (guest is already in user time).
    backend.cpu_percent = lambda interval=None, percpu=False: [0, 0] if percpu else 0
    sample[0] = times(110, 20, 110, 10, 15, 0)
    result = collector.collect().sections["cpu"].data
    assert result["percent"] == 50
    assert result["per_core"] == [50, 50]


def test_slow_inventory_has_a_bounded_refresh_cadence(setup_collector):
    collector, _, provider, clock = setup_collector
    collector.collect()
    clock[0] += 1
    collector.collect()
    assert provider.calls == {"system": 1, "storage": 1, "gpu": 1}
    clock[0] += 5
    collector.collect()
    assert provider.calls == {"system": 1, "storage": 1, "gpu": 2}
    clock[0] += 30
    collector.collect()
    assert provider.calls == {"system": 2, "storage": 2, "gpu": 3}


@pytest.mark.parametrize(
    "current,previous,elapsed,expected",
    [(30, 10, 2, 10), (5, 10, 2, None), (10, 10, 0, None), (10, 10, -1, None)],
)
def test_counter_reset_and_clock_reversal(current, previous, elapsed, expected):
    assert counter_rate(current, previous, elapsed) == expected
