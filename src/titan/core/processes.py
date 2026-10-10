"""Read-only process inspection and deliberately conservative termination."""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil


class ProcessSafetyError(RuntimeError):
    """An operation was refused or could not be completed safely."""


CRITICAL_NAMES = frozenset(
    {
        "system",
        "registry",
        "memory compression",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "lsass.exe",
        "lsaiso.exe",
        "services.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "secure system",
        "init",
        "systemd",
        "kthreadd",
        "dbus-daemon",
        "udevd",
        "systemd-udevd",
    }
)


def _same_identity(process: Any, create_time: float) -> bool:
    return abs(float(process.create_time()) - create_time) <= 0.000001


def _target(pid: int, create_time: float, backend: Any) -> Any:
    if isinstance(pid, bool) or pid <= 0 or not math.isfinite(create_time) or create_time <= 0:
        raise ProcessSafetyError("Select a live process with its recorded start time.")
    try:
        process = backend.Process(pid)
        if not _same_identity(process, create_time):
            raise ProcessSafetyError("The PID now belongs to a different process. Refresh first.")
        return process
    except (backend.NoSuchProcess, backend.ZombieProcess) as exc:
        raise ProcessSafetyError("The selected process has already exited.") from exc
    except backend.AccessDenied as exc:
        raise ProcessSafetyError(
            "Permission denied: the process identity cannot be verified."
        ) from exc


def _protected_pids(backend: Any) -> set[int]:
    protected = {0, 1, 2, 4, os.getpid(), os.getppid()}
    try:
        protected.update(parent.pid for parent in backend.Process(os.getpid()).parents())
    except (backend.NoSuchProcess, backend.AccessDenied):
        # The immediate parent is always protected even on restricted platforms.
        pass
    return protected


def _services(pid: int, backend: Any) -> list[str]:
    if sys.platform == "win32":
        if not hasattr(backend, "win_service_iter"):
            raise ProcessSafetyError("Service ownership cannot be checked on this installation.")
        names = []
        try:
            for service in backend.win_service_iter():
                info = service.as_dict()
                if info.get("pid") == pid:
                    names.append(str(info.get("name", "Windows service")))
        except (backend.AccessDenied, OSError, RuntimeError) as exc:
            raise ProcessSafetyError(
                "Permission denied while checking Windows service ownership."
            ) from exc
        return names
    if sys.platform.startswith("linux"):
        try:
            cgroups = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ProcessSafetyError(
                "The selected process exited while its ownership was checked."
            ) from exc
        except OSError as exc:
            raise ProcessSafetyError(
                "Service ownership cannot be checked for this process."
            ) from exc
        return sorted(
            {
                part
                for line in cgroups.splitlines()
                for part in line.split("/")
                if part.endswith(".service")
            }
        )
    return []


def inspect_process(pid: int, create_time: float, *, backend: Any = psutil) -> dict[str, Any]:
    """Inspect a PID only when the caller's PID/start-time identity still matches."""
    process = _target(pid, create_time, backend)
    try:
        with process.oneshot():
            data = process.as_dict(
                attrs=[
                    "pid",
                    "name",
                    "exe",
                    "create_time",
                    "ppid",
                    "username",
                    "status",
                    "cmdline",
                ],
                ad_value=None,
            )
            data["memory_bytes"] = process.memory_info().rss
        try:
            data["children"] = [
                {"pid": child.pid, "name": child.name()} for child in process.children()
            ]
        except (backend.AccessDenied, backend.NoSuchProcess):
            data["children"] = None
        reason = None
        if not data.get("name"):
            reason = "The process name is inaccessible, so Titan cannot show a safe confirmation."
        elif pid in _protected_pids(backend) or str(data.get("name", "")).lower() in CRITICAL_NAMES:
            reason = "Titan, its ancestors, and system-critical processes are protected."
        else:
            try:
                services = _services(pid, backend)
                if services:
                    reason = "Service process: manage it through the operating system's service controls."
                data["services"] = services
            except ProcessSafetyError as exc:
                reason = str(exc)
        data["protected"] = reason is not None
        data["protection_reason"] = reason
        if not _same_identity(process, create_time):
            raise ProcessSafetyError("The process changed during inspection. Refresh first.")
        return data
    except (backend.NoSuchProcess, backend.ZombieProcess) as exc:
        raise ProcessSafetyError("The selected process exited during inspection.") from exc
    except backend.AccessDenied as exc:
        raise ProcessSafetyError("Permission denied while inspecting this process.") from exc


def terminate_process(
    pid: int, create_time: float, confirmed: bool = False, *, backend: Any = psutil
) -> dict[str, Any]:
    """Request OS termination. Never escalate to kill or silently affect services."""
    if not confirmed:
        raise ProcessSafetyError("Confirm the displayed process name and PID before terminating.")
    details = inspect_process(pid, create_time, backend=backend)
    if details["protected"]:
        raise ProcessSafetyError(str(details["protection_reason"]))
    process = _target(pid, create_time, backend)
    try:
        # Re-check on the same Process object immediately before the OS operation.
        if not _same_identity(process, create_time):
            raise ProcessSafetyError("The process identity changed. No termination was requested.")
        process.terminate()
    except backend.AccessDenied as exc:
        raise ProcessSafetyError(
            "Permission denied. Titan will not request elevation automatically."
        ) from exc
    except (backend.NoSuchProcess, backend.ZombieProcess) as exc:
        raise ProcessSafetyError("The selected process has already exited.") from exc
    return {
        "pid": pid,
        "name": details.get("name"),
        "create_time": create_time,
        "termination_requested": True,
    }


@dataclass
class _Sample:
    at: float
    cpu_seconds: float | None
    metadata_at: float
    exe: str | None
    username: str | None
    io: tuple[int, int] | None = None


class ProcessSampler:
    """Bounded PID/start-time state; process CPU may exceed 100% on multiple cores."""

    def __init__(self, backend: Any = psutil, max_processes: int = 2048) -> None:
        self.backend = backend
        self.max_processes = max(1, min(max_processes, 16384))
        self._previous: dict[tuple[int, float], _Sample] = {}

    def collect(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        current: dict[tuple[int, float], _Sample] = {}
        items: list[dict[str, Any]] = []
        unavailable = 0
        truncated = False
        attrs = ["pid", "name", "create_time", "ppid", "memory_info", "memory_percent", "cpu_times"]
        for process in self.backend.process_iter(attrs=attrs, ad_value=None):
            if len(items) >= self.max_processes:
                truncated = True
                break
            try:
                info = process.info
                start = info.get("create_time")
                if start is None:
                    unavailable += 1
                    continue
                key = (process.pid, float(start))
                previous = self._previous.get(key)
                cpu_times = info.get("cpu_times")
                cpu_seconds = (
                    None if cpu_times is None else float(cpu_times.user + cpu_times.system)
                )
                percent = None
                if (
                    previous
                    and previous.cpu_seconds is not None
                    and cpu_seconds is not None
                    and now > previous.at
                ):
                    difference = cpu_seconds - previous.cpu_seconds
                    if difference >= 0:
                        percent = round(100 * difference / (now - previous.at), 2)
                if previous and now - previous.metadata_at < 30:
                    exe, username, metadata_at = (
                        previous.exe,
                        previous.username,
                        previous.metadata_at,
                    )
                else:
                    metadata = process.as_dict(attrs=["exe", "username"], ad_value=None)
                    exe, username, metadata_at = metadata.get("exe"), metadata.get("username"), now
                memory = info.get("memory_info")
                try:
                    io = getattr(process, "io_counters", lambda: None)()
                    io_values = (io.read_bytes, io.write_bytes) if io else None
                except (
                    OSError,
                    NotImplementedError,
                    self.backend.AccessDenied,
                    self.backend.NoSuchProcess,
                    self.backend.ZombieProcess,
                ):
                    io_values = None
                io_rates: list[float | None] = [None, None]
                if previous and previous.io and io_values and now > previous.at:
                    io_rates = [
                        (value - old) / (now - previous.at) if value >= old else None
                        for value, old in zip(io_values, previous.io, strict=True)
                    ]
                items.append(
                    {
                        "pid": process.pid,
                        "name": info.get("name"),
                        "cpu_percent": percent,
                        "memory_bytes": None if memory is None else memory.rss,
                        "memory_percent": info.get("memory_percent"),
                        "exe": exe,
                        "create_time": start,
                        "ppid": info.get("ppid"),
                        "username": username,
                        "io_read_bps": io_rates[0],
                        "io_write_bps": io_rates[1],
                        "io_read_bytes": io_values[0] if io_values else None,
                        "io_write_bytes": io_values[1] if io_values else None,
                    }
                )
                current[key] = _Sample(now, cpu_seconds, metadata_at, exe, username, io_values)
            except (
                self.backend.NoSuchProcess,
                self.backend.ZombieProcess,
                self.backend.AccessDenied,
            ):
                unavailable += 1
        self._previous = current
        return {
            "items": items,
            "inaccessible": unavailable,
            "truncated": truncated,
            "cpu_percent_basis": "100% equals one logical CPU; multithreaded processes may exceed 100%.",
            "io_basis": "Linux: storage-accounted process bytes. Windows: all OS process I/O, including file, network and device operations; not physical disk traffic. Missing/reset counters remain unavailable.",
        }
