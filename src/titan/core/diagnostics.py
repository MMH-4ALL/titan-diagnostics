"""Conservative offline rules: evidence first, no universal health score."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from titan.core.config import Config
from titan.core.models import Finding, Snapshot


def _data(sample: Snapshot, section: str) -> dict[str, Any]:
    value = sample.sections.get(section)
    return value.data if value is not None and value.error is None else {}


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _sustained(
    samples: Sequence[Snapshot], predicate: Callable[[Snapshot], bool], config: Config
) -> list[Snapshot]:
    """Return the latest uninterrupted qualifying run, measuring actual time."""
    run: list[Snapshot] = []
    max_gap = max(config.refresh_seconds * 3, 2.0)
    for sample in samples:
        if not predicate(sample):
            run = []
            continue
        if run and sample.timestamp - run[-1].timestamp > max_gap:
            run = []
        run.append(sample)
    if len(run) >= 2 and run[-1].timestamp - run[0].timestamp >= config.sustained_seconds:
        return run
    return []


def evaluate(samples: Sequence[Snapshot], config: Config | None = None) -> list[Finding]:
    settings = config or Config()
    ordered = sorted(
        {
            sample.timestamp: sample for sample in samples if math.isfinite(sample.timestamp)
        }.values(),
        key=lambda sample: sample.timestamp,
    )
    if not ordered:
        return []
    latest = ordered[-1]
    findings: list[Finding] = []

    def cpu_busy(sample: Snapshot) -> bool:
        percent = _numeric(_data(sample, "cpu").get("percent"))
        return percent is not None and percent >= settings.cpu_threshold

    cpu_run = _sustained(ordered, cpu_busy, settings)
    if cpu_run:
        values = [float(_data(sample, "cpu")["percent"]) for sample in cpu_run]
        elapsed = cpu_run[-1].timestamp - cpu_run[0].timestamp
        findings.append(
            Finding(
                id="cpu-sustained",
                title="Sustained high CPU activity",
                severity="warning",
                evidence=f"CPU utilization was {min(values):.1f}–{max(values):.1f}% across {len(values)} contiguous observations over {elapsed:.1f} seconds; configured threshold {settings.cpu_threshold:g}%.",
                explanation="Sustained CPU demand can delay other applications. Utilization alone does not prove overheating or throttling.",
                possible_causes=[
                    "An application is performing intensive work",
                    "Several applications are active",
                    "Background updates or indexing",
                ],
                next_step="Inspect the highest CPU-consuming processes and determine whether their work is expected before requesting termination.",
                confidence="high",
            )
        )

    def memory_pressure(sample: Snapshot) -> bool:
        data = _data(sample, "memory")
        percent = _numeric(data.get("percent"))
        total, available, swap = (
            _numeric(data.get(key)) for key in ("total", "available", "swap_percent")
        )
        low_available = (
            total is not None and total > 0 and available is not None and available / total <= 0.10
        )
        active_swap = swap is not None and swap >= 50
        return (
            percent is not None
            and percent >= settings.memory_threshold
            and (low_available or active_swap)
        )

    memory_run = _sustained(ordered, memory_pressure, settings)
    if memory_run:
        values = [float(_data(sample, "memory")["percent"]) for sample in memory_run]
        elapsed = memory_run[-1].timestamp - memory_run[0].timestamp
        findings.append(
            Finding(
                id="memory-pressure",
                title="Sustained memory pressure",
                severity="warning",
                evidence=f"RAM usage remained {min(values):.1f}–{max(values):.1f}% for {elapsed:.1f} seconds with low available memory or substantial swap usage.",
                explanation="Limited available memory may increase paging and responsiveness problems. Cached memory is reusable; high reported usage alone is insufficient evidence.",
                possible_causes=[
                    "Large application workloads",
                    "Many active applications",
                    "An application retaining memory",
                ],
                next_step="Inspect memory-consuming processes and compare available memory over time. Save work before closing applications.",
                confidence="high",
            )
        )

    storage = latest.sections.get("storage")
    if storage is not None:
        from titan.hardware.storage import storage_findings

        findings.extend(storage_findings(storage))
        for index, volume in enumerate(storage.data.get("volumes", [])):
            percent, free, total = (
                _numeric(volume.get(key)) for key in ("percent", "free", "total")
            )
            if (
                percent is not None
                and total is not None
                and total > 0
                and free is not None
                and percent >= 90
            ):
                findings.append(
                    Finding(
                        id=f"disk-space-{index}",
                        title="A mounted volume is nearly full",
                        severity="critical"
                        if percent >= 99
                        else "warning"
                        if percent >= 95
                        else "attention",
                        evidence=f"Volume {volume.get('mountpoint', 'unknown')} uses {percent:.1f}% of {total / (1024**3):.1f} GiB; {free / (1024**3):.2f} GiB remains.",
                        explanation="Low filesystem space can prevent updates and application writes. This measures the mounted filesystem, not physical drive health.",
                        possible_causes=[
                            "Accumulated application data",
                            "Large downloads or media",
                            "Temporary files",
                        ],
                        next_step="Review storage usage and move or remove only files you recognize after backing up important data.",
                        confidence="high",
                    )
                )

    battery = _data(latest, "battery")
    batteries = battery.get("batteries") or [battery]
    for index, measured_battery in enumerate(batteries):
        wear = _numeric(measured_battery.get("wear_percent"))
        design = _numeric(
            measured_battery.get("design_capacity_mwh", measured_battery.get("design_capacity"))
        )
        full = _numeric(
            measured_battery.get("full_capacity_mwh", measured_battery.get("full_charge_capacity"))
        )
        if (
            wear is not None
            and design is not None
            and full is not None
            and design > 0
            and 0 <= full <= design
            and 20 <= wear <= 100
            and abs(wear - 100 * (1 - full / design)) <= 2
        ):
            units = (
                " mWh" if "design_capacity_mwh" in measured_battery else " reported capacity units"
            )
            findings.append(
                Finding(
                    id="battery-wear" if len(batteries) == 1 else f"battery-wear-{index}",
                    title="Battery capacity is reduced",
                    severity="warning" if wear >= 40 else "attention",
                    evidence=f"Reported full-charge capacity is {full:g}{units} versus design capacity {design:g}{units}; reported wear is {wear:.1f}%.",
                    explanation="The battery reports lower capacity than its design value. Firmware estimates can change after calibration; this is not a guaranteed runtime prediction.",
                    possible_causes=[
                        "Battery aging",
                        "Charge-cycle wear",
                        "Capacity estimation changes",
                    ],
                    next_step="Compare a platform battery report and observed runtime. Consult the device manufacturer if runtime is inadequate or the battery is swollen.",
                    confidence="medium",
                )
            )

    network = _data(latest, "network")
    test = network.get("diagnostic_test")
    if isinstance(test, dict) and test.get("success") is False:
        findings.append(
            Finding(
                id="network-test-failed",
                title="The requested connectivity test failed",
                severity="attention",
                evidence=str(
                    test.get("error", "The explicitly requested endpoint did not respond.")
                ),
                explanation="Failure to reach one selected endpoint does not establish a general internet outage.",
                possible_causes=[
                    "The selected service is unavailable",
                    "DNS or routing trouble",
                    "A firewall blocks the selected connection",
                ],
                next_step="Verify the endpoint and port, then compare another endpoint you explicitly choose.",
                confidence="medium",
            )
        )

    latest_processes = _data(latest, "processes").get("items", [])
    process_window = [
        sample
        for sample in ordered
        if latest.timestamp - sample.timestamp
        <= max(75, settings.sustained_seconds + 3 * settings.refresh_seconds)
    ]
    process_index = {
        sample.timestamp: {
            (item.get("pid"), item.get("create_time")): item
            for item in _data(sample, "processes").get("items", [])
        }
        for sample in process_window
    }
    for process in latest_processes:
        pid, created = process.get("pid"), process.get("create_time")
        # Windows PID 0 accounts for idle time, not application workload.
        if pid is None or pid == 0 or created is None:
            continue

        def match(
            sample: Snapshot, identity: tuple[Any, Any] = (pid, created)
        ) -> dict[str, Any] | None:
            return process_index[sample.timestamp].get(identity)

        def process_busy(sample: Snapshot) -> bool:
            item = match(sample)
            value = _numeric(item.get("cpu_percent")) if item else None
            return value is not None and value >= 80

        run = _sustained(process_window, process_busy, settings)
        if run:
            elapsed = run[-1].timestamp - run[0].timestamp
            findings.append(
                Finding(
                    id=f"process-cpu-{pid}",
                    title="A process is persistently CPU-active",
                    severity="attention",
                    evidence=f"{process.get('name', 'Process')} (PID {pid}) consumed at least 80% of one logical CPU for {elapsed:.1f} seconds across {len(run)} observations with the same process creation time.",
                    explanation="Process percentages can exceed 100% when multiple logical CPUs are used. Intensive work may be expected.",
                    possible_causes=[
                        "Expected intensive application work",
                        "A busy background job",
                        "An application loop",
                    ],
                    next_step="Inspect the process details and its purpose. Save work before using Titan's confirmed termination action.",
                    confidence="high",
                )
            )
        observations = [(sample.timestamp, match(sample)) for sample in process_window]
        if len(observations) < 4 or any(item is None for _, item in observations):
            continue
        if any(
            second[0] - first[0] > max(settings.refresh_seconds * 3, 2)
            for first, second in zip(observations, observations[1:], strict=False)
        ):
            continue
        elapsed = observations[-1][0] - observations[0][0]
        memory = [
            _numeric(item.get("memory_bytes")) for _, item in observations if item is not None
        ]
        if elapsed < 60 or any(value is None for value in memory):
            continue
        numbers = [value for value in memory if value is not None]
        growth = numbers[-1] - numbers[0]
        rising = sum(right >= left for left, right in zip(numbers, numbers[1:], strict=False)) / (
            len(numbers) - 1
        )
        if growth >= 32 * 1024**2 and growth >= max(numbers[0], 1) * 0.25 and rising >= 0.8:
            findings.append(
                Finding(
                    id=f"process-memory-growth-{pid}",
                    title="Sustained process memory growth",
                    severity="attention",
                    evidence=f"{process.get('name', 'Process')} (PID {pid}) resident memory grew {growth / 1024**2:.1f} MiB over {elapsed:.0f} seconds; identity remained consistent.",
                    explanation="Continued growth may warrant investigation, but workload changes and caching can produce the same pattern. A memory leak is suspected, not confirmed.",
                    possible_causes=[
                        "A growing application workload",
                        "Application caching",
                        "Possible memory retention or leak",
                    ],
                    next_step="Observe a longer window under a steady workload and compare the application's own diagnostics.",
                    confidence="low",
                )
            )

    for alert in _data(latest, "storage").get("health_trend_alerts", []):
        findings.append(
            Finding(
                id=f"health-trend-{alert['identity']}-{alert['metric']}",
                title="Drive health counter changed",
                severity="attention" if alert["metric"] == "percentage_used" else "warning",
                evidence=f"{alert.get('device')}: {alert['metric']} changed from {alert['before']:g} to {alert['after']:g} between successful reads at {alert['from_timestamp']:.0f} and {alert['timestamp']:.0f}. Identity: {alert['identity_quality']}.",
                explanation="A change in drive-reported errors, spare capacity or wear deserves review. Wear increases can be normal. Location-based identity may confuse a replacement of the same model and size.",
                possible_causes=[
                    "Drive deterioration",
                    "Expected endurance consumption",
                    "A replacement drive with indistinguishable identity",
                ],
                next_step="Preserve important data and compare the device's own SMART records. Confirm this is the same physical drive before attributing the change.",
                confidence="medium"
                if alert["identity_quality"] == "hashed OS hardware identity"
                else "low",
            )
        )
    thermal = _data(latest, "thermal")
    increments = thermal.get("throttling", {}).get("increments", {})
    if increments:
        findings.append(
            Finding(
                id="thermal-events",
                title="Thermal throttle event observed",
                severity="warning",
                evidence="; ".join(
                    f"{name}: +{value}" for name, value in list(increments.items())[:8]
                ),
                explanation="The kernel's direct thermal-event counter rose since the previous sensor read. Package counters mirror across CPUs. This proves an event during that interval, not continuous throttling.",
                possible_causes=[
                    "Cooling limitations during load",
                    "Restricted airflow",
                    "High ambient temperature",
                ],
                next_step="Review CPU temperatures and airflow under the same workload. Frequency alone cannot identify thermal throttling.",
                confidence="high",
            )
        )
    for sensor in thermal.get("sensors", []):
        temperature, critical = (
            _numeric(sensor.get("temperature_c")),
            _numeric(sensor.get("critical_c")),
        )
        if (
            sensor.get("cpu_sensor")
            and temperature is not None
            and critical is not None
            and temperature >= critical
        ):
            findings.append(
                Finding(
                    id=f"thermal-critical-{sensor.get('label')}",
                    title="CPU sensor reached its reported critical limit",
                    severity="critical",
                    evidence=f"{sensor.get('label')}: {temperature:g} C; sensor-reported critical threshold {critical:g} C.",
                    explanation="This compares an actual sensor reading with its own threshold, rather than applying a universal CPU temperature limit.",
                    possible_causes=[
                        "Insufficient cooling",
                        "High workload",
                        "Sensor reporting error",
                    ],
                    next_step="Reduce the workload, check cooling and verify the reading with the platform's hardware monitor.",
                    confidence="medium",
                )
            )
    for name, section in latest.sections.items():
        if section.error:
            findings.append(
                Finding(
                    id=f"collection-{name}",
                    title=f"{name.title()} data could not be collected",
                    severity="informational",
                    evidence=section.error,
                    explanation="An unavailable collector does not establish a hardware fault.",
                    possible_causes=[
                        "Permission restrictions",
                        "An unsupported platform API",
                        "A device changed during collection",
                    ],
                    next_step="Read the collector's status and run titan doctor. Retry ordinary monitoring before considering elevated access.",
                    confidence="high",
                )
            )
    priority = {"critical": 0, "warning": 1, "attention": 2, "informational": 3}
    unique = {finding.id: finding for finding in findings}
    return sorted(
        unique.values(), key=lambda finding: (priority.get(finding.severity, 4), finding.id)
    )
