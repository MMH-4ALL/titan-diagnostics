"""Modular GPU providers. A missing metric is always None, never zero."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from typing import Any

from titan.core.models import Section
from titan.utils.commands import CommandResult, number, run_command

Runner = Callable[..., CommandResult]
GPU_METRICS = (
    "utilization_percent",
    "memory_used_bytes",
    "memory_total_bytes",
    "temperature_c",
    "clock_mhz",
    "power_w",
)
NVIDIA_QUERY: Sequence[str] = (
    "nvidia-smi",
    "--query-gpu=index,name,pci.bus_id,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu,clocks.current.graphics,power.draw",
    "--format=csv,noheader,nounits",
)


def empty_gpu(identity: str, name: str, vendor: str, driver: str | None = None) -> dict[str, Any]:
    return {
        "id": identity,
        "name": name,
        "vendor": vendor,
        "driver": driver,
        **dict.fromkeys(GPU_METRICS),
        "unavailable": list(GPU_METRICS),
    }


def parse_nvidia_csv(output: str) -> list[dict[str, Any]]:
    devices = []
    for row in csv.reader(io.StringIO(output), skipinitialspace=True):
        if len(row) != 10:
            continue
        row = [field.strip() for field in row]
        gpu = empty_gpu(row[2], row[1], "NVIDIA", row[3])
        for key, raw in zip(GPU_METRICS, row[4:], strict=True):
            value = number(raw)
            if value is not None and value >= 0:
                if key.endswith("_bytes"):
                    value = int(value * 1024 * 1024)
                gpu[key] = value
        gpu["unavailable"] = [key for key in GPU_METRICS if gpu[key] is None]
        devices.append(gpu)
    return devices


def nvidia_devices(runner: Runner = run_command) -> tuple[list[dict[str, Any]], str | None]:
    result = runner(NVIDIA_QUERY, timeout=3.0)
    if not result.ok:
        return [], result.error or "NVIDIA driver query failed"
    devices = parse_nvidia_csv(result.stdout)
    return devices, None if devices else "NVIDIA returned no readable GPU metrics"


def collect_gpu() -> Section:
    from titan.platforms.base import get_provider

    return get_provider().collect_gpu()
