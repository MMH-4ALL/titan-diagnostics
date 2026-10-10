import pytest

from titan.hardware.storage import storage_findings
from titan.platforms.windows import parse_windows_storage


@pytest.mark.parametrize(
    "guid",
    [
        "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
        "e3c9e316-0b5c-4db8-817d-f92df00215ae",
        "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
        "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}",
    ],
)
def test_reserved_efi_and_recovery_guid_without_braces_is_not_missing_mount(guid):
    section = parse_windows_storage(
        {
            "disks": [{"Number": 0, "FriendlyName": "Test drive", "Size": 1000}],
            "partitions": [
                {
                    "DiskNumber": 0,
                    "PartitionNumber": 1,
                    "GptType": guid,
                    "Type": "Unknown",
                    "Volume": None,
                    "DriveLetter": "",
                    "AccessPaths": [],
                }
            ],
        }
    )
    assert section.data["drives"][0]["partitions"][0]["system_partition"]
    assert not any("unmounted" in finding.id for finding in storage_findings(section))
