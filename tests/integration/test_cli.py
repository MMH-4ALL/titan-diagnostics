from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from titan import cli
from titan.core.config import Config, load_config
from titan.core.models import Section, Snapshot

runner = CliRunner()


@pytest.fixture()
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Snapshot:
    monkeypatch.setattr("titan.core.config.default_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "default_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cli, "default_history_path", lambda: tmp_path / "history.sqlite3")
    snapshot = Snapshot(
        1_800_000_000,
        "Test",
        {
            name: Section({"percent": 25})
            for name in ("cpu", "memory", "gpu", "battery", "storage", "network", "processes")
        },
    )
    snapshot.sections["storage"].data = {"drives": [], "volumes": []}
    snapshot.sections["processes"].data = {
        "items": [
            {"pid": 123, "name": "PrivateName", "exe": "/home/alice/private", "username": "alice"}
        ]
    }
    monkeypatch.setattr(cli, "_collect", lambda: snapshot)
    return snapshot


@pytest.mark.parametrize(
    "command", ["overview", "cpu", "memory", "gpu", "storage", "network", "battery", "processes"]
)
def test_metric_commands_machine_readable(command: str, isolated_cli: Snapshot) -> None:
    result = runner.invoke(cli.app, [command, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["platform"] == "Test"


def test_version_and_help_do_not_launch_ui() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "Titan" in result.stdout
    for command in ([], ["storage"], ["network"], ["history"], ["config"]):
        result = runner.invoke(cli.app, [*command, "--help"])
        assert result.exit_code == 0


def test_report_default_privacy_review_and_file(isolated_cli: Snapshot, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["report", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert "alice" not in result.stdout
    assert "PrivateName" not in result.stdout
    assert json.loads(result.stdout)["privacy"] == "sensitive identifiers redacted"
    result = runner.invoke(cli.app, ["report", "--include-sensitive"])
    assert result.exit_code == 2
    assert "--review" in result.output
    path = tmp_path / "detailed.json"
    declined = runner.invoke(
        cli.app, ["report", "--include-sensitive", "--review", "--output", str(path)], input="n\n"
    )
    assert declined.exit_code != 0
    assert not path.exists()
    approved = runner.invoke(
        cli.app, ["report", "--include-sensitive", "--review", "--yes", "--output", str(path)]
    )
    assert approved.exit_code == 0, approved.output
    assert "alice" in path.read_text(encoding="utf-8")


def test_report_bad_format_and_write_failure(isolated_cli: Snapshot, tmp_path: Path) -> None:
    assert runner.invoke(cli.app, ["report", "--format", "pdf"]).exit_code == 2
    result = runner.invoke(
        cli.app, ["report", "--output", str(tmp_path / "absent" / "report.json")]
    )
    assert result.exit_code == 1
    assert "Titan:" in result.output


def test_history_opt_in_pause_and_missing_db(isolated_cli: Snapshot, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["history", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["samples"] == []
    assert not (tmp_path / "history.sqlite3").exists()
    assert runner.invoke(cli.app, ["history", "enable"]).exit_code == 0
    assert load_config(tmp_path / "config.toml").record_history
    assert runner.invoke(cli.app, ["history", "pause"]).exit_code == 0
    assert not load_config(tmp_path / "config.toml").record_history


def test_config_validation_and_reset_recovery(isolated_cli: Snapshot, tmp_path: Path) -> None:
    assert runner.invoke(cli.app, ["config", "set", "theme", "arctic"]).exit_code == 0
    assert load_config(tmp_path / "config.toml").theme == "arctic"
    assert runner.invoke(cli.app, ["config", "set", "retention_days", "true"]).exit_code == 1
    assert load_config(tmp_path / "config.toml").retention_days == 7
    (tmp_path / "config.toml").write_text("broken = [", encoding="utf-8")
    assert runner.invoke(cli.app, ["config", "--json"]).exit_code == 1
    assert runner.invoke(cli.app, ["config", "reset", "--yes"]).exit_code == 0
    assert load_config(tmp_path / "config.toml") == Config()


def test_diagnose_short_window_no_fabrication(
    isolated_cli: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_diagnostic_samples", lambda duration, interval: [isolated_cli])
    result = runner.invoke(cli.app, ["diagnose", "--duration", "0", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sample_count"] == 1
    assert not any(
        finding["id"] == "cpu-sustained" for finding in json.loads(result.stdout)["findings"]
    )


def test_storage_read_only_and_failed_metric_exit(isolated_cli: Snapshot) -> None:
    result = runner.invoke(cli.app, ["storage", "diagnose", "--json"])
    assert result.exit_code == 0
    assert "Read-only" in json.loads(result.stdout)["safety"]
    isolated_cli.sections["cpu"].error = "Access denied"
    result = runner.invoke(cli.app, ["cpu", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "Access denied"


def test_network_requires_target_and_failure_exit(
    isolated_cli: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(cli.app, ["network", "test"]).exit_code == 2
    assert runner.invoke(cli.app, ["network", "test", "localhost", "--count", "6"]).exit_code == 2
    import titan.hardware.network

    called = []

    def fake(target: str, **kwargs: object) -> Section:
        called.append((target, kwargs))
        return Section({"tcp": {"success": False}})

    monkeypatch.setattr(titan.hardware.network, "network_test", fake)
    result = runner.invoke(cli.app, ["network", "test", "localhost", "--port", "443", "--json"])
    assert result.exit_code == 1
    assert called[0][0] == "localhost"
    assert json.loads(result.stdout)["data"]["tcp"]["success"] is False


def test_process_export_is_redacted(isolated_cli: Snapshot, tmp_path: Path) -> None:
    path = tmp_path / "processes.json"
    result = runner.invoke(cli.app, ["processes", "--output", str(path)])
    assert result.exit_code == 0, result.output
    assert "alice" not in path.read_text(encoding="utf-8")


def test_doctor_handles_missing_tools_without_failing(
    isolated_cli: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert not any(json.loads(result.stdout)["optional_tools"].values())
