"""Portable fixtures exercise Linux/Windows hardware semantics without hardware."""

import json
import subprocess
from pathlib import Path

import pytest

from titan.core.models import Section
from titan.hardware.battery import wear_percent
from titan.hardware.gpu import parse_nvidia_csv
from titan.hardware.storage import add_smart_health, parse_smart_json, storage_findings
from titan.platforms.base import UnsupportedProvider, get_provider
from titan.platforms.linux import LinuxProvider, parse_lsblk, parse_pci_names, usb_has_block_device
from titan.platforms.windows import WindowsProvider, parse_windows_storage
from titan.utils.commands import CommandResult, json_object, number, run_command


def test_timeout_is_reported_without_partial_measurements(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 0.1
        raise subprocess.TimeoutExpired(args[0], 0.1, output="untrusted partial")

    monkeypatch.setattr(subprocess, "run", timeout)
    result = run_command(["fixed-tool"], timeout=0.1)
    assert "timed out" in result.error
    assert not result.ok and not result.stdout


@pytest.mark.parametrize(
    "exception,expected",
    [
        (FileNotFoundError(), "not installed"),
        (PermissionError(), "Permission denied"),
        (OSError(), "could not be started"),
    ],
)
def test_optional_tool_start_failures(monkeypatch, exception, expected):
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(subprocess, "run", fail)
    assert expected in run_command(["optional-tool"]).error


@pytest.mark.parametrize("value", [None, "N/A", "[Not Supported]", "NaN", "inf", True, {}])
def test_missing_numbers_are_not_zero(value):
    assert number(value) is None


def test_nvidia_uses_csv_parser_and_keeps_missing_metrics():
    devices = parse_nvidia_csv(
        '0,"NVIDIA GPU, special",00000000:01:00.0,555,35,1024,8192,61,N/A,[Not Supported]\nmalformed'
    )
    assert len(devices) == 1
    assert devices[0]["name"] == "NVIDIA GPU, special"
    assert devices[0]["memory_used_bytes"] == 1024**3
    assert devices[0]["utilization_percent"] == 35
    assert devices[0]["clock_mhz"] is None
    assert set(devices[0]["unavailable"]) == {"clock_mhz", "power_w"}


def test_linux_machine_readable_pci_gpu_names():
    names = parse_pci_names(
        '0000:02:00.0 -r01 "VGA compatible controller" "Advanced Micro Devices, Inc." -p00 "Navi 33 [Radeon RX 7600]"\ninvalid "unfinished'
    )
    assert names == {"0000:02:00.0": "Advanced Micro Devices, Inc. Navi 33 [Radeon RX 7600]"}


def test_linux_partitions_remain_separate_and_lvm_descendants_mount():
    section = parse_lsblk(
        {
            "blockdevices": [
                {
                    "name": "sda",
                    "path": "/dev/sda",
                    "type": "disk",
                    "size": 1_000_000,
                    "rota": True,
                    "ro": False,
                    "tran": "usb",
                    "children": [
                        {
                            "name": "sda1",
                            "type": "part",
                            "fstype": "LVM2_member",
                            "children": [
                                {
                                    "name": "vg-root",
                                    "type": "lvm",
                                    "fstype": "ext4",
                                    "mountpoints": ["/"],
                                }
                            ],
                        }
                    ],
                },
                {"name": "loop0", "type": "loop", "size": 200},
            ]
        }
    )
    assert len(section.data["drives"]) == 1
    drive = section.data["drives"][0]
    assert drive["size"] == 1_000_000 and drive["type"] == "HDD"
    assert len(drive["partitions"]) == 2
    assert not storage_findings(section)


def test_windows_ignores_recovery_partition_and_preserves_dynamic_cim_disk():
    section = parse_windows_storage(
        {
            "disks": [{"Number": 0, "BusType": 17, "Size": 1000, "IsReadOnly": False}],
            "cim": [
                {"Index": 0, "Model": "NVMe"},
                {"Index": 1, "Model": "Dynamic disk", "Size": 2000},
            ],
            "partitions": [
                {
                    "DiskNumber": 0,
                    "PartitionNumber": 1,
                    "Type": "Recovery",
                    "DriveLetter": "\x00",
                    "Volume": {"FileSystem": "NTFS"},
                }
            ],
        }
    )
    assert len(section.data["drives"]) == 2
    assert section.data["drives"][0]["interface"] == "NVMe"
    assert section.data["drives"][1]["partitions_known"] is False
    assert not storage_findings(section)


def test_windows_singletons_and_directory_mount_no_drive_letter():
    section = parse_windows_storage(
        {
            "disks": {"Number": 2, "BusType": 7, "FriendlyName": "External"},
            "partitions": {
                "DiskNumber": 2,
                "PartitionNumber": 1,
                "DriveLetter": "",
                "AccessPaths": ["\\\\?\\Volume{guid}\\", "C:\\mount\\"],
                "Volume": {"FileSystem": "NTFS"},
            },
        }
    )
    drive = section.data["drives"][0]
    assert drive["partitions"][0]["mountpoint"] == "C:\\mount\\"
    assert not storage_findings(section)


def test_detected_raw_and_readonly_is_evidence_not_automatic_repair():
    section = parse_windows_storage(
        {
            "disks": [{"Number": 2, "FriendlyName": "External", "IsReadOnly": True}],
            "partitions": [
                {"DiskNumber": 2, "PartitionNumber": 1, "Volume": {"FileSystem": "RAW"}}
            ],
        }
    )
    findings = storage_findings(section)
    assert {finding.severity for finding in findings} == {"attention", "warning"}
    assert any("read-only" in finding.title for finding in findings)
    assert any("RAW" in finding.title for finding in findings)
    assert all("Do not initialize or format" in finding.next_step for finding in findings)


def test_smart_health_bits_do_not_discard_valid_json():
    payload = {
        "smartctl": {"exit_status": 8},
        "smart_status": {"passed": False},
        "temperature": {"current": 35},
    }
    assert json_object(CommandResult(json.dumps(payload), returncode=8)) == payload
    health = parse_smart_json(payload, 8)
    assert health["available"] and health["reported_passed"] is False
    section = Section(data={"drives": [{"id": "d", "health": health}]})
    assert storage_findings(section)[0].severity == "critical"


def test_unsupported_smart_is_not_healthy_and_partial_data_is_labeled():
    health = parse_smart_json({"smartctl": {"exit_status": 2}}, 2)
    assert not health["available"] and health["status"] == "Health unavailable"
    health = parse_smart_json({"smartctl": {"exit_status": 4}, "smart_status": {"passed": True}}, 4)
    assert health["query_incomplete"] and health["reported_passed"] is True


def test_nvme_endurance_and_ata_raw_fields_are_not_interchangeable():
    nvme = parse_smart_json(
        {
            "nvme_smart_health_information_log": {
                "media_errors": 2,
                "percentage_used": 102,
                "available_spare": 4,
                "available_spare_threshold": 10,
                "critical_warning": 0,
            }
        }
    )
    assert nvme["protocol"] == "NVMe"
    findings = storage_findings(Section(data={"drives": [{"id": "nvme", "health": nvme}]}))
    assert len(findings) == 3
    ata = parse_smart_json(
        {
            "ata_smart_attributes": {
                "table": [
                    {"id": 5, "name": "Retired_Block_Count", "raw": {"value": 9}},
                    {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 2}},
                ]
            }
        }
    )
    assert "reallocated_sectors" not in ata
    assert ata["pending_sectors"] == 2


def test_smart_only_queries_enumerated_paths_and_safe_types():
    calls = []

    def runner(args, timeout):
        calls.append(args)
        if "--scan" in args:
            return CommandResult(
                json.dumps(
                    {
                        "devices": [
                            {"name": "/dev/sda", "type": "sat"},
                            {"name": "/dev/sdz", "type": "ata"},
                        ]
                    }
                ),
                returncode=0,
            )
        return CommandResult('{"smart_status":{"passed":true}}', returncode=0)

    section = Section(
        data={
            "platform": "Linux",
            "drives": [{"device": "/dev/sda"}, {"device": "--format /dev/sdb"}],
        }
    )
    add_smart_health(section, runner=runner)
    assert len(calls) == 2 and calls[-1][-1] == "/dev/sda"
    assert "sat" in calls[-1]
    assert all("-t" not in call and "-s" not in call for call in calls)
    assert section.data["drives"][1]["health"]["available"] is False


def test_windows_smart_scanned_sda_matches_enumerated_pd0():
    calls = []

    def runner(args, timeout):
        calls.append(args)
        return CommandResult(
            json.dumps(
                {"devices": [{"name": "/dev/sda", "type": "sat"}]}
                if "--scan" in args
                else {"smart_status": {"passed": True}}
            ),
            returncode=0,
        )

    add_smart_health(
        Section(data={"platform": "Windows", "drives": [{"device": "/dev/pd0"}]}), runner
    )
    assert calls[-1][-3:] == ["-d", "sat", "/dev/pd0"]


def test_smart_budget_limits_queries(monkeypatch):
    current = [0.0]
    monkeypatch.setattr("titan.hardware.storage.time.monotonic", lambda: current[0])
    calls = []

    def runner(args, timeout):
        calls.append(args)
        current[0] += 2.0
        return CommandResult("{}", returncode=0)

    section = Section(data={"drives": [{"device": "/dev/sda"}, {"device": "/dev/sdb"}]})
    add_smart_health(section, runner, budget=3)
    assert len(calls) == 2
    assert any("budget" in reason for reason in section.unavailable)


def test_missing_gpu_and_disk_tools_are_contained():
    def missing(args, timeout):
        return CommandResult(error="missing tool")

    assert LinuxProvider(missing, Path("/nonexistent")).collect_storage().error
    assert LinuxProvider(missing, Path("/nonexistent")).collect_gpu().data["items"] == []
    assert WindowsProvider(missing).collect_gpu().data["items"] == []
    assert WindowsProvider(missing).collect_battery_metadata()["batteries"] == []


def test_windows_cpu_registry_fallback(monkeypatch):
    monkeypatch.setattr(
        "titan.platforms.windows._registry_cpu",
        lambda: {"Name": "Fixture CPU", "Manufacturer": "Fixture Vendor"},
    )

    def runner(args, timeout):
        return CommandResult('{"unavailable":["CPU model","OS details"]}', returncode=0)

    section = WindowsProvider(runner).collect_system()
    assert section.data["cpu_model"] == "Fixture CPU"
    assert section.unavailable == ["OS details"]


def test_nvidia_success_does_not_hide_failed_other_gpu_inventory():
    def runner(args, timeout):
        if args[0] == "nvidia-smi":
            return CommandResult(
                "0,NVIDIA GPU,00000000:01:00.0,555,35,1024,8192,61,700,8", returncode=0
            )
        return CommandResult(error="Access denied")

    section = WindowsProvider(runner).collect_gpu()
    assert len(section.data["items"]) == 1
    assert section.error is None
    assert any("AMD/Intel" in reason for reason in section.unavailable)


def test_linux_capacity_unit_conversion_and_charge_only_wear(tmp_path):
    battery = tmp_path / "class/power_supply/BAT0"
    battery.mkdir(parents=True)
    for key, value in {
        "type": "Battery",
        "charge_full_design": "5000000",
        "charge_full": "4000000",
        "cycle_count": "7",
    }.items():
        (battery / key).write_text(value)
    metadata = LinuxProvider(sysfs_root=tmp_path).collect_battery_metadata()
    result = metadata["batteries"][0]
    assert result["design_capacity_mwh"] is None
    assert result["wear_percent"] == 20 and result["cycle_count"] == 7
    (battery / "voltage_min_design").write_text("12000000")
    result = LinuxProvider(sysfs_root=tmp_path).collect_battery_metadata()["batteries"][0]
    assert result["design_capacity_mwh"] == 60000
    assert result["full_capacity_mwh"] == 48000


@pytest.mark.parametrize(
    "design,full,expected",
    [(100, 80, 20), (100, 101, 0), (None, 80, None), (0, 80, None), (100, 0, None)],
)
def test_capacity_wear_requires_reliable_values(design, full, expected):
    assert wear_percent(design, full) == expected


def test_platform_factory_explicit_and_unknown():
    assert isinstance(get_provider("Windows"), WindowsProvider)
    assert isinstance(get_provider("Linux"), LinuxProvider)
    assert isinstance(get_provider("Other"), UnsupportedProvider)


def test_usb_interface_no_media_finding_preserves_uncertainty():
    section = Section(
        data={
            "drives": [],
            "usb_storage_bridges": [
                {
                    "id": "1-1:1.0",
                    "mass_storage_interface": True,
                    "has_block_device": False,
                    "source": "sysfs",
                }
            ],
        }
    )
    finding = storage_findings(section)[0]
    assert "empty card reader" in finding.explanation
    assert "cannot be distinguished" in finding.explanation


def test_usb_block_inventory_requires_accessible_evidence(monkeypatch, tmp_path):
    interface = tmp_path / "usb-interface"
    interface.mkdir()
    assert usb_has_block_device(interface) is False
    (interface / "host/target/block/sda").mkdir(parents=True)
    assert usb_has_block_device(interface) is True

    def denied_walk(root, onerror):
        onerror(PermissionError())
        return iter([])

    monkeypatch.setattr("titan.platforms.linux.os.walk", denied_walk)
    assert usb_has_block_device(interface) is None
    finding = storage_findings(
        Section(
            data={
                "usb_storage_bridges": [{"mass_storage_interface": True, "has_block_device": None}]
            }
        )
    )
    assert not finding


def test_missing_lsblk_schema_is_an_error():
    def runner(args, timeout):
        return CommandResult("{}", returncode=0)

    assert LinuxProvider(runner).collect_storage().error


def test_inventory_error_does_not_claim_disk_missing():
    finding = storage_findings(Section(error="permission denied"))[0]
    assert finding.severity == "informational" and "not proof" in finding.explanation
