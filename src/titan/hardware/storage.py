"""Physical storage inventory and read-only SMART interpretation.

smartctl exit codes are a bitmask, not a conventional success/failure code:
https://github.com/smartmontools/smartmontools/blob/master/smartmontools/smartctl.8.in
Only already enumerated whole-disk device paths can be queried. No self-tests,
configuration changes, filesystem repair, formatting or initialization occur.
"""

from __future__ import annotations

import re
import time
from typing import Any

from titan.core.models import Finding, Section
from titan.hardware.gpu import Runner
from titan.utils.commands import as_list, integer, json_object, number, run_command

DEVICE_PATTERN = re.compile(
    r"/dev/(?:sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+(?:n\d+)?|mmcblk\d+|pd\d+)"
)
SMART_TYPES = {
    "ata",
    "scsi",
    "nvme",
    "sat",
    "sat,12",
    "sat,16",
    "sat,auto",
    "sntjmicron",
    "sntasmedia",
    "sntrealtek",
}


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def parse_smart_json(payload: dict[str, Any], returncode: int = 0) -> dict[str, Any]:
    status = integer(_nested(payload, "smartctl", "exit_status"))
    status = returncode if status is None else status
    reported = _nested(payload, "smart_status", "passed")
    passed = reported if isinstance(reported, bool) else None
    health: dict[str, Any] = {
        "source": "smartctl",
        "available": False,
        "status": "Health unavailable",
        "reported_passed": passed,
        "exit_status": status,
        "temperature_c": number(_nested(payload, "temperature", "current")),
        "power_on_hours": number(_nested(payload, "power_on_time", "hours")),
        "query_incomplete": bool(status & 7),
        "notice": "SMART describes reported device observations; it cannot guarantee future reliability.",
    }
    nvme = payload.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        health["protocol"] = "NVMe"
        for key in (
            "critical_warning",
            "available_spare",
            "available_spare_threshold",
            "percentage_used",
            "media_errors",
            "num_err_log_entries",
        ):
            health[key] = integer(nvme.get(key))
        health["temperature_c"] = (
            health["temperature_c"]
            if health["temperature_c"] is not None
            else number(nvme.get("temperature"))
        )
        health["power_on_hours"] = (
            health["power_on_hours"]
            if health["power_on_hours"] is not None
            else number(nvme.get("power_on_hours"))
        )
    else:
        table = _nested(payload, "ata_smart_attributes", "table")
        health["protocol"] = (
            "ATA"
            if isinstance(table, list)
            else str(_nested(payload, "device", "protocol") or "Unknown")
        )
        # IDs are vendor-specific. Interpret only the conventional *named* sector
        # counters rather than SSD raw attributes with unrelated meanings.
        for attribute in as_list(table):
            if not isinstance(attribute, dict):
                continue
            attribute_id = integer(attribute.get("id"))
            name = str(attribute.get("name") or "").lower()
            key = None
            if attribute_id == 5 and "realloc" in name and "sector" in name:
                key = "reallocated_sectors"
            elif attribute_id == 197 and "pending" in name and "sector" in name:
                key = "pending_sectors"
            elif attribute_id == 198 and "uncorrect" in name:
                key = "uncorrectable_sectors"
            if key:
                health[key] = integer(_nested(attribute, "raw", "value"))
        scsi_defects = integer(payload.get("scsi_grown_defect_list"))
        if scsi_defects is not None:
            health["grown_defects"] = scsi_defects
    has_measurement = passed is not None or any(
        health.get(key) is not None
        for key in (
            "temperature_c",
            "power_on_hours",
            "critical_warning",
            "media_errors",
            "reallocated_sectors",
            "pending_sectors",
            "uncorrectable_sectors",
            "grown_defects",
            "percentage_used",
        )
    )
    health["available"] = has_measurement or bool(status & 0b11111000)
    warning = (
        passed is False
        or bool(status & 0b00011000)
        or any(
            (health.get(key) or 0) > 0
            for key in (
                "critical_warning",
                "media_errors",
                "pending_sectors",
                "uncorrectable_sectors",
                "reallocated_sectors",
                "grown_defects",
            )
        )
    )
    if health["available"]:
        health["status"] = (
            "Warning reported"
            if warning
            else ("SMART reports passed" if passed else "SMART data available")
        )
        if health["query_incomplete"]:
            health["status"] += " (partial query)"
    return health


def _device_alias(device: str, windows: bool) -> str:
    if windows and re.fullmatch(r"/dev/sd[a-z]+", device):
        # smartmontools documents sd[a-z] -> PhysicalDrive[0-25], then sd[aa..].
        index = 0
        for letter in device.removeprefix("/dev/sd"):
            index = index * 26 + ord(letter) - ord("a") + 1
        return f"/dev/pd{index - 1}"
    return device


def add_smart_health(section: Section, runner: Runner = run_command, budget: float = 6.0) -> None:
    drives = section.data.get("drives", [])
    if not drives:
        return
    deadline = time.monotonic() + budget
    scan = runner(["smartctl", "--scan", "-j"], timeout=min(2.0, budget))
    if scan.error:
        section.unavailable.append(scan.error)
        return
    scan_payload = json_object(scan) or {}
    device_types = {}
    for scanned in as_list(scan_payload.get("devices")):
        if not isinstance(scanned, dict):
            continue
        name, kind = scanned.get("name"), scanned.get("type")
        if isinstance(name, str) and DEVICE_PATTERN.fullmatch(name) and kind in SMART_TYPES:
            device_types[_device_alias(name, section.data.get("platform") == "Windows")] = kind
    for drive in drives:
        device = drive.get("device")
        if not isinstance(device, str) or not DEVICE_PATTERN.fullmatch(device):
            drive["health"] = {
                "available": False,
                "status": "Health unavailable",
                "reason": "Device is outside the supported enumerated path formats",
            }
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            section.unavailable.append(
                "Remaining drive-health queries deferred: collection time budget reached"
            )
            break
        args = ["smartctl", "-a", "-j", "-q", "noserial"]
        if device in device_types:
            args += ["-d", device_types[device]]
        args.append(device)
        result = runner(args, timeout=min(2.0, remaining))
        payload = json_object(result)
        if payload is None:
            drive["health"] = {
                "available": False,
                "status": "Health unavailable",
                "reason": result.error or "SMART inaccessible, unsupported or unreadable",
            }
        else:
            drive["health"] = parse_smart_json(payload, result.returncode or 0)


def collect_storage(include_smart: bool = True) -> Section:
    from titan.platforms.base import get_provider

    section = get_provider().collect_storage()
    if include_smart:
        add_smart_health(section)
    return section


def storage_findings(section: Section) -> list[Finding]:
    findings = []

    def add(
        identity: str,
        title: str,
        severity: str,
        evidence: str,
        explanation: str,
        causes: list[str],
        next_step: str,
        confidence: str = "high",
    ) -> None:
        findings.append(
            Finding(identity, title, severity, evidence, explanation, causes, next_step, confidence)
        )

    backup = "Preserve important data and make a backup before maintenance. Do not initialize or format a drive containing unrecovered files."
    for drive in section.data.get("drives", []):
        identity, name = str(drive.get("id")), str(drive.get("model") or drive.get("name"))
        health = drive.get("health") or {}
        if (
            health.get("reported_passed") is False
            or (integer(health.get("exit_status")) or 0) & 8
            or (health.get("critical_warning") or 0)
        ):
            add(
                f"storage-health-{identity}",
                f"{name}: device reports a health warning",
                "critical",
                f"SMART passed={health.get('reported_passed')}; critical warning={health.get('critical_warning')}; exit status={health.get('exit_status')}",
                "The device reports a current health failure or NVMe critical warning. This is direct device evidence, not a prediction of when it will fail.",
                [
                    "Drive reliability or spare-capacity problem",
                    "A device-reported critical condition",
                ],
                backup
                + " Consult the drive vendor or a qualified recovery specialist if data is irreplaceable.",
            )
        counters = {
            key: health[key]
            for key in (
                "media_errors",
                "pending_sectors",
                "uncorrectable_sectors",
                "reallocated_sectors",
                "grown_defects",
            )
            if (health.get(key) or 0) > 0
        }
        if counters:
            add(
                f"storage-errors-{identity}",
                f"{name}: storage error counters reported",
                "warning",
                str(counters),
                "These are cumulative reported errors or sector counters. A trend and vendor context are needed to establish whether a fault is progressing.",
                ["Historical media damage", "Developing media problems"],
                backup + " Compare counters over time and review vendor diagnostics.",
            )
        spare, threshold = health.get("available_spare"), health.get("available_spare_threshold")
        if spare is not None and threshold is not None and spare < threshold:
            add(
                f"storage-spare-{identity}",
                f"{name}: NVMe spare below device threshold",
                "warning",
                f"Available spare {spare}%; device threshold {threshold}%.",
                "The drive reports spare capacity below its own threshold.",
                ["Reserved capacity has been consumed"],
                backup,
            )
        if (health.get("percentage_used") or 0) >= 100:
            add(
                f"storage-endurance-{identity}",
                f"{name}: NVMe estimated endurance consumed",
                "warning",
                f"Percentage used={health['percentage_used']}%.",
                "This vendor estimate reached the nominal endurance budget; it does not mean the drive has necessarily failed.",
                ["Cumulative writes and device wear"],
                backup + " Plan replacement using the vendor's guidance.",
            )
        status = integer(health.get("exit_status")) or 0
        if status & 16:
            add(
                f"storage-attribute-{identity}",
                f"{name}: SMART attribute threshold reached",
                "warning",
                "smartctl exit status bit 4 is set.",
                "A current SMART attribute is at or below its device-defined threshold.",
                ["Device-reported pre-failure attribute"],
                backup,
            )
        if status & 0b11100000:
            add(
                f"storage-logs-{identity}",
                f"{name}: historical SMART events",
                "attention",
                f"smartctl historical status bits={status & 0b11100000}.",
                "Past attribute failures, error-log records or self-test failures were reported. Historical entries alone do not prove a current failure.",
                ["Older recorded errors", "An earlier unsuccessful self-test"],
                "Review event ages and vendor interpretation; compare with current health and recent symptoms.",
            )
        if drive.get("read_only") is True:
            add(
                f"storage-readonly-{identity}",
                f"{name}: read-only device",
                "attention",
                "The operating system marks this physical drive read-only.",
                "Writes are blocked at the device level. This can be intentional and is not alone proof of hardware failure.",
                ["Write protection", "Policy", "A protective device or OS state"],
                backup + " Review read-only status in the OS storage tools.",
            )
        if drive.get("offline") is True:
            add(
                f"storage-offline-{identity}",
                f"{name}: detected but offline",
                "attention",
                "The Windows storage API reports IsOffline=true.",
                "The device is detected but unavailable for normal mounted-volume access.",
                ["OS policy", "Duplicate disk signature", "Administrative state"],
                backup + " Inspect its status in Disk Management.",
            )
            continue
        parts = [part for part in drive.get("partitions", []) if not part.get("system_partition")]
        if drive.get("partitions_known") and not drive.get("partitions"):
            add(
                f"storage-no-volume-{identity}",
                f"{name}: no accessible volume enumerated",
                "attention",
                "Physical drive is detected; no partition or whole-disk filesystem was enumerated.",
                "This snapshot cannot distinguish a new blank disk, an unsupported layout, encryption, or damaged partition metadata.",
                ["Blank drive", "Unsupported storage layout", "Unreadable partition metadata"],
                backup
                + " Check the cable and adapter, then inspect the drive in the operating system's storage viewer.",
                "medium",
            )
        for index, part in enumerate(parts):
            filesystem = part.get("filesystem")
            if filesystem is not None and str(filesystem).lower() == "raw":
                add(
                    f"storage-raw-{identity}-{index}",
                    f"{name}: RAW filesystem reported",
                    "warning",
                    f"{part.get('device')}: filesystem=RAW.",
                    "The OS cannot identify a usable filesystem. This does not prove the drive itself is broken.",
                    ["Unformatted partition", "Damaged metadata", "Unsupported filesystem"],
                    backup + " Seek data recovery advice before filesystem repair if files matter.",
                )
            elif not part.get("mountpoint"):
                title = (
                    "partition has no drive letter"
                    if section.data.get("platform") == "Windows" and part.get("volume_detected")
                    else "partition is not mounted"
                )
                add(
                    f"storage-unmounted-{identity}-{index}",
                    f"{name}: {title}",
                    "attention",
                    f"{part.get('device')}: no ordinary mount path; filesystem={filesystem or 'unidentified'}.",
                    "An unmounted partition will not appear as a normal file-manager volume. This may be intentional. Encryption and unsupported filesystems require additional context.",
                    [
                        "No mount or drive-letter assignment",
                        "Unsupported or encrypted volume",
                        "Filesystem metadata problem",
                    ],
                    backup
                    + " Inspect the partition's purpose before assigning a mount point or drive letter.",
                    "medium",
                )
    for bridge in section.data.get("usb_storage_bridges", []):
        if bridge.get("mass_storage_interface") is True and bridge.get("has_block_device") is False:
            add(
                f"storage-usb-{bridge.get('id')}",
                "USB storage interface has no block device",
                "attention",
                str(bridge.get("source")),
                "A USB mass-storage interface exists in sysfs without a descendant block device. This can also be an empty card reader; adapter failure and drive failure cannot be distinguished from this snapshot.",
                [
                    "Empty removable-media reader",
                    "Cable, adapter or power problem",
                    "Drive not responding",
                ],
                "Try a direct USB port and a known-good cable or adapter. Preserve unrecovered data; do not initialize the drive.",
                "medium",
            )
    if section.error:
        add(
            "storage-unavailable",
            "Storage diagnosis has incomplete evidence",
            "informational",
            section.error,
            "Physical storage inventory could not be completed; absence from this result is not proof that a drive is absent.",
            ["Missing tool", "Access restriction", "OS inventory query failure"],
            "Review Titan doctor and the operating system's device/storage viewer.",
            "low",
        )
    return findings
