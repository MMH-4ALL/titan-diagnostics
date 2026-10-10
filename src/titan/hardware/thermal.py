"""Platform sensor readings and direct Linux thermal-event counter evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from titan.core.models import Section
from titan.core.monitoring import finite
from titan.utils.commands import as_list, json_object, run_command

THERMAL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
foreach ($namespace in @('root\LibreHardwareMonitor','root\OpenHardwareMonitor')) {
  try {
    $hardware = @(Get-CimInstance -Namespace $namespace -ClassName Hardware -ErrorAction Stop)
    $cpuIds = @($hardware | Where-Object { $_.HardwareType -eq 'Cpu' } | ForEach-Object Identifier)
    $sensors = @(Get-CimInstance -Namespace $namespace -ClassName Sensor -ErrorAction Stop |
      Where-Object { $_.SensorType -eq 'Temperature' -and ($cpuIds -contains $_.Parent) } |
      Select-Object Name,Value,Identifier)
    @{source=$namespace; sensors=$sensors} | ConvertTo-Json -Depth 5 -Compress
    exit
  } catch {}
}
@{source=$null; sensors=@()} | ConvertTo-Json -Compress
"""


class ThermalSampler:
    def __init__(self) -> None:
        self.previous: dict[str, int] = {}

    def collect(self, backend: Any, provider: Any) -> Section:
        sensors: list[dict[str, Any]] = []
        sysfs = getattr(provider, "sysfs", None)
        source = "OS sensors"
        if sysfs is not None:
            try:
                groups = backend.sensors_temperatures()
            except (AttributeError, OSError):
                groups = {}
            for group, readings in groups.items():
                if any(
                    term in group.lower()
                    for term in ("coretemp", "k10temp", "zenpower", "cpu", "acpitz")
                ):
                    # ACPI zones are explicitly marked; they may not represent the CPU.
                    for reading in readings:
                        sensors.append(
                            {
                                "label": f"{group} / {reading.label or 'sensor'}",
                                "temperature_c": finite(reading.current),
                                "high_c": finite(reading.high),
                                "critical_c": finite(reading.critical),
                                "cpu_sensor": group.lower() != "acpitz",
                            }
                        )
            counters = {}
            for path in list(
                Path(sysfs).glob("devices/system/cpu/cpu[0-9]*/thermal_throttle/*_throttle_count")
            )[:4096]:
                try:
                    counters[str(path.relative_to(sysfs))] = int(path.read_text().strip())
                except (OSError, ValueError):
                    pass
            increments = {
                key: value - self.previous[key]
                for key, value in counters.items()
                if key in self.previous
                and value >= self.previous[key]
                and value != self.previous[key]
            }
            self.previous = counters
            throttle = {
                "available": bool(counters),
                "counters": counters,
                "increments": increments,
                "source": "Linux sysfs thermal_throttle",
                "note": "Per-CPU counters since boot. Package events are mirrored across CPUs; do not sum. An increase proves a thermal event in the interval, not continuous throttling.",
            }
        elif provider.__class__.__name__ == "WindowsProvider":
            result = getattr(provider, "runner", run_command)(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", THERMAL_SCRIPT],
                timeout=3,
            )
            payload = json_object(result) or {}
            source = (
                payload.get("source")
                or "LibreHardwareMonitor / OpenHardwareMonitor WMI unavailable"
            )
            for item in as_list(payload.get("sensors")):
                if isinstance(item, dict):
                    sensors.append(
                        {
                            "label": item.get("Name"),
                            "temperature_c": finite(item.get("Value")),
                            "high_c": None,
                            "critical_c": None,
                            "cpu_sensor": True,
                        }
                    )
            throttle = {
                "available": False,
                "increments": {},
                "note": "Windows does not expose direct thermal throttle counters through this collector. A low frequency alone is not proof.",
            }
        else:
            throttle = {"available": False, "increments": {}}
        return Section(
            {"sensors": sensors, "source": source, "throttling": throttle},
            unavailable=(
                [
                    "CPU sensor data; Windows can read an already running LibreHardwareMonitor/OpenHardwareMonitor WMI provider"
                ]
                if not sensors
                else []
            )
            + (["Direct thermal throttle counters"] if not throttle["available"] else []),
        )
