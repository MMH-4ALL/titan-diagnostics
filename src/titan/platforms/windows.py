"""Read-only CIM/Storage inventory on Windows; scripts are fixed constants."""

from __future__ import annotations

import hashlib
import platform
import re
from typing import Any

from titan.core.models import Section
from titan.hardware.battery import wear_percent
from titan.hardware.gpu import GPU_METRICS, Runner, empty_gpu, nvidia_devices
from titan.utils.commands import as_list, integer, json_object, number, run_command

PS_HEADER = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);$ErrorActionPreference='Stop';"
SYSTEM_SCRIPT = (
    PS_HEADER
    + """
$errors=@();$cpu=$null;$os=$null;$sys=$null
try {$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1 Name,Manufacturer} catch {$errors+='CPU model'}
try {$os=Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber} catch {$errors+='OS details'}
try {$sys=Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory} catch {$errors+='Installed RAM'}
@{cpu=$cpu;os=$os;system=$sys;unavailable=$errors}|ConvertTo-Json -Depth 5 -Compress
"""
)
STORAGE_SCRIPT = (
    PS_HEADER
    + """
$errors=@();$disks=@();$cim=@();$parts=@()
try {$cim=@(Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,Size,InterfaceType,DeviceID,SerialNumber)} catch {$errors+='CIM physical drives'}
try {$disks=@(Get-Disk | Select-Object Number,FriendlyName,Size,BusType,IsReadOnly,IsOffline,PartitionStyle,HealthStatus,UniqueId,SerialNumber)} catch {$errors+='Storage disk inventory'}
try {
  $parts=@(Get-Partition | ForEach-Object {
    $p=$_;$v=$null
    try {$v=$p|Get-Volume -ErrorAction Stop|Select-Object -First 1 FileSystem,Size,SizeRemaining} catch {}
    [PSCustomObject]@{DiskNumber=$p.DiskNumber;PartitionNumber=$p.PartitionNumber;DriveLetter=[string]$p.DriveLetter;AccessPaths=@($p.AccessPaths);Type=$p.Type;GptType=[string]$p.GptType;Size=$p.Size;Volume=$v}
  })
} catch {$errors+='Partition and volume inventory'}
@{disks=$disks;cim=$cim;partitions=$parts;unavailable=$errors}|ConvertTo-Json -Depth 8 -Compress
"""
)
GPU_SCRIPT = (
    PS_HEADER
    + """
@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,DriverVersion,PNPDeviceID)|ConvertTo-Json -Depth 4 -Compress
"""
)
BATTERY_SCRIPT = (
    PS_HEADER
    + """
$errors=@();$static=@();$full=@();$cycles=@()
try {$static=@(Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData|Select-Object InstanceName,DesignedCapacity)} catch {$errors+='Battery design capacity'}
try {$full=@(Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity|Select-Object InstanceName,FullChargedCapacity)} catch {$errors+='Battery full charge capacity'}
try {$cycles=@(Get-CimInstance -Namespace root/wmi -ClassName BatteryCycleCount|Select-Object InstanceName,CycleCount)} catch {$errors+='Battery cycle count'}
@{static=$static;full=$full;cycles=$cycles;unavailable=$errors}|ConvertTo-Json -Depth 5 -Compress
"""
)
BUS_NAMES = {
    0: "Unknown",
    1: "SCSI",
    2: "ATAPI",
    3: "ATA",
    4: "IEEE 1394",
    5: "SSA",
    6: "Fibre Channel",
    7: "USB",
    8: "RAID",
    9: "iSCSI",
    10: "SAS",
    11: "SATA",
    12: "SD",
    13: "MMC",
    14: "Virtual",
    15: "File backed virtual",
    16: "Storage Spaces",
    17: "NVMe",
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _registry_cpu() -> dict[str, str]:
    """HKLM processor metadata is a read-only fallback when CIM is restricted."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            0,
            winreg.KEY_READ,
        ) as key:
            return {
                "Name": str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip(),
                "Manufacturer": str(winreg.QueryValueEx(key, "VendorIdentifier")[0]).strip(),
            }
    except (ImportError, OSError):
        return {}


def parse_windows_storage(payload: dict[str, Any]) -> Section:
    """Merge basic disks with CIM inventory, which also sees dynamic disks.

    No drive is classified as failed because a cmdlet omitted its information.
    Hidden recovery, reserved and EFI partitions normally have no drive letter.
    """
    basic: dict[int, dict[str, Any]] = {}
    cim: dict[int, dict[str, Any]] = {}
    for source, key, destination in (("disks", "Number", basic), ("cim", "Index", cim)):
        for item in as_list(payload.get(source)):
            if isinstance(item, dict):
                identity = integer(item.get(key))
                if identity is not None:
                    destination[identity] = item
    partitions = [item for item in as_list(payload.get("partitions")) if isinstance(item, dict)]
    drives = []
    for identity in sorted(set(basic) | set(cim)):
        disk, fallback = basic.get(identity, {}), cim.get(identity, {})
        bus_raw = disk.get("BusType")
        bus_int = integer(bus_raw)
        interface = (
            BUS_NAMES.get(bus_int, str(bus_raw))
            if bus_int is not None
            else (str(bus_raw) if bus_raw is not None else fallback.get("InterfaceType"))
        )
        partition_list = []
        for part in partitions:
            if integer(part.get("DiskNumber")) != identity:
                continue
            volume = _mapping(part.get("Volume"))
            letter = str(part.get("DriveLetter") or "").strip("\x00 ") or None
            paths = [
                path
                for path in as_list(part.get("AccessPaths"))
                if isinstance(path, str) and not path.startswith("\\\\?\\Volume")
            ]
            mountpoint = paths[0] if paths else (f"{letter}:\\" if letter else None)
            gpt = str(part.get("GptType") or "").lower().strip("{}")
            kind = str(part.get("Type") or "")
            system_partition = kind.lower() in {"system", "reserved", "recovery"} or gpt in {
                "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                "e3c9e316-0b5c-4db8-817d-f92df00215ae",
                "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
            }
            partition_list.append(
                {
                    "device": f"Disk {identity} / partition {part.get('PartitionNumber', '?')}",
                    "mountpoint": mountpoint,
                    "filesystem": volume.get("FileSystem") or None,
                    "letter": letter,
                    "size": integer(part.get("Size")),
                    "available": integer(volume.get("SizeRemaining")),
                    "system_partition": system_partition,
                    "kind": kind,
                    "volume_detected": bool(volume),
                }
            )
        name = disk.get("FriendlyName") or fallback.get("Model") or f"Physical drive {identity}"
        unique = disk.get("UniqueId") or disk.get("SerialNumber") or fallback.get("SerialNumber")
        drives.append(
            {
                "id": f"PhysicalDrive{identity}",
                "name": name,
                "model": fallback.get("Model") or name,
                "device": f"/dev/pd{identity}",
                "identity": hashlib.sha256(
                    str(unique or f"{identity}:{name}:{disk.get('Size') or fallback.get('Size')}")
                    .strip()
                    .encode()
                ).hexdigest(),
                "identity_quality": "hashed OS hardware identity"
                if unique
                else "location/model/size; replacement may be indistinguishable",
                "size": integer(disk.get("Size") or fallback.get("Size")),
                "interface": interface or "Unknown",
                "type": "SSD" if str(interface).lower() == "nvme" else "Unknown",
                "read_only": disk.get("IsReadOnly"),
                "offline": disk.get("IsOffline"),
                "partition_style": str(disk.get("PartitionStyle"))
                if disk.get("PartitionStyle") is not None
                else None,
                "partitions": partition_list,
                "partitions_known": "Partition and volume inventory"
                not in as_list(payload.get("unavailable"))
                and identity in basic,
                "os_health": disk.get("HealthStatus"),
                "health": {"available": False, "status": "Health unavailable", "source": None},
            }
        )
    unavailable = [str(item) for item in as_list(payload.get("unavailable"))]
    complete = not any(
        "drives" in item.lower() or "disk inventory" in item.lower() for item in unavailable
    )
    unavailable += ["USB bridge-to-disk correlation"]
    error = (
        "Physical drive inventory unavailable"
        if not drives
        and any(
            "drives" in item.lower() or "disk inventory" in item.lower() for item in unavailable
        )
        else None
    )
    return Section(
        data={"drives": drives, "platform": "Windows", "inventory_complete": complete},
        unavailable=unavailable,
        error=error,
    )


class WindowsProvider:
    def __init__(self, runner: Runner = run_command):
        self.runner = runner

    def collect_disk_performance(self, names: list[str]) -> dict[str, Any]:
        from titan.platforms.windows_disk import disk_performance

        return disk_performance(names)

    def collect_disk_queues(self) -> dict[str, float]:
        script = (
            PS_HEADER
            + "@{queues=@(Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk | Select-Object Name,CurrentDiskQueueLength)}|ConvertTo-Json -Compress"
        )
        payload, _ = self._query(script, timeout=3)
        result = {}
        for item in as_list((payload or {}).get("queues")):
            if not isinstance(item, dict):
                continue
            match = re.match(r"^(\d+)(?:\s|$)", str(item.get("Name")))
            value = number(item.get("CurrentDiskQueueLength"))
            if match and value is not None:
                result[f"PhysicalDrive{match[1]}"] = value
        return result

    def _query(self, script: str, timeout: float = 8.0) -> tuple[dict[str, Any] | None, str | None]:
        result = self.runner(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=timeout,
        )
        if not result.ok:
            return None, result.error or "Windows hardware query failed or access was denied"
        payload = json_object(result)
        return (
            payload,
            None if payload is not None else "Windows returned an unreadable hardware response",
        )

    def collect_system(self) -> Section:
        payload, error = self._query(SYSTEM_SCRIPT)
        payload = payload or {}
        cpu, os_data, system = (_mapping(payload.get(key)) for key in ("cpu", "os", "system"))
        if not cpu:
            cpu = _registry_cpu()
        unavailable = [str(item) for item in as_list(payload.get("unavailable"))]
        if cpu.get("Name") and "CPU model" in unavailable:
            unavailable.remove("CPU model")
        return Section(
            data={
                "os": os_data.get("Caption") or platform.system(),
                "version": os_data.get("Version") or platform.version(),
                "architecture": platform.machine(),
                "hostname": platform.node(),
                "cpu_model": cpu.get("Name"),
                "cpu_manufacturer": cpu.get("Manufacturer"),
                "memory_installed_bytes": integer(system.get("TotalPhysicalMemory")),
            },
            unavailable=unavailable,
            error=error,
        )

    def collect_storage(self) -> Section:
        payload, error = self._query(STORAGE_SCRIPT, timeout=12.0)
        if payload is None:
            return Section(
                data={"drives": [], "platform": "Windows"},
                unavailable=["Physical drives", "Partitions"],
                error=error,
            )
        return parse_windows_storage(payload)

    def collect_gpu(self) -> Section:
        # NVIDIA metrics are additive: CIM always enumerates AMD/Intel adapters too.
        result = self.runner(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", GPU_SCRIPT],
            timeout=6.0,
        )
        adapters: list[Any] = []
        if result.ok:
            import json

            try:
                adapters = as_list(json.loads(result.stdout.lstrip("\ufeff")))
            except (ValueError, TypeError):
                pass
        nvidia, nvidia_error = nvidia_devices(self.runner)
        devices = list(nvidia)
        matched: set[int] = set()
        for adapter in adapters:
            if not isinstance(adapter, dict):
                continue
            name = str(adapter.get("Name") or "Unknown GPU")
            vendor = str(adapter.get("AdapterCompatibility") or "Unknown")
            # Match one instance at a time, preserving multiple identical GPUs.
            candidate = next(
                (
                    idx
                    for idx, gpu in enumerate(nvidia)
                    if idx not in matched and gpu["name"].lower() == name.lower()
                ),
                None,
            )
            if candidate is not None:
                matched.add(candidate)
                continue
            devices.append(
                empty_gpu(f"adapter-{len(devices)}", name, vendor, adapter.get("DriverVersion"))
            )
        unavailable = sorted({metric for device in devices for metric in device["unavailable"]})
        if not result.ok or not adapters:
            unavailable.append("CIM GPU inventory: additional AMD/Intel adapters may be unreadable")
        if not devices:
            unavailable = list(GPU_METRICS) + ["GPU inventory"]
        # A missing optional NVIDIA tool is not an error for an AMD/Intel machine.
        error = (
            (result.error or nvidia_error or "GPU inventory unavailable") if not devices else None
        )
        return Section(data={"items": devices}, unavailable=unavailable, error=error)

    def collect_battery_metadata(self) -> dict[str, Any]:
        payload, error = self._query(BATTERY_SCRIPT)
        if payload is None:
            return {
                "batteries": [],
                "unavailable": [error or "Battery capacity metadata"],
                "source": "Windows root/wmi",
            }
        static = {
            _mapping(item).get("InstanceName"): _mapping(item)
            for item in as_list(payload.get("static"))
        }
        full = {
            _mapping(item).get("InstanceName"): _mapping(item)
            for item in as_list(payload.get("full"))
        }
        cycles = {
            _mapping(item).get("InstanceName"): _mapping(item)
            for item in as_list(payload.get("cycles"))
        }
        batteries = []
        for index, identity in enumerate(set(static) | set(full) | set(cycles)):
            if identity is None:
                continue
            design = number(static.get(identity, {}).get("DesignedCapacity"))
            capacity = number(full.get(identity, {}).get("FullChargedCapacity"))
            # WMI sentinel 0xFFFFFFFF means an unknown measurement.
            design = design if design is not None and 0 < design < 0xFFFFFFFF else None
            capacity = capacity if capacity is not None and 0 < capacity < 0xFFFFFFFF else None
            cycle = integer(cycles.get(identity, {}).get("CycleCount"))
            batteries.append(
                {
                    "id": f"battery-{index}",
                    "design_capacity_mwh": design,
                    "full_capacity_mwh": capacity,
                    "wear_percent": wear_percent(design, capacity),
                    "cycle_count": cycle if cycle is not None and 0 <= cycle < 0xFFFFFFFF else None,
                    "temperature_c": None,
                }
            )
        return {
            "batteries": batteries,
            "source": "Windows root/wmi capacity classes (mWh)",
            "unavailable": [str(item) for item in as_list(payload.get("unavailable"))]
            + ["Battery temperature", "Power profile"],
        }
