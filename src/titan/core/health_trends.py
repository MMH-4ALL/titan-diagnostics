"""SMART counter changes; optional persistence contains hashed identity and metrics only."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any

from titan.core.models import Section
from titan.core.monitoring import drive_identity, finite

COUNTERS = {
    "reallocated_sectors": 1,
    "pending_sectors": 1,
    "uncorrectable_sectors": 1,
    "media_errors": 1,
    "percentage_used": 1,
    "available_spare": -1,
    "grown_defects": 1,
}


class HealthTrendError(RuntimeError):
    """The local health store could not be used; its contents are preserved."""


class HealthTrends:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.points: deque[dict[str, Any]] = deque(maxlen=4096)
        self.alerts: list[dict[str, Any]] = []
        if path and path.exists():
            try:
                with closing(
                    sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
                ) as db:
                    for at, identity, raw in reversed(
                        db.execute(
                            "SELECT at,identity,metrics FROM health ORDER BY at DESC LIMIT 4096"
                        ).fetchall()
                    ):
                        if (
                            not isinstance(raw, str)
                            or len(raw) > 8192
                            or finite(at) is None
                            or not isinstance(identity, str)
                            or len(identity) > 256
                        ):
                            raise ValueError("Invalid health record")
                        metrics = json.loads(raw)
                        if not isinstance(metrics, dict) or any(
                            key not in COUNTERS or finite(value) is None or value < 0
                            for key, value in metrics.items()
                        ):
                            raise ValueError("Invalid health counters")
                        self.points.append(
                            {"timestamp": at, "identity": identity, "metrics": metrics}
                        )
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                raise HealthTrendError(
                    f"Local SMART history unavailable ({type(exc).__name__}); existing data was preserved."
                ) from exc

    def record(
        self, section: Section, timestamp: float | None = None, *, persist: bool = False
    ) -> list[dict[str, Any]]:
        timestamp = time.time() if timestamp is None else timestamp
        alerts = []
        records = []
        for drive in section.data.get("drives", []):
            health = drive.get("health") or {}
            if not health.get("available"):
                continue
            metrics = {key: finite(health.get(key)) for key in COUNTERS}
            metrics = {
                key: value for key, value in metrics.items() if value is not None and value >= 0
            }
            if not metrics:
                continue
            identity = drive_identity(drive)
            before = next(
                (
                    point
                    for point in reversed(self.points)
                    if point["identity"] == identity and point["timestamp"] < timestamp
                ),
                None,
            )
            if before:
                for key, value in metrics.items():
                    old = before["metrics"].get(key)
                    if old is None:
                        continue
                    change = value - old
                    if change * COUNTERS[key] > 0:
                        alerts.append(
                            {
                                "identity": identity,
                                "device": drive.get("device"),
                                "metric": key,
                                "before": old,
                                "after": value,
                                "change": change,
                                "from_timestamp": before["timestamp"],
                                "timestamp": timestamp,
                                "identity_quality": drive.get(
                                    "identity_quality", "location/model/size"
                                ),
                                "note": "Compare the same drive. Increasing error counts or wear deserve review; wear changes can be expected with use.",
                            }
                        )
            point = {"timestamp": timestamp, "identity": identity, "metrics": metrics}
            self.points.append(point)
            records.append(point)
        self.alerts = alerts
        if persist and self.path and records:
            try:
                self._persist(records, timestamp)
            except (sqlite3.Error, OSError) as exc:
                raise HealthTrendError(
                    f"SMART history write failed ({type(exc).__name__}); session measurements remain available."
                ) from exc
        return alerts

    def _persist(self, records: list[dict[str, Any]], timestamp: float) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS health (at REAL, identity TEXT, metrics TEXT)")
            db.executemany(
                "INSERT INTO health VALUES (?,?,?)",
                [(p["timestamp"], p["identity"], json.dumps(p["metrics"])) for p in records],
            )
            db.execute("DELETE FROM health WHERE at < ?", (timestamp - 365 * 86400,))
            db.execute(
                "DELETE FROM health WHERE rowid NOT IN (SELECT rowid FROM health ORDER BY at DESC LIMIT 4096)"
            )
