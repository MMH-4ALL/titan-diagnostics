"""Bounded session observations. Missing inventory never becomes a disconnect."""

from __future__ import annotations

import hashlib
import math
from collections import deque
from pathlib import Path
from typing import Any

from titan.core.models import Section


def finite(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def drive_identity(drive: dict[str, Any]) -> str:
    # Providers can supply a hash of an OS hardware identity. Raw serials are not retained.
    return str(
        drive.get("identity")
        or hashlib.sha256(
            repr(
                (drive.get("id"), drive.get("device"), drive.get("model"), drive.get("size"))
            ).encode()
        ).hexdigest()
    )


class DriveTimeline:
    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=512)
        self.present: dict[str, dict[str, Any]] | None = None
        self.seen: set[str] = set()
        self.last_observation: float | None = None

    def observe(self, section: Section, timestamp: float) -> None:
        observed = section.data.get("inventory_observed_at", timestamp)
        if (
            observed == self.last_observation
            or section.error
            or not section.data.get("inventory_complete", False)
        ):
            return
        self.last_observation = observed
        current = {drive_identity(d): d for d in section.data.get("drives", [])}
        previous = self.present or {}
        for identity in previous.keys() - current.keys():
            self._event(timestamp, "disconnected", previous[identity])
        for identity in current.keys() - previous.keys():
            kind = (
                "present at start"
                if self.present is None
                else "reconnected"
                if identity in self.seen
                else "connected"
            )
            self._event(timestamp, kind, current[identity])
        self.seen.update(current)
        # Bound identities as well as event records.
        if len(self.seen) > 2048:
            self.seen = set(current) | {str(e["identity"]) for e in self.events}
        self.present = current

    def _event(self, timestamp: float, kind: str, drive: dict[str, Any]) -> None:
        self.events.append(
            {
                "timestamp": timestamp,
                "event": kind,
                "identity": drive_identity(drive),
                "device": drive.get("device"),
                "model": drive.get("model"),
                "identity_quality": drive.get(
                    "identity_quality", "location/model/size; replacement may be indistinguishable"
                ),
            }
        )


class DiskSampler:
    def __init__(self) -> None:
        self.previous: dict[str, tuple[float, dict[str, float | None]]] = {}
        self.identities: dict[str, str] = {}

    def sample(
        self,
        counters: dict[str, Any],
        now: float,
        *,
        queues: dict[str, Any] | None = None,
        sysfs: Path | None = None,
        identities: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if identities is not None:
            for name, identity in identities.items():
                if self.identities.get(name) != identity:
                    self.previous.pop(name, None)
            self.identities = identities
        current = {}
        result = []
        for name, counter in list(counters.items())[:512]:
            values = {
                key: finite(getattr(counter, key, None))
                for key in (
                    "read_bytes",
                    "write_bytes",
                    "read_count",
                    "write_count",
                    "read_time",
                    "write_time",
                    "busy_time",
                )
            }
            old = self.previous.get(name)
            elapsed = now - old[0] if old else 0

            def delta(
                key: str,
                values: dict[str, float | None] = values,
                old: Any = old,
                elapsed: float = elapsed,
            ) -> float | None:
                value, before = values[key], old[1][key] if old else None
                return (
                    value - before
                    if value is not None and before is not None and elapsed > 0 and value >= before
                    else None
                )

            row: dict[str, Any] = {
                "device": name,
                "interval_seconds": elapsed if elapsed > 0 else None,
            }
            for operation in ("read", "write"):
                count, duration, byte_count = (
                    delta(f"{operation}_count"),
                    delta(f"{operation}_time"),
                    delta(f"{operation}_bytes"),
                )
                row[f"{operation}_bps"] = byte_count / elapsed if byte_count is not None else None
                row[f"{operation}_iops"] = count / elapsed if count is not None else None
                row[f"{operation}_latency_ms"] = (
                    duration / count if duration is not None and count and count > 0 else None
                )
            busy = delta("busy_time")
            row["busy_percent"] = 100 * busy / (elapsed * 1000) if busy is not None else None
            row["queue_depth"] = (queues or {}).get(name)
            if sysfs is not None and "/" not in name and "\\" not in name:
                try:
                    inflight = [
                        int(v)
                        for v in (sysfs / "class/block" / name / "inflight").read_text().split()
                    ]
                    row["queue_depth"] = sum(inflight) if len(inflight) == 2 else None
                except (OSError, ValueError):
                    pass
            result.append(row)
            current[name] = (now, values)
        self.previous = current
        return result
