"""Redacted, atomic before/after snapshots with explicit missing measurements."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from titan.core.models import Snapshot
from titan.core.monitoring import drive_identity, finite
from titan.reports.export import report_payload

METRICS = {
    "cpu": {"percent": "%", "temperature_c": "C", "freq_mhz": "MHz"},
    "memory": {"used": "bytes", "available": "bytes", "percent": "%"},
    "storage": {"read_bps": "bytes/s", "write_bps": "bytes/s"},
    "network": {"download_bps": "bytes/s", "upload_bps": "bytes/s"},
    "battery": {"percent": "%", "wear_percent": "%"},
}


def snapshot_payload(snapshot: Snapshot) -> dict[str, Any]:
    payload = report_payload(snapshot, [])
    payload["kind"] = "titan-comparison-snapshot"
    inventory = {}
    for section, key in (("storage", "drives"), ("gpu", "items"), ("network", "interfaces")):
        value = snapshot.sections.get(section)
        if value and not value.error and isinstance(value.data.get(key), list):
            inventory[section] = [
                drive_identity(item)
                if section == "storage"
                else hashlib.sha256(
                    repr(
                        (
                            item.get("id"),
                            item.get("name"),
                            item.get("pci_address"),
                            item.get("pnp_device_id"),
                        )
                    ).encode()
                ).hexdigest()
                for item in value.data[key]
            ]
    payload["inventory_fingerprints"] = inventory
    return payload


def save_snapshot(snapshot: Snapshot, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot_payload(snapshot), stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return path


def load_snapshot(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Snapshot is too large (limit 16 MiB).")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("kind") != "titan-comparison-snapshot"
        or data.get("schema_version") != 1
    ):
        raise ValueError("Expected a Titan comparison snapshot (schema 1).")
    snap = data.get("snapshot")
    if (
        not isinstance(snap, dict)
        or not isinstance(snap.get("sections"), dict)
        or finite(snap.get("timestamp")) is None
    ):
        raise ValueError("Snapshot data is malformed.")
    for value in snap["sections"].values():
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
            raise ValueError("Snapshot section is malformed.")
    inventory = data.get("inventory_fingerprints", {})
    if not isinstance(inventory, dict) or any(
        not isinstance(values, list)
        or len(values) > 4096
        or any(not isinstance(value, str) or len(value) > 256 for value in values)
        for values in inventory.values()
    ):
        raise ValueError("Snapshot inventory fingerprints are malformed.")
    return data


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    left, right = before["snapshot"], after["snapshot"]
    rows = []
    for section, fields in METRICS.items():
        old = left["sections"].get(section, {})
        new = right["sections"].get(section, {})
        for field, unit in fields.items():
            a = finite(old.get("data", {}).get(field)) if not old.get("error") else None
            b = finite(new.get("data", {}).get(field)) if not new.get("error") else None
            rows.append(
                {
                    "metric": f"{section}.{field}",
                    "unit": unit,
                    "before": a,
                    "after": b,
                    "change": b - a if a is not None and b is not None else None,
                }
            )
    for section, field in (
        ("storage", "drives"),
        ("gpu", "items"),
        ("network", "interfaces"),
        ("processes", "items"),
    ):
        a, b = left["sections"].get(section, {}), right["sections"].get(section, {})
        old, new = a.get("data", {}).get(field), b.get("data", {}).get(field)
        rows.append(
            {
                "metric": f"{section}.observed_count",
                "unit": "items",
                "before": len(old) if isinstance(old, list) and not a.get("error") else None,
                "after": len(new) if isinstance(new, list) and not b.get("error") else None,
                "change": len(new) - len(old)
                if isinstance(old, list)
                and isinstance(new, list)
                and not a.get("error")
                and not b.get("error")
                else None,
            }
        )
    inventory_changes = []
    for kind, identities in before.get("inventory_fingerprints", {}).items():
        others = after.get("inventory_fingerprints", {}).get(kind)
        if isinstance(identities, list) and isinstance(others, list):
            inventory_changes.append(
                {
                    "kind": kind,
                    "added": len(set(others) - set(identities)),
                    "removed": len(set(identities) - set(others)),
                }
            )
    metadata_changes = []
    for section, metadata_fields in (
        ("cpu", ("model", "physical_cores", "logical_cores")),
        ("system", ("os", "version", "architecture")),
    ):
        old = left["sections"].get(section, {}).get("data", {})
        new = right["sections"].get(section, {}).get("data", {})
        for field in metadata_fields:
            if (
                old.get(field) is not None
                and new.get(field) is not None
                and old[field] != new[field]
            ):
                metadata_changes.append(
                    {"field": f"{section}.{field}", "before": old[field], "after": new[field]}
                )
    return {
        "before_timestamp": left["timestamp"],
        "after_timestamp": right["timestamp"],
        "elapsed_seconds": right["timestamp"] - left["timestamp"],
        "measurements": rows,
        "inventory_changes": inventory_changes,
        "metadata_changes": metadata_changes,
        "note": "Point-in-time observations, not a benchmark or proof of causation. Counts include accessible inventory only. Compare the same computer under similar workloads.",
        "platform_changed": left.get("platform") != right.get("platform"),
    }
