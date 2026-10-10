from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from titan.core.config import Config, ConfigError, load_config, save_config, validate_config
from titan.core.diagnostics import evaluate
from titan.core.history import MAX_QUERY_ROWS, HistoryError, HistoryStore
from titan.core.models import Finding, Section, Snapshot
from titan.reports.export import export_report


def sample(
    timestamp: float, cpu: float | None = 95, memory: float = 50, processes: list | None = None
) -> Snapshot:
    return Snapshot(
        timestamp,
        "Test",
        {
            "cpu": Section({"percent": cpu}),
            "memory": Section(
                {
                    "percent": memory,
                    "total": 1000,
                    "available": 1000 * (1 - memory / 100),
                    "swap_percent": 0,
                }
            ),
            "storage": Section({"drives": [], "volumes": []}),
            "processes": Section({"items": processes or []}),
        },
    )


def test_configuration_round_trip_and_missing_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings" / "config.toml"
    assert load_config(path) == Config()
    assert not path.exists()
    config = replace(Config(), theme="matrix", record_history=True, retention_days=21)
    save_config(config, path)
    assert load_config(path) == config
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "setting,value",
    [
        ("refresh_seconds", 0),
        ("retention_days", True),
        ("retention_days", 7.5),
        ("theme", "unknown"),
        ("cpu_threshold", float("nan")),
        ("record_history", "false"),
        ("refresh_seconds", True),
        ("unknown", 1),
    ],
)
def test_invalid_settings_rejected(setting: str, value: object) -> None:
    with pytest.raises(ConfigError):
        validate_config({setting: value})


def test_corrupt_config_has_recovery_guidance(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("theme = [bad", encoding="utf-8")
    with pytest.raises(ConfigError, match="config reset"):
        load_config(path)
    assert path.read_text(encoding="utf-8") == "theme = [bad"


def test_history_stays_off_until_recording_and_preserves_missing(tmp_path: Path) -> None:
    path = tmp_path / "history" / "samples.sqlite3"
    store = HistoryStore(path)
    assert not path.exists()
    assert store.query() == []
    assert not path.parent.exists()
    now = time.time()
    store.record(sample(now, cpu=None))
    row = store.query()[0]
    assert row["cpu_percent"] is None
    assert row["memory_percent"] == 50
    assert row["samples"] == 1
    store.close()
    with HistoryStore(path) as reopened:
        assert len(reopened.query()) == 1


def test_history_retention_and_average(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("titan.core.history.time.time", lambda: 2_000_000)
    with HistoryStore(tmp_path / "history.sqlite3", retention_days=1) as store:
        store.record(sample(2_000_000 - 90_000))
        store.record(sample(1_999_991, cpu=20))
        store.record(sample(1_999_992, cpu=40))
        store.cleanup()
        rows = store.query(bucket_seconds=10)
        assert len(rows) == 1
        assert rows[0]["cpu_percent"] == 30
        assert rows[0]["samples"] == 2
        assert store.query(start=1_999_992)[0]["cpu_percent"] == 40
        with pytest.raises(ValueError):
            store.query(start=2, end=1)


def test_history_queries_bounded_by_aggregation(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    now = time.time()
    with HistoryStore(path) as store:
        store.record(sample(now))
        with sqlite3.connect(path) as connection:
            connection.executemany(
                "INSERT INTO samples(timestamp,cpu_percent) VALUES (?,?)",
                [(now - index, 42) for index in range(1, MAX_QUERY_ROWS + 500)],
            )
        rows = store.query()
        assert len(rows) <= MAX_QUERY_ROWS
        assert sum(row["samples"] for row in rows) == MAX_QUERY_ROWS + 500
        assert any(row["samples"] > 1 for row in rows)


def test_history_corruption_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    contents = b"This is not a database; preserve me."
    path.write_bytes(contents)
    with pytest.raises(HistoryError, match="preserved"):
        HistoryStore(path).query()
    assert path.read_bytes() == contents


def test_cpu_requires_real_sustained_contiguous_time() -> None:
    assert evaluate([sample(0)]) == []
    assert evaluate([sample(index / 100) for index in range(20)]) == []
    assert evaluate([sample(0), sample(100)]) == []
    samples = [sample(index) for index in range(11)]
    findings = evaluate(samples)
    assert "cpu-sustained" in {finding.id for finding in findings}
    assert "10.0 seconds" in findings[0].evidence
    samples[5].sections["cpu"].data["percent"] = None
    assert not any(finding.id == "cpu-sustained" for finding in evaluate(samples))


def test_memory_pressure_needs_context_and_elapsed_time() -> None:
    samples = [sample(index, cpu=5, memory=95) for index in range(11)]
    assert any(finding.id == "memory-pressure" for finding in evaluate(samples))
    for observation in samples:
        observation.sections["memory"].data["available"] = 500
    assert not any(finding.id == "memory-pressure" for finding in evaluate(samples))


def test_process_identity_change_prevents_cpu_and_growth_findings() -> None:
    samples = [
        sample(
            index,
            cpu=5,
            processes=[
                {
                    "pid": 44,
                    "name": "worker",
                    "create_time": 1 if index < 5 else 2,
                    "cpu_percent": 98,
                    "memory_bytes": 1000,
                }
            ],
        )
        for index in range(11)
    ]
    assert not any(finding.id.startswith("process-") for finding in evaluate(samples))


def test_idle_accounting_is_not_an_active_application() -> None:
    processes = [
        {"pid": 0, "name": "System Idle Process", "create_time": 1, "cpu_percent": 1450},
        {"pid": 44, "name": "worker", "create_time": 1, "cpu_percent": 98},
    ]
    samples = [sample(index, cpu=5, processes=processes) for index in range(11)]
    identities = {finding.id for finding in evaluate(samples)}
    assert "process-cpu-0" not in identities
    assert "process-cpu-44" in identities


def test_process_growth_is_suspected_and_requires_long_window() -> None:
    settings = replace(Config(), refresh_seconds=20)
    samples = [
        sample(
            index * 20,
            cpu=5,
            processes=[
                {
                    "pid": 44,
                    "name": "worker",
                    "create_time": 1,
                    "cpu_percent": 0,
                    "memory_bytes": (100 + index * 20) * 1024**2,
                }
            ],
        )
        for index in range(4)
    ]
    growth = [finding for finding in evaluate(samples, settings) if "memory-growth" in finding.id]
    assert len(growth) == 1
    assert "suspected, not confirmed" in growth[0].explanation
    assert growth[0].confidence == "low"
    assert not any("memory-growth" in finding.id for finding in evaluate(samples[:3], settings))


def test_battery_requires_reliable_capacity_and_unavailable_is_not_fault() -> None:
    observation = sample(0, cpu=5)
    observation.sections["battery"] = Section({"wear_percent": 50})
    assert not evaluate([observation])
    observation.sections["battery"].data.update(design_capacity=100, full_charge_capacity=50)
    assert any(finding.id == "battery-wear" for finding in evaluate([observation]))
    observation.sections["gpu"] = Section(error="Permission denied")
    assert any(finding.severity == "informational" for finding in evaluate([observation]))


def sensitive_sample() -> tuple[Snapshot, list[Finding]]:
    observation = sample(time.time(), cpu=5)
    observation.sections["processes"] = Section(
        {
            "items": [
                {
                    "pid": 55,
                    "name": "SecretWorker",
                    "exe": "C:\\Users\\Alice\\private.exe",
                    "username": "Alice",
                    "cpu_percent": 2,
                }
            ]
        }
    )
    observation.sections["network"] = Section(
        {
            "interfaces": {
                "Alice's VPN": {
                    "addresses": [{"address": "192.168.1.23"}],
                    "mac": "aa:bb:cc:dd:ee:ff",
                }
            }
        }
    )
    observation.sections["storage"] = Section(
        {"drives": [{"serial_number": "SECRET1234", "name": "Alice drive"}]}
    )
    findings = [
        Finding(
            "test",
            "A process issue",
            "attention",
            "Alice runs SecretWorker from C:\\Users\\Alice\\private.exe at 192.168.1.23.",
            "<script>alert('unsafe')</script>",
            ["unknown"],
            "Review carefully",
        )
    ]
    return observation, findings


@pytest.mark.parametrize("format", ["json", "csv", "html", "text"])
def test_all_reports_redact_values_and_free_text(format: str) -> None:
    observation, findings = sensitive_sample()
    report = export_report(observation, findings, format)
    for identifier in (
        "Alice",
        "SecretWorker",
        "private.exe",
        "192.168.1.23",
        "SECRET1234",
        "aa:bb:cc:dd:ee:ff",
    ):
        assert identifier not in report
    assert "[redacted]" in report


def test_html_offline_and_escaped_csv_safe_and_sensitive_opt_in() -> None:
    observation, findings = sensitive_sample()
    output = export_report(observation, findings, "html")
    assert "<script>" not in output
    assert "&lt;script&gt;" in output
    assert "Content-Security-Policy" in output
    assert "http://" not in output and "https://" not in output
    detailed = json.loads(export_report(observation, findings, "json", True))
    assert detailed["snapshot"]["sections"]["processes"]["data"]["items"][0]["username"] == "Alice"
    findings[0].explanation = '=HYPERLINK("https://bad")'
    rows = list(csv.reader(io.StringIO(export_report(observation, findings, "csv"))))
    assert any(value.startswith("'=HYPERLINK") for _, value in rows)
    with pytest.raises(ValueError, match="format"):
        export_report(observation, findings, "pdf")


def test_missing_report_metrics_are_null_not_zero() -> None:
    result = json.loads(export_report(sample(0, cpu=None), [], "json"))
    assert result["snapshot"]["sections"]["cpu"]["data"]["percent"] is None
    assert "schema_version" in result
    assert "tools" in result
    assert asdict(Config())["record_history"] is False
