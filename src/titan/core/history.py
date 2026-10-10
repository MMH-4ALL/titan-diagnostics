"""Opt-in bounded SQLite metric history. No personal identifiers are stored."""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from titan.core.models import Snapshot

METRICS = (
    "cpu_percent",
    "memory_percent",
    "disk_read_bps",
    "disk_write_bps",
    "network_upload_bps",
    "network_download_bps",
    "gpu_percent",
    "cpu_temperature",
    "battery_percent",
)
MAX_QUERY_ROWS = 10_000


class HistoryError(RuntimeError):
    """The history database is inaccessible or damaged."""


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _metrics(snapshot: Snapshot) -> dict[str, float | None]:
    def data(name: str) -> dict[str, Any]:
        section = snapshot.sections.get(name)
        return section.data if section is not None and not section.error else {}

    cpu, memory, storage, network, battery, gpu = (
        data(name) for name in ("cpu", "memory", "storage", "network", "battery", "gpu")
    )
    gpu_values = [
        _number(item.get("utilization_percent", item.get("percent")))
        for item in gpu.get("items", [])
    ]
    valid_gpu = [value for value in gpu_values if value is not None]
    return {
        "cpu_percent": _number(cpu.get("percent")),
        "memory_percent": _number(memory.get("percent")),
        "disk_read_bps": _number(storage.get("read_bps")),
        "disk_write_bps": _number(storage.get("write_bps")),
        "network_upload_bps": _number(network.get("upload_bps")),
        "network_download_bps": _number(network.get("download_bps")),
        "gpu_percent": sum(valid_gpu) / len(valid_gpu) if valid_gpu else None,
        "cpu_temperature": _number(cpu.get("temperature", cpu.get("temperature_c"))),
        "battery_percent": _number(battery.get("percent")),
    }


class HistoryStore:
    def __init__(self, path: str | Path, retention_days: int = 7) -> None:
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("retention_days must be an integer from 1 to 365.")
        self.path = Path(path)
        self.retention_days = retention_days
        self._connection: sqlite3.Connection | None = None
        self._last_cleanup = 0.0

    def _open(self, create: bool) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        if not create and not self.path.exists():
            return None
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise sqlite3.DatabaseError("database integrity check failed")
            connection.execute("PRAGMA journal_mode=WAL")
            columns = ", ".join(f"{name} REAL" for name in METRICS)
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS samples (timestamp REAL PRIMARY KEY, {columns})"
            )
            expected = {"timestamp", *METRICS}
            actual = {row[1] for row in connection.execute("PRAGMA table_info(samples)")}
            if actual != expected:
                raise sqlite3.DatabaseError(
                    "history schema is incompatible with this Titan version"
                )
            connection.commit()
            self._connection = connection
            return connection
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise HistoryError(
                f"Cannot open Titan history: {exc}. The existing file at {self.path} was preserved. Back it up and choose a new history file to recover recording."
            ) from exc

    def record(self, snapshot: Snapshot) -> None:
        if not math.isfinite(snapshot.timestamp):
            raise HistoryError("A history sample needs a finite timestamp.")
        connection = self._open(create=True)
        assert connection is not None
        values = _metrics(snapshot)
        columns = ", ".join(("timestamp", *METRICS))
        placeholders = ", ".join("?" for _ in range(len(METRICS) + 1))
        try:
            with connection:
                connection.execute(
                    f"INSERT OR REPLACE INTO samples ({columns}) VALUES ({placeholders})",
                    (snapshot.timestamp, *(values[name] for name in METRICS)),
                )
            if time.time() - self._last_cleanup > 3600:
                self.cleanup()
        except sqlite3.Error as exc:
            raise HistoryError(
                f"Cannot record history: {exc}. Existing history was preserved."
            ) from exc

    def query(
        self, start: float | None = None, end: float | None = None, bucket_seconds: int = 0
    ) -> list[dict[str, Any]]:
        if type(bucket_seconds) is not int or bucket_seconds < 0:
            raise ValueError("bucket_seconds must be a nonnegative integer.")
        if any(value is not None and not math.isfinite(value) for value in (start, end)):
            raise ValueError("History range timestamps must be finite.")
        if start is not None and end is not None and start > end:
            raise ValueError("History start must be before end.")
        connection = self._open(create=False)
        if connection is None:
            return []
        conditions, parameters = [], []
        if start is not None:
            conditions.append("timestamp >= ?")
            parameters.append(start)
        if end is not None:
            conditions.append("timestamp <= ?")
            parameters.append(end)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        try:
            bounds = connection.execute(
                f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM samples{where}", parameters
            ).fetchone()
            if bounds[0] == 0:
                return []
            bucket = bucket_seconds
            if bounds[0] > MAX_QUERY_ROWS:
                bucket = max(bucket, math.ceil((bounds[2] - bounds[1]) / (MAX_QUERY_ROWS - 1)), 1)
            if bucket:
                aggregates = ", ".join(f"AVG({name}) AS {name}" for name in METRICS)
                sql = f"SELECT CAST(timestamp / ? AS INTEGER) * ? AS timestamp, COUNT(*) AS samples, {aggregates} FROM samples{where} GROUP BY CAST(timestamp / ? AS INTEGER) ORDER BY timestamp LIMIT ?"
                result = connection.execute(
                    sql, [bucket, bucket, *parameters, bucket, MAX_QUERY_ROWS]
                )
            else:
                result = connection.execute(
                    f"SELECT *, 1 AS samples FROM samples{where} ORDER BY timestamp LIMIT ?",
                    [*parameters, MAX_QUERY_ROWS],
                )
            return [dict(row) for row in result]
        except sqlite3.Error as exc:
            raise HistoryError(
                f"Cannot read history: {exc}. Existing history was preserved."
            ) from exc

    def cleanup(self) -> int:
        connection = self._open(create=False)
        if connection is None:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        try:
            with connection:
                cursor = connection.execute("DELETE FROM samples WHERE timestamp < ?", (cutoff,))
            self._last_cleanup = time.time()
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise HistoryError(f"Cannot apply history retention: {exc}.") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> HistoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
