"""Read-only native disk performance with precise 100ns-to-ms conversion.

psutil 7.2.2's Windows disk times lose sub-second resolution. Reading the
documented DISK_PERFORMANCE structure avoids guessing its exposed units.
"""

from __future__ import annotations

import ctypes
import re
from types import SimpleNamespace
from typing import Any, ClassVar


class Performance(ctypes.Structure):
    _fields_: ClassVar[list[Any]] = (
        [
            (name, ctypes.c_int64)
            for name in ("bytes_read", "bytes_written", "read_time", "write_time", "idle_time")
        ]
        + [
            (name, ctypes.c_uint32)
            for name in ("read_count", "write_count", "queue_depth", "split_count")
        ]
        + [
            ("query_time", ctypes.c_int64),
            ("device_number", ctypes.c_uint32),
            ("manager", ctypes.c_uint16 * 8),
        ]
    )


def decode_performance(value: Performance) -> Any:
    return SimpleNamespace(
        read_bytes=value.bytes_read,
        write_bytes=value.bytes_written,
        read_count=value.read_count,
        write_count=value.write_count,
        read_time=value.read_time / 10000,
        write_time=value.write_time / 10000,
        queue_depth=value.queue_depth,
    )


def disk_performance(names: list[str], api: Any = None) -> dict[str, Any]:
    if api is None:
        api = ctypes.WinDLL("kernel32.dll", use_last_error=True)  # type: ignore[attr-defined]
        api.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        api.CreateFileW.restype = ctypes.c_void_p
        api.DeviceIoControl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        api.DeviceIoControl.restype = ctypes.c_int
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
    result = {}
    for name in names[:256]:
        if not re.fullmatch(r"PhysicalDrive\d{1,6}", name):
            continue
        handle = api.CreateFileW("\\\\.\\" + name, 0, 3, None, 3, 0, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            continue
        try:
            capacity = 1024
            while capacity <= 65536:
                buffer = ctypes.create_string_buffer(capacity)
                returned = ctypes.c_uint32()
                if api.DeviceIoControl(
                    handle, 0x70020, None, 0, buffer, capacity, ctypes.byref(returned), None
                ):
                    if ctypes.sizeof(Performance) <= returned.value <= capacity:
                        result[name] = decode_performance(Performance.from_buffer_copy(buffer))
                    break
                if getattr(ctypes, "get_last_error", lambda: 0)() != 122:
                    break
                capacity *= 2
        finally:
            api.CloseHandle(handle)
    return result
