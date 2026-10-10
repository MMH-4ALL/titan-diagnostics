"""Linux inventory from lsblk and documented sysfs attributes."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shlex
from pathlib import Path
from typing import Any

from titan.core.models import Section
from titan.hardware.battery import wear_percent
from titan.hardware.gpu import GPU_METRICS, Runner, empty_gpu, nvidia_devices
from titan.utils.commands import as_list, integer, json_object, number, run_command

LSBLK_ARGS = [
    "lsblk",
    "--json",
    "--bytes",
    "--output",
    "NAME,PATH,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS,ROTA,TRAN,RO,PKNAME,PARTTYPE",
]
SYSTEM_PARTITIONS = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
}


def parse_pci_names(output: str) -> dict[str, str]:
    """Parse lspci's documented machine-readable single-line format."""
    names = {}
    for line in output.splitlines():
        try:
            # Options such as -r01/-p00 can appear between positional fields.
            fields = [field for field in shlex.split(line) if not field.startswith("-")]
        except ValueError:
            continue
        if len(fields) >= 4 and re.fullmatch(
            r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", fields[0]
        ):
            names[fields[0].lower()] = f"{fields[2]} {fields[3]}"
    return names


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def usb_has_block_device(interface: Path) -> bool | None:
    """Return False only after a readable traversal, not after access denial."""
    errors: list[OSError] = []
    try:
        # os.walk reads a symlink passed as its top directory, without following
        # other symlinks beneath it. This avoids loops through sysfs backlinks.
        for current, directories, _files in os.walk(interface, onerror=errors.append):
            if Path(current).name == "block" and directories:
                return True
    except OSError:
        return None
    return None if errors else False


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, "0"):
        return False
    if value in (1, "1"):
        return True
    return None


def _descendants(node: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for child in as_list(node.get("children")):
        if isinstance(child, dict):
            result.append(child)
            result.extend(_descendants(child))
    return result


def _partition(node: dict[str, Any]) -> dict[str, Any]:
    mountpoints = [
        point
        for point in as_list(node.get("mountpoints", node.get("mountpoint")))
        if isinstance(point, str) and point
    ]
    filesystem = node.get("fstype") or None
    # swap and encrypted/LVM containers are not ordinary mountable filesystems.
    container = filesystem in {"swap", "LVM2_member", "crypto_LUKS", "linux_raid_member"}
    return {
        "device": node.get("path") or f"/dev/{node.get('name', '?')}",
        "mountpoint": mountpoints[0] if mountpoints else None,
        "mountpoints": mountpoints,
        "filesystem": filesystem,
        "letter": None,
        "size": integer(node.get("size")),
        "kind": node.get("type"),
        "system_partition": container
        or str(node.get("parttype") or "").lower() in SYSTEM_PARTITIONS,
        "volume_detected": filesystem is not None,
    }


def parse_lsblk(payload: dict[str, Any]) -> Section:
    drives = []
    for disk in as_list(payload.get("blockdevices")):
        if not isinstance(disk, dict) or disk.get("type") != "disk":
            continue
        device = disk.get("path") or f"/dev/{disk.get('name', '?')}"
        descendants = _descendants(disk)
        partitions = [_partition(node) for node in descendants]
        # A whole disk can directly contain a filesystem without a partition table.
        if disk.get("fstype") or any(as_list(disk.get("mountpoints", disk.get("mountpoint")))):
            partitions.insert(0, _partition(disk))
        rotation = _boolean(disk.get("rota"))
        drives.append(
            {
                "id": device,
                "device": device,
                "name": disk.get("name") or device,
                "model": str(disk.get("model") or "Unknown").strip(),
                "size": integer(disk.get("size")),
                "interface": disk.get("tran") or "Unknown",
                "type": "HDD"
                if rotation is True
                else ("SSD / non-rotating" if rotation is False else "Unknown"),
                "read_only": _boolean(disk.get("ro")),
                "offline": None,
                "partitions": partitions,
                "partitions_known": True,
                "health": {"available": False, "status": "Health unavailable", "source": None},
            }
        )
    return Section(data={"drives": drives, "platform": "Linux", "inventory_complete": True})


class LinuxProvider:
    def __init__(
        self,
        runner: Runner = run_command,
        sysfs_root: Path | None = None,
        proc_root: Path | None = None,
    ):
        self.runner = runner
        self.sysfs = sysfs_root or Path("/sys")
        self.proc = proc_root or Path("/proc")

    def collect_system(self) -> Section:
        cpuinfo = read_text(self.proc / "cpuinfo") or ""
        fields = dict(re.findall(r"^([^:\n]+)\s*:\s*(.+)$", cpuinfo, re.MULTILINE))
        fields = {key.strip(): value.strip() for key, value in fields.items()}
        return Section(
            data={
                "os": platform.system(),
                "version": platform.release(),
                "architecture": platform.machine(),
                "hostname": platform.node(),
                "cpu_model": fields.get("model name")
                or fields.get("Hardware")
                or platform.processor()
                or None,
                "cpu_manufacturer": fields.get("vendor_id"),
                "memory_installed_bytes": None,
            },
            unavailable=["Installed RAM independent of kernel reserved memory"],
        )

    def collect_storage(self) -> Section:
        result = self.runner(LSBLK_ARGS, timeout=5.0)
        payload = json_object(result)
        if not result.ok or payload is None or not isinstance(payload.get("blockdevices"), list):
            return Section(
                data={"drives": [], "platform": "Linux"},
                unavailable=["Physical drive inventory"],
                error=result.error or "lsblk inventory failed or returned unreadable JSON",
            )
        section = parse_lsblk(payload)
        for drive in section.data["drives"]:
            name = Path(drive["device"]).name
            serial = read_text(self.sysfs / "class/block" / name / "device/serial") or read_text(
                self.sysfs / "class/block" / name / "wwid"
            )
            if serial:
                drive["identity"] = hashlib.sha256(
                    (str(drive.get("model")) + ":" + serial).encode()
                ).hexdigest()
                drive["identity_quality"] = "hashed OS hardware identity"
        bridges = []
        try:
            interfaces = list((self.sysfs / "bus/usb/devices").glob("*:*.?"))
            for interface in interfaces:
                if read_text(interface / "bInterfaceClass") != "08":
                    continue
                block_state = usb_has_block_device(interface)
                bridges.append(
                    {
                        "id": interface.name,
                        "mass_storage_interface": True,
                        "has_block_device": block_state,
                        "source": "sysfs USB interface class 08 + descendant block device directories",
                    }
                )
                if block_state is None:
                    section.unavailable.append(
                        "A USB interface's block-device descendants could not be inspected"
                    )
        except OSError:
            section.unavailable.append("USB interface inventory")
        section.data["usb_storage_bridges"] = bridges
        return section

    def collect_gpu(self) -> Section:
        devices, nvidia_error = nvidia_devices(self.runner)
        try:
            cards = sorted((self.sysfs / "class/drm").glob("card[0-9]*"))
        except OSError:
            cards = []
        pci_names: dict[str, str] = {}
        if any(re.fullmatch(r"card\d+", card.name) for card in cards):
            pci_result = self.runner(["lspci", "-D", "-mm"], timeout=2.0)
            if pci_result.ok:
                pci_names = parse_pci_names(pci_result.stdout)
        for card in cards:
            if not re.fullmatch(r"card\d+", card.name):
                continue
            device = card / "device"
            vendor_id = read_text(device / "vendor")
            vendor = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}.get(
                vendor_id or "", "Unknown"
            )
            try:
                bus_id = device.resolve().name
                driver = (
                    (device / "driver").resolve().name if (device / "driver").exists() else None
                )
            except OSError:
                bus_id, driver = card.name, None
            # PCI domains in nvidia-smi are often eight hex digits, sysfs four.
            if any(str(gpu["id"]).lower().endswith(bus_id.lower()) for gpu in devices):
                continue
            pci_id = read_text(device / "device") or "unknown device"
            name = (
                read_text(device / "product_name")
                or pci_names.get(bus_id.lower())
                or f"{vendor} GPU ({pci_id})"
            )
            gpu = empty_gpu(bus_id, name, vendor, driver)
            utilization = number(read_text(device / "gpu_busy_percent"))
            if utilization is not None and 0 <= utilization <= 100:
                gpu["utilization_percent"] = utilization
            for key, filename in (
                ("memory_used_bytes", "mem_info_vram_used"),
                ("memory_total_bytes", "mem_info_vram_total"),
            ):
                value = integer(read_text(device / filename))
                gpu[key] = value if value is not None and value >= 0 else None
            # amdgpu pp_dpm_sclk marks the active clock with an asterisk.
            clocks = read_text(device / "pp_dpm_sclk") or ""
            clock = re.search(r"(\d+)Mhz\s+\*", clocks, re.IGNORECASE)
            gpu["clock_mhz"] = (
                number(clock.group(1)) if clock else number(read_text(card / "gt_cur_freq_mhz"))
            )
            try:
                hwmons = list((device / "hwmon").glob("hwmon*"))
            except OSError:
                hwmons = []
            for hwmon in hwmons:
                temperature = number(read_text(hwmon / "temp1_input"))
                power = number(read_text(hwmon / "power1_average"))
                if temperature is not None:
                    gpu["temperature_c"] = temperature / 1000
                if power is not None:
                    gpu["power_w"] = power / 1_000_000
            gpu["unavailable"] = [key for key in GPU_METRICS if gpu[key] is None]
            devices.append(gpu)
        return Section(
            data={"items": devices},
            unavailable=sorted({metric for gpu in devices for metric in gpu["unavailable"]})
            if devices
            else ["GPU inventory", *GPU_METRICS],
            error=nvidia_error if not devices else None,
        )

    def collect_battery_metadata(self) -> dict[str, Any]:
        batteries = []
        try:
            supplies = sorted((self.sysfs / "class/power_supply").glob("*"))
        except OSError:
            supplies = []
        for supply in supplies:
            if read_text(supply / "type") != "Battery" or read_text(supply / "scope") == "Device":
                continue
            design_energy, full_energy = (
                number(read_text(supply / filename))
                for filename in ("energy_full_design", "energy_full")
            )
            design_charge, full_charge = (
                number(read_text(supply / filename))
                for filename in ("charge_full_design", "charge_full")
            )
            voltage = number(read_text(supply / "voltage_min_design"))
            design = (
                design_energy / 1000 if design_energy is not None and design_energy > 0 else None
            )
            full = full_energy / 1000 if full_energy is not None and full_energy > 0 else None
            source = "sysfs energy_full_design/energy_full (uWh)"
            # Capacity conversion requires design voltage; live voltage varies with charge.
            if design is None and full is None and voltage is not None and voltage > 0:
                design = (
                    design_charge * voltage / 1_000_000_000
                    if design_charge is not None and design_charge > 0
                    else None
                )
                full = (
                    full_charge * voltage / 1_000_000_000
                    if full_charge is not None and full_charge > 0
                    else None
                )
                source = "sysfs charge capacity x design voltage (estimated mWh)"
            wear = wear_percent(design, full)
            if wear is None:
                wear = wear_percent(design_charge, full_charge)
            cycle = integer(read_text(supply / "cycle_count"))
            temperature = number(read_text(supply / "temp"))
            batteries.append(
                {
                    "id": supply.name,
                    "design_capacity_mwh": design,
                    "full_capacity_mwh": full,
                    "wear_percent": wear,
                    "cycle_count": cycle if cycle is not None and cycle >= 0 else None,
                    "temperature_c": temperature / 10 if temperature is not None else None,
                    "source": source,
                }
            )
        return {
            "batteries": batteries,
            "source": "Linux power_supply sysfs",
            "unavailable": ["Power profile"],
        }
