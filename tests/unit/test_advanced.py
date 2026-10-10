import ctypes
import json
from types import SimpleNamespace as NS

import pytest

from titan.art import WORDMARK
from titan.core.diagnostics import evaluate
from titan.core.health_trends import HealthTrendError, HealthTrends
from titan.core.models import Section, Snapshot
from titan.core.monitoring import DiskSampler, DriveTimeline
from titan.core.snapshots import compare_snapshots, load_snapshot, save_snapshot, snapshot_payload
from titan.hardware.map import hardware_map
from titan.hardware.stability import StabilityTest
from titan.hardware.thermal import ThermalSampler
from titan.hardware.traffic import DataStats, ProcessTraffic, TcpOwner, parse_tcptop
from titan.platforms.windows_disk import Performance, disk_performance


def disk(identity="one", device="/dev/sda", **health):
    return {
        "identity": identity,
        "identity_quality": "hashed OS hardware identity",
        "device": device,
        "model": "Test",
        "health": {"available": True, **health},
    }


def test_timeline_ignores_failed_partial_and_cached_inventory():
    timeline = DriveTimeline()
    first = Section({"drives": [disk()], "inventory_complete": True, "inventory_observed_at": 1})
    timeline.observe(first, 100)
    timeline.observe(first, 101)
    timeline.observe(Section({"drives": [], "inventory_complete": False}), 102)
    timeline.observe(Section({"drives": [], "inventory_complete": True}, error="denied"), 103)
    assert [e["event"] for e in timeline.events] == ["present at start"]
    timeline.observe(Section({"drives": [], "inventory_complete": True}), 104)
    timeline.observe(
        Section({"drives": [disk(device="/dev/sdb")], "inventory_complete": True}), 105
    )
    assert [e["event"] for e in timeline.events] == [
        "present at start",
        "disconnected",
        "reconnected",
    ]
    assert timeline.events[-1]["device"] == "/dev/sdb"


def counter(n):
    return NS(
        read_bytes=n * 100,
        write_bytes=n * 200,
        read_count=n,
        write_count=n * 2,
        read_time=n * 5,
        write_time=n * 16,
        busy_time=n * 10,
    )


def test_disk_latency_idle_reset_clock_and_queue(tmp_path):
    path = tmp_path / "class/block/sda"
    path.mkdir(parents=True)
    (path / "inflight").write_text("2 3")
    sampler = DiskSampler()
    assert sampler.sample({"sda": counter(10)}, 10)[0]["read_latency_ms"] is None
    row = sampler.sample({"sda": counter(14)}, 12, sysfs=tmp_path)[0]
    assert (
        row["read_bps"],
        row["read_iops"],
        row["read_latency_ms"],
        row["write_latency_ms"],
        row["queue_depth"],
    ) == (200, 2, 5, 8, 5)
    assert sampler.sample({"sda": counter(14)}, 13)[0]["read_latency_ms"] is None
    assert sampler.sample({"sda": counter(2)}, 14)[0]["read_bps"] is None
    assert sampler.sample({"sda": counter(10)}, 12)[0]["read_bps"] is None
    assert sampler.sample({}, 15) == []
    assert not sampler.previous
    sampler.sample({"sda": counter(10)}, 20, identities={"sda": "first"})
    assert (
        sampler.sample({"sda": counter(200)}, 21, identities={"sda": "replacement"})[0]["read_bps"]
        is None
    )


def test_smart_trends_persist_only_on_request_and_alert_on_deterioration(tmp_path):
    path = tmp_path / "health.db"
    trends = HealthTrends(path)
    trends.record(
        Section({"drives": [disk(media_errors=0, available_spare=100, percentage_used=4)]}), 100
    )
    assert not path.exists()
    alerts = trends.record(
        Section({"drives": [disk(media_errors=2, available_spare=99, percentage_used=5)]}),
        110,
        persist=True,
    )
    assert {a["metric"] for a in alerts} == {"media_errors", "available_spare", "percentage_used"}
    assert len(HealthTrends(path).points) == 1
    assert trends.record(Section({"drives": [disk("replacement", media_errors=40)]}), 120) == []
    assert (
        trends.record(Section({"drives": [disk(media_errors=0, available_spare=100)]}), 130) == []
    )
    trends.record(Section({"drives": [{"health": {"available": False}}]}), 140)
    assert len(trends.points) == 4
    assert b"/dev/sda" not in path.read_bytes()


def test_corrupt_smart_history_is_preserved(tmp_path):
    path = tmp_path / "health.db"
    original = b"corrupt database"
    path.write_bytes(original)
    with pytest.raises(HealthTrendError):
        HealthTrends(path)
    assert path.read_bytes() == original


def test_snapshots_redact_missing_data_and_reject_invalid_files(tmp_path):
    before = Snapshot(
        100,
        "Test",
        {
            "cpu": Section({"percent": 10, "temperature_c": None}),
            "system": Section({"hostname": "private-host"}),
        },
    )
    after = Snapshot(120, "Test", {"cpu": Section({"percent": 30, "temperature_c": 70})})
    path = tmp_path / "before.json"
    save_snapshot(before, path)
    assert "private-host" not in path.read_text()
    result = compare_snapshots(load_snapshot(path), snapshot_payload(after))
    assert result["elapsed_seconds"] == 20
    rows = {r["metric"]: r for r in result["measurements"]}
    assert rows["cpu.percent"]["change"] == 20
    assert rows["cpu.temperature_c"]["change"] is None
    path.write_text('{"kind":"other"}')
    with pytest.raises(ValueError):
        load_snapshot(path)
    path.write_text(
        json.dumps(
            {
                "kind": "titan-comparison-snapshot",
                "schema_version": 1,
                "snapshot": {"timestamp": 1, "sections": {"cpu": []}},
            }
        )
    )
    with pytest.raises(ValueError):
        load_snapshot(path)


def test_linux_thermal_counter_evidence_and_critical_threshold(tmp_path):
    path = tmp_path / "devices/system/cpu/cpu0/thermal_throttle"
    path.mkdir(parents=True)
    (path / "core_throttle_count").write_text("10")
    backend = NS(
        sensors_temperatures=lambda: {
            "coretemp": [NS(label="Core 0", current=95, high=90, critical=95)]
        }
    )
    sampler = ThermalSampler()
    first = sampler.collect(backend, NS(sysfs=tmp_path))
    assert first.data["throttling"]["increments"] == {}
    (path / "core_throttle_count").write_text("12")
    second = sampler.collect(backend, NS(sysfs=tmp_path))
    findings = evaluate([Snapshot(100, "Linux", {"thermal": second})])
    assert "thermal-events" in {f.id for f in findings}
    assert any(f.severity == "critical" for f in findings)
    (path / "core_throttle_count").write_text("1")
    assert sampler.collect(backend, NS(sysfs=tmp_path)).data["throttling"]["increments"] == {}


def test_ascii_inventory_is_safe_and_bounded():
    snap = Snapshot(
        1,
        "Linux",
        {
            "cpu": Section({"model": "\x1b[31mCPU\nINJECT"}),
            "storage": Section(
                {
                    "drives": [
                        {
                            "device": "/dev/sda",
                            "model": "x" * 300,
                            "partitions": [
                                {
                                    "device": "/dev/sda1",
                                    "filesystem": "ext4",
                                    "mountpoint": "/home/private",
                                }
                            ],
                        }
                    ]
                }
            ),
        },
    )
    drawing = hardware_map(snap, 40)
    assert "\x1b" not in drawing
    assert max(map(len, drawing.splitlines())) <= 40
    assert "ext4" in drawing
    assert WORDMARK.isascii() and len(WORDMARK.splitlines()) == 5


@pytest.mark.parametrize(
    "values",
    [
        {"port": 0},
        {"duration": float("nan")},
        {"duration": 3601},
        {"interval": 0.1},
        {"target": "https://example.com"},
    ],
)
def test_stability_validates_bounds(values):
    with pytest.raises(ValueError):
        StabilityTest(**({"target": "localhost"} | values))


def test_stability_summary_uses_tcp_failures_and_stops_without_probe(monkeypatch):
    test = StabilityTest("localhost", duration=1)
    test.samples.extend(
        [
            {"success": True, "stage": "tcp", "latency_ms": 10},
            {"success": False, "stage": "dns", "latency_ms": None},
            {"success": True, "stage": "tcp", "latency_ms": 20},
        ]
    )
    result = test.summary()
    assert result["median_ms"] == 15 and result["p95_ms"] == 20 and result["jitter_ms"] == 10
    assert result["dns_failures"] == 1 and result["failure_percent"] == pytest.approx(100 / 3)
    monkeypatch.setattr(
        "titan.hardware.stability.probe", lambda *_: pytest.fail("cancelled test must not probe")
    )
    test.stop()
    assert test.run()["state"] == "stopped"


def test_tcptop_tcp_attribution_parser_and_default_off():
    values = parse_tcptop(
        "PID COMM LADDR RADDR RX_KB TX_KB\n123 Browser 127.0.0.1:9 127.0.0.2:8 2 3\n123 Browser ::1:9 ::2:8 1 1\nbad data"
    )
    assert values == {123: (3072, 4096)}
    assert ProcessTraffic().collect([]).data["active"] is False
    assert DataStats.out_bytes.offset == 0 and DataStats.in_bytes.offset == 16


def test_native_windows_disk_units_read_only_handles_and_retry(monkeypatch):
    calls, closed = [], []
    value = Performance()
    value.read_time, value.write_time, value.read_count, value.queue_depth = 25000, 50000, 2, 3

    def ioctl(handle, code, _input, _input_size, buffer, capacity, returned, _overlap):
        calls.append(capacity)
        assert code == 0x70020
        if len(calls) == 1:
            return False
        ctypes.memmove(buffer, ctypes.byref(value), ctypes.sizeof(value))
        ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32)).contents.value = ctypes.sizeof(value)
        return True

    def create(path, access, share, _security, disposition, _flags, _template):
        assert path == r"\\.\PhysicalDrive0" and access == 0 and share == 3 and disposition == 3
        return 42

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122, raising=False)
    result = disk_performance(
        ["PhysicalDrive0", "../../unsafe"],
        NS(CreateFileW=create, DeviceIoControl=ioctl, CloseHandle=closed.append),
    )
    assert result["PhysicalDrive0"].read_time == 2.5
    assert result["PhysicalDrive0"].write_time == 5
    assert result["PhysicalDrive0"].queue_depth == 3
    assert calls == [1024, 2048] and closed == [42]


def test_native_windows_disk_buffer_retry_is_bounded(monkeypatch):
    calls, closed = [], []
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122, raising=False)

    def ioctl(*args):
        calls.append(args[5])
        return False

    api = NS(CreateFileW=lambda *_: 42, DeviceIoControl=ioctl, CloseHandle=closed.append)
    assert disk_performance(["PhysicalDrive0"], api) == {}
    assert max(calls) == 65536 and len(calls) == 7 and closed == [42]


def test_windows_tcp_rates_and_pid_reuse(monkeypatch):
    clock, counters = [10.0], [1000, 2000]
    owner = TcpOwner(5, 1, 2, 3, 4, 123)
    table = (1).to_bytes(4, "little") + bytes(owner)

    def get_table(buffer, length, _order, family, kind, _reserved):
        assert family == 2 and kind == 5
        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(table)
        if buffer is None:
            return 122
        ctypes.memmove(buffer, table, len(table))
        return 0

    def get_stats(
        _row,
        kind,
        enabled,
        _rwversion,
        _rwsize,
        _ros,
        _rosversion,
        _rossize,
        stats,
        _rodversion,
        rodsize,
    ):
        assert kind == 1 and rodsize == ctypes.sizeof(DataStats)
        ctypes.cast(enabled, ctypes.POINTER(ctypes.c_ubyte)).contents.value = 1
        data = ctypes.cast(stats, ctypes.POINTER(DataStats)).contents
        data.in_bytes, data.out_bytes = counters
        return 0

    traffic = ProcessTraffic()
    traffic.api = NS(GetExtendedTcpTable=get_table, GetPerTcpConnectionEStats=get_stats)
    monkeypatch.setattr("titan.hardware.traffic.time.monotonic", lambda: clock[0])
    identity = [{"pid": 123, "create_time": 1}]
    assert traffic._windows(identity).data["items"] == []
    counters[:] = [5000, 8000]
    clock[0] += 2
    row = traffic._windows(identity).data["items"][0]
    assert (row["download_bps"], row["upload_bps"]) == (2000, 3000)
    clock[0] += 1
    assert traffic._windows([{"pid": 123, "create_time": 2}]).data["items"] == []
