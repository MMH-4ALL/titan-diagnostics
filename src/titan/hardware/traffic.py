"""Opt-in TCP attribution. Never infer process bytes from interface totals."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from titan.core.models import Section
from titan.utils.commands import run_command


class TcpRow(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in ("state", "local_addr", "local_port", "remote_addr", "remote_port")
    ]


class TcpOwner(ctypes.Structure):
    _fields_ = TcpRow._fields_ + [("pid", ctypes.c_uint32)]


class DataStats(ctypes.Structure):
    _fields_ = (
        [
            (name, ctypes.c_uint64)
            for name in (
                "out_bytes",
                "out_segments",
                "in_bytes",
                "in_segments",
                "segments_out",
                "segments_in",
            )
        ]
        + [
            (name, ctypes.c_uint32)
            for name in ("soft_errors", "soft_reason", "snd_una", "snd_nxt", "snd_max")
        ]
        + [("acked", ctypes.c_uint64), ("rcv_nxt", ctypes.c_uint32), ("received", ctypes.c_uint64)]
    )


def parse_tcptop(output: str) -> dict[int, tuple[float, float]]:
    result: dict[int, tuple[float, float]] = {}
    for line in output.splitlines()[:8192]:
        match = re.fullmatch(r"\s*(\d+)\s+(.+?)\s+(\S+:\d+)\s+(\S+:\d+)\s+(\d+)\s+(\d+)\s*", line)
        if match:
            pid, received, sent = int(match[1]), int(match[5]) * 1024, int(match[6]) * 1024
            a, b = result.get(pid, (0.0, 0.0))
            result[pid] = (a + received, b + sent)
    return result


class ProcessTraffic:
    def __init__(self) -> None:
        self.enabled = False
        self.previous: dict[tuple[Any, ...], tuple[float, int, int]] = {}
        self.last_linux = 0.0
        self.api: Any = None

    def collect(self, items: list[dict[str, Any]]) -> Section:
        if not self.enabled:
            self.previous = {}
            return Section(
                {"items": [], "active": False},
                unavailable=["Process TCP tracing is off; start it explicitly on Processes."],
            )
        if os.name == "nt":
            try:
                return self._windows(items)
            except (OSError, AttributeError, ValueError) as exc:
                return Section(
                    {"items": [], "active": True},
                    error=f"TCP EStats unavailable ({type(exc).__name__}).",
                )
        executable = next(
            (
                str(path)
                for path in (Path("/usr/share/bcc/tools/tcptop"), Path("/usr/sbin/tcptop-bpfcc"))
                if path.is_file()
            ),
            None,
        ) or shutil.which("tcptop-bpfcc")
        if not executable:
            return Section(
                {"items": [], "active": True},
                unavailable=[
                    "Install the optional BCC tcptop tool and run with existing BPF permissions. Titan does not install tools or elevate."
                ],
            )
        started = time.time()
        response = run_command([executable, "-C", "-S", "1", "1"], timeout=8)
        if not response.ok:
            return Section(
                {"items": [], "active": True},
                error="BCC TCP tracing failed or timed out; check installed BCC, kernel compatibility and BPF permissions.",
            )
        rates = parse_tcptop(response.stdout)
        # Revalidate identity after tracing; a recycled PID must not inherit activity.
        import psutil

        rows = []
        for item in items:
            if item.get("pid") not in rates:
                continue
            try:
                if (
                    abs(psutil.Process(item["pid"]).create_time() - float(item["create_time"]))
                    > 0.01
                ):
                    continue
            except (psutil.Error, ValueError, TypeError):
                continue
            down, up = rates[item["pid"]]
            rows.append(
                {
                    "pid": item["pid"],
                    "create_time": item["create_time"],
                    "download_bps": down,
                    "upload_bps": up,
                }
            )
        return Section(
            {
                "items": rows,
                "active": True,
                "observed_at": started,
                "scope": "Linux BCC IPv4/IPv6 TCP, one-second trace windows. Values rounded down to 1 KiB by tcptop. Setup gaps and UDP are not measured; absent processes are unknown, not zero.",
            }
        )

    def _windows(self, items: list[dict[str, Any]]) -> Section:
        if self.api is None:
            self.api = ctypes.WinDLL("iphlpapi.dll")  # type: ignore[attr-defined]
            self.api.GetExtendedTcpTable.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_int,
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            self.api.GetExtendedTcpTable.restype = ctypes.c_uint32
            self.api.GetPerTcpConnectionEStats.argtypes = [
                ctypes.POINTER(TcpRow),
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            self.api.GetPerTcpConnectionEStats.restype = ctypes.c_uint32
            self.api.SetPerTcpConnectionEStats.argtypes = [
                ctypes.POINTER(TcpRow),
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            self.api.SetPerTcpConnectionEStats.restype = ctypes.c_uint32
        length = ctypes.c_uint32()
        self.api.GetExtendedTcpTable(None, ctypes.byref(length), False, 2, 5, 0)
        buffer = None
        for _ in range(3):
            if not 4 <= length.value <= 4 * 1024 * 1024:
                raise ValueError("Invalid TCP table size")
            buffer = ctypes.create_string_buffer(length.value)
            status = self.api.GetExtendedTcpTable(buffer, ctypes.byref(length), False, 2, 5, 0)
            if status == 0:
                break
            if status != 122:
                raise OSError(status, "TCP table query failed")
        else:
            raise OSError("TCP table kept changing")
        assert buffer is not None
        count = min(
            ctypes.c_uint32.from_buffer_copy(buffer).value,
            (len(buffer) - 4) // ctypes.sizeof(TcpOwner),
            4096,
        )
        identities = {int(p["pid"]): p.get("create_time") for p in items}
        now = time.monotonic()
        current = {}
        totals: dict[int, list[float]] = {}
        denied = 0
        for index in range(count):
            owner = TcpOwner.from_buffer_copy(buffer, 4 + index * ctypes.sizeof(TcpOwner))
            if owner.state != 5 or owner.pid not in identities:
                continue
            row = TcpRow(
                owner.state,
                owner.local_addr,
                owner.local_port,
                owner.remote_addr,
                owner.remote_port,
            )
            enabled, stats = ctypes.c_ubyte(), DataStats()
            status = self.api.GetPerTcpConnectionEStats(
                ctypes.byref(row),
                1,
                ctypes.byref(enabled),
                0,
                1,
                None,
                0,
                0,
                ctypes.byref(stats),
                0,
                ctypes.sizeof(stats),
            )
            if status or not enabled.value:
                enabled.value = 1
                status = self.api.SetPerTcpConnectionEStats(
                    ctypes.byref(row), 1, ctypes.byref(enabled), 0, 1, 0
                )
                if status:
                    denied += 1
                continue
            key = (
                owner.pid,
                identities[owner.pid],
                owner.local_addr,
                owner.local_port,
                owner.remote_addr,
                owner.remote_port,
            )
            old = self.previous.get(key)
            current[key] = (now, stats.in_bytes, stats.out_bytes)
            if old and now > old[0] and stats.in_bytes >= old[1] and stats.out_bytes >= old[2]:
                value = totals.setdefault(owner.pid, [0.0, 0.0])
                value[0] += (stats.in_bytes - old[1]) / (now - old[0])
                value[1] += (stats.out_bytes - old[2]) / (now - old[0])
        self.previous = current
        return Section(
            {
                "items": [
                    {
                        "pid": pid,
                        "create_time": identities[pid],
                        "download_bps": values[0],
                        "upload_bps": values[1],
                    }
                    for pid, values in totals.items()
                ],
                "active": True,
                "observed_at": time.time(),
                "denied_connections": denied,
                "scope": "Windows IPv4 TCP payload bytes for connections present in two samples. UDP, IPv6 and short-lived connections are not included. EStats requests existing administrator rights; Titan never elevates. Windows retains enabled counters until the connection closes, as recommended by Microsoft.",
            },
            unavailable=[
                "TCP EStats collection denied for some connections; administrator rights may be needed."
            ]
            if denied
            else [],
        )
