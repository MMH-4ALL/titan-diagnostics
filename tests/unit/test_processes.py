from contextlib import nullcontext
from types import SimpleNamespace as NS

import psutil
import pytest

from titan.core import processes
from titan.core.processes import ProcessSafetyError, ProcessSampler, terminate_process


class Process:
    pid = 321

    def __init__(self, start=10.0, cpu=1.0):
        self.start = start
        self.cpu = cpu
        self.terminated = False
        self.refresh_info()

    def refresh_info(self):
        self.info = {
            "pid": self.pid,
            "name": "user-app",
            "create_time": self.start,
            "ppid": 100,
            "memory_info": NS(rss=200),
            "memory_percent": 20,
            "cpu_times": NS(user=self.cpu, system=0),
        }

    def create_time(self):
        return self.start

    def as_dict(self, attrs, ad_value=None):
        return {
            key: {
                "pid": self.pid,
                "name": "user-app",
                "exe": "app",
                "create_time": self.start,
                "username": "person",
                "ppid": 100,
                "status": "running",
                "cmdline": [],
            }.get(key)
            for key in attrs
        }

    def memory_info(self):
        return NS(rss=200)

    def oneshot(self):
        return nullcontext()

    def children(self):
        return []

    def terminate(self):
        self.terminated = True


def backend_for(process):
    return NS(
        AccessDenied=psutil.AccessDenied,
        NoSuchProcess=psutil.NoSuchProcess,
        ZombieProcess=psutil.ZombieProcess,
        process_iter=lambda **kwargs: [process],
        Process=lambda pid: process,
    )


def test_process_cpu_identity_and_reused_pid():
    process = Process()
    sampler = ProcessSampler(backend_for(process))
    assert sampler.collect(10)["items"][0]["cpu_percent"] is None
    process.cpu += 1
    process.refresh_info()
    assert sampler.collect(12)["items"][0]["cpu_percent"] == 50
    process.start = 11
    process.cpu = 0
    process.refresh_info()
    assert sampler.collect(14)["items"][0]["cpu_percent"] is None
    assert list(sampler._previous) == [(321, 11.0)]


def test_sampler_cache_is_bounded():
    process = Process()
    backend = backend_for(process)
    backend.process_iter = lambda **kwargs: [process] * 10
    sampler = ProcessSampler(backend, max_processes=2)
    result = sampler.collect(10)
    assert result["truncated"]
    assert len(result["items"]) == 2
    assert len(sampler._previous) <= 2


def test_process_io_rates_require_same_identity_and_no_reset():
    process = Process()
    values = [100, 200]
    process.io_counters = lambda: NS(read_bytes=values[0], write_bytes=values[1])
    sampler = ProcessSampler(backend_for(process))
    assert sampler.collect(10)["items"][0]["io_read_bps"] is None
    values[:] = [500, 800]
    assert sampler.collect(12)["items"][0]["io_write_bps"] == 300
    values[:] = [0, 0]
    assert sampler.collect(13)["items"][0]["io_read_bps"] is None
    process.start = 20
    process.refresh_info()
    values[:] = [900, 900]
    assert sampler.collect(14)["items"][0]["io_read_bps"] is None


def test_inaccessible_process_io_keeps_other_metrics():
    process = Process()

    def denied():
        raise psutil.AccessDenied()

    process.io_counters = denied
    result = ProcessSampler(backend_for(process)).collect(10)["items"][0]
    assert result["memory_bytes"] == 200 and result["io_read_bps"] is None


def test_confirmation_and_identity_are_required(monkeypatch):
    process = Process()
    backend = backend_for(process)
    with pytest.raises(ProcessSafetyError, match="Confirm"):
        terminate_process(321, 10, backend=backend)
    with pytest.raises(ProcessSafetyError, match="different process"):
        terminate_process(321, 9, confirmed=True, backend=backend)
    assert not process.terminated


@pytest.mark.parametrize(
    "protection,services,expected",
    [({321}, [], "protected"), (set(), ["test.service"], "Service process")],
)
def test_system_and_service_processes_are_protected(monkeypatch, protection, services, expected):
    process = Process()
    monkeypatch.setattr(processes, "_protected_pids", lambda backend: protection)
    monkeypatch.setattr(processes, "_services", lambda pid, backend: services)
    with pytest.raises(ProcessSafetyError, match=expected):
        terminate_process(321, 10, True, backend=backend_for(process))
    assert not process.terminated


def test_identity_rechecked_immediately_before_termination(monkeypatch):
    process = Process()
    monkeypatch.setattr(
        processes, "inspect_process", lambda *args, **kwargs: {"protected": False, "name": "app"}
    )
    identities = iter([10, 11])
    process.create_time = lambda: next(identities)
    with pytest.raises(ProcessSafetyError, match="identity changed"):
        terminate_process(321, 10, True, backend=backend_for(process))
    assert not process.terminated


def test_permission_denial_is_readable_and_does_not_kill(monkeypatch):
    process = Process()
    monkeypatch.setattr(
        processes, "inspect_process", lambda *args, **kwargs: {"protected": False, "name": "app"}
    )

    def denied():
        raise psutil.AccessDenied(321)

    process.terminate = denied
    with pytest.raises(ProcessSafetyError, match="Permission denied"):
        terminate_process(321, 10, True, backend=backend_for(process))
    assert not process.terminated
