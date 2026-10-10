"""Script-friendly commands and the interactive Titan entry point."""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
import tomllib
from dataclasses import asdict
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from titan.core.config import (
    Config,
    ConfigError,
    default_config_path,
    default_data_dir,
    default_history_path,
    load_config,
    save_config,
    validate_config,
)
from titan.core.diagnostics import evaluate
from titan.core.history import HistoryError, HistoryStore
from titan.core.models import Snapshot
from titan.reports.export import export_processes, export_report

app = typer.Typer(
    no_args_is_help=False,
    help="Titan: local, read-only system monitoring and evidence-based diagnostics.",
    pretty_exceptions_enable=False,
)
storage_app = typer.Typer(
    invoke_without_command=True,
    help="Physical drives, mounted volumes and safe storage troubleshooting.",
)
network_app = typer.Typer(
    invoke_without_command=True,
    help="Network counters and explicitly requested connectivity tests.",
)
history_app = typer.Typer(
    invoke_without_command=True, help="Query opt-in history, enable recording or pause it."
)
config_app = typer.Typer(
    invoke_without_command=True, help="View, change or reset validated TOML settings."
)
snapshot_app = typer.Typer(help="Save redacted local before/after snapshots and compare them.")
app.add_typer(storage_app, name="storage")
app.add_typer(network_app, name="network")
app.add_typer(history_app, name="history")
app.add_typer(config_app, name="config")
app.add_typer(snapshot_app, name="snapshot")
console = Console()


def _version() -> str:
    try:
        return version("titan-diagnostics")
    except PackageNotFoundError:
        from titan import __version__

        return __version__


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"Titan {_version()}")
        raise typer.Exit()


def _error(exc: Exception) -> NoReturn:
    typer.echo(f"Titan: {exc}", err=True)
    raise typer.Exit(code=1)


def _configuration() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        _error(exc)


def _collect() -> Snapshot:
    from titan.core.collectors import Collector

    collector = Collector()
    collector.collect()
    time.sleep(0.15)
    return collector.collect()


def _print(value: Any, json_output: bool = False) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if isinstance(value, dict):
        table = Table(
            show_header=True, border_style="bright_blue", header_style="bold cyan", expand=True
        )
        table.add_column("Metric", style="cyan", ratio=1)
        table.add_column("Observation", ratio=3)
        for name, item in value.items():
            if item is None:
                rendered = "Unavailable"
            elif isinstance(item, (dict, list, tuple)):
                rendered = json.dumps(item, ensure_ascii=False, indent=2)
            else:
                rendered = str(item)
            table.add_row(Text(str(name)), Text(rendered))
        console.print(table)
    else:
        console.print_json(data=value)


def _metric(name: str, json_output: bool) -> None:
    try:
        snapshot = _collect()
        section = snapshot.sections.get(name)
        if section is None:
            _error(RuntimeError(f"Collector did not return a {name} section."))
        assert section is not None
        _print(
            {"timestamp": snapshot.timestamp, "platform": snapshot.platform, **asdict(section)},
            json_output,
        )
        if section.error:
            raise typer.Exit(code=1)
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@app.callback(invoke_without_command=True)
def _main_callback(
    ctx: typer.Context,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Display the installed version.",
        ),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is None:
        settings = _configuration()
        from titan.app import TitanApp

        TitanApp(config=settings).run()


@app.command()
def overview(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Print real observations and unavailable collector states."""
    try:
        snapshot = _collect()
        _print(asdict(snapshot), json_output)
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@app.command()
def cpu(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """CPU identification, usage, frequency and accessible sensors."""
    _metric("cpu", json_output)


@app.command()
def memory(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """RAM availability, usage and swap; caching is not a fault by itself."""
    _metric("memory", json_output)


@app.command()
def gpu(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Available GPU providers, metrics and explicit support limitations."""
    _metric("gpu", json_output)


@app.command()
def battery(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Battery and platform power information where available."""
    _metric("battery", json_output)


@app.command()
def processes(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "--export",
            "-o",
            help="Export redacted processes; .csv produces one process per row, otherwise JSON.",
        ),
    ] = None,
) -> None:
    """List running processes; export redacts paths, names and usernames."""
    if output is None:
        _metric("processes", json_output)
        return
    try:
        snapshot = _collect()
        process_section = snapshot.sections.get("processes")
        if process_section is None or process_section.error:
            raise RuntimeError(
                process_section.error if process_section else "Process metrics are unavailable."
            )
        process_snapshot = Snapshot(
            snapshot.timestamp, snapshot.platform, {"processes": process_section}
        )
        result = (
            export_processes(process_snapshot)
            if output.suffix.lower() == ".csv"
            else export_report(process_snapshot, [], "json")
        )
        output.write_text(result, encoding="utf-8")
        typer.echo(f"Saved redacted process export to {output}.")
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@storage_app.callback()
def storage(
    ctx: typer.Context, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    if ctx.invoked_subcommand is None:
        _metric("storage", json_output)


@storage_app.command("diagnose")
def storage_diagnose(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Explain detected unmounted, raw, read-only and unhealthy drives safely."""
    from titan.hardware.storage import add_smart_health, storage_findings

    try:
        snapshot = _collect()
        section = snapshot.sections.get("storage")
        if section is None:
            raise RuntimeError("Storage observations are unavailable.")
        add_smart_health(section)
        findings = storage_findings(section)
        _print(
            {
                "findings": [asdict(item) for item in findings],
                "unavailable": section.unavailable,
                "error": section.error,
                "safety": "Read-only diagnosis. Do not initialize or format a drive containing unrecovered data. SMART cannot guarantee future reliability.",
            },
            json_output,
        )
        if section.error:
            raise typer.Exit(code=1)
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@network_app.callback()
def network(
    ctx: typer.Context, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    if ctx.invoked_subcommand is None:
        _metric("network", json_output)


@network_app.command("test")
def network_test_command(
    target: Annotated[
        str, typer.Argument(help="An explicitly selected hostname or IP address; no scanning.")
    ],
    port: Annotated[int, typer.Option(min=1, max=65535)] = 443,
    count: Annotated[int, typer.Option(min=1, max=5)] = 4,
    timeout: Annotated[float, typer.Option(min=0.2, max=10)] = 3,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resolve DNS and test one chosen endpoint with bounded TCP/ICMP checks."""
    from titan.hardware.network import network_test

    try:
        section = network_test(target, port=port, count=count, timeout=timeout)
        _print(asdict(section), json_output)
        if section.error or section.data.get("tcp", {}).get("success") is False:
            raise typer.Exit(code=1)
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@network_app.command("stability")
def network_stability(
    target: str,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 443,
    duration: Annotated[float, typer.Option(min=1, max=3600)] = 60,
    interval: Annotated[float, typer.Option(min=1, max=60)] = 1,
    timeout: Annotated[float, typer.Option(min=0.2, max=10)] = 2,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Repeated TCP handshakes to your endpoint; Ctrl+C stops and prints partial results."""
    from titan.hardware.stability import StabilityTest

    try:
        test = StabilityTest(target, port, duration, interval, timeout)
        try:
            result = test.run()
        except KeyboardInterrupt:
            test.stop()
            test.state = "stopped"
            result = test.summary()
        _print(result, json_output)
        if result["failures"]:
            raise typer.Exit(code=1)
    except (ValueError, OSError) as exc:
        _error(exc)


@snapshot_app.command("save")
def snapshot_save(
    output: Path, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Save current measurements, redacted by default."""
    from titan.core.snapshots import save_snapshot

    try:
        path = save_snapshot(_collect(), output)
        _print({"saved": str(path)}, json_output)
    except (ValueError, OSError) as exc:
        _error(exc)


@snapshot_app.command("compare")
def snapshot_compare(
    before: Path, after: Path, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Compare two saved Titan snapshots. Missing measurements remain unavailable."""
    from titan.core.snapshots import compare_snapshots, load_snapshot

    try:
        _print(compare_snapshots(load_snapshot(before), load_snapshot(after)), json_output)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        _error(exc)


@snapshot_app.command("list")
def snapshot_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List up to 100 snapshots saved in Titan's local data folder."""
    root = default_data_dir() / "snapshots"
    try:
        _print(
            {"snapshots": [str(path) for path in sorted(root.glob("*.json"), reverse=True)[:100]]},
            json_output,
        )
    except OSError as exc:
        _error(exc)


@storage_app.command("health")
def storage_health(
    record: Annotated[
        bool, typer.Option(help="Store hashed drive identity and numeric counters locally.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read SMART and compare with saved successful reads; recording is explicit."""
    from titan.core.health_trends import HealthTrends
    from titan.hardware.storage import add_smart_health

    try:
        snapshot = _collect()
        section = snapshot.sections["storage"]
        add_smart_health(section)
        trends = HealthTrends(default_data_dir() / "health.sqlite3")
        alerts = trends.record(section, persist=record)
        _print(
            {"storage": asdict(section), "trend_alerts": alerts, "recording": record}, json_output
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@storage_app.command("health-history")
def health_history(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show numerical health points previously recorded on this computer."""
    from titan.core.health_trends import HealthTrends

    try:
        trends = HealthTrends(default_data_dir() / "health.sqlite3")
        _print(
            {
                "points": list(trends.points),
                "note": "Hashed identities are persistent local identifiers. Retention: 365 days / 4096 points.",
            },
            json_output,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@app.command("thermal")
def thermal_command(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """CPU sensors and direct thermal-event evidence where available."""
    _metric("thermal", json_output)


@app.command("hardware-map")
def hardware_map_command() -> None:
    """Draw an ASCII map of actual detected hardware and partitions."""

    from titan.hardware.map import hardware_map

    console.print(Text(hardware_map(_collect(), console.width)))


@app.command("monitor")
def monitor_command(
    duration: Annotated[float, typer.Option(min=1, max=300)] = 10,
    trace_tcp: Annotated[
        bool, typer.Option(help="Opt in to platform TCP tracing with existing permissions.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Observe drive events, disk latency/queues, thermal and process I/O activity."""
    from titan.core.collectors import Collector

    collector = Collector()
    collector.traffic.enabled = trace_tcp
    try:
        snapshot = collector.collect()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            snapshot = collector.collect()
        _print(
            {
                "timestamp": snapshot.timestamp,
                "platform": snapshot.platform,
                "sections": {
                    key: asdict(snapshot.sections[key])
                    for key in ("storage", "thermal", "processes", "process_traffic")
                },
            },
            json_output,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)
    finally:
        collector.traffic.enabled = False


def _diagnostic_samples(duration: float, interval: float) -> list[Snapshot]:
    from titan.core.collectors import Collector

    collector = Collector()
    samples = [collector.collect()]
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        time.sleep(min(interval, max(deadline - time.monotonic(), 0)))
        samples.append(collector.collect())
    return samples


@app.command()
def diagnose(
    duration: Annotated[
        float,
        typer.Option(
            min=0,
            max=300,
            help="Seconds to observe; sustained rules require the configured evidence window.",
        ),
    ] = 10.0,
    interval: Annotated[float, typer.Option(min=0.25, max=10)] = 1.0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    fail_on_warning: Annotated[
        bool, typer.Option(help="Exit 3 when warning or critical findings occur.")
    ] = False,
) -> None:
    """Collect a timed evidence window and run conservative local rules."""
    settings = _configuration()
    # Gap detection must use the actual requested collection cadence.
    settings = validate_config(asdict(settings) | {"refresh_seconds": interval})
    try:
        samples = _diagnostic_samples(duration, interval)
        from titan.hardware.storage import add_smart_health

        storage_section = samples[-1].sections.get("storage")
        if storage_section is not None:
            add_smart_health(storage_section)
        findings = evaluate(samples, settings)
        _print(
            {
                "observed_seconds": samples[-1].timestamp - samples[0].timestamp,
                "sample_count": len(samples),
                "findings": [asdict(item) for item in findings],
                "limitations": "No triggered rule is not a guarantee of hardware health; unavailable sensors remain unavailable.",
            },
            json_output,
        )
        if fail_on_warning and any(item.severity in {"warning", "critical"} for item in findings):
            raise typer.Exit(code=3)
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


@app.command()
def report(
    format: Annotated[
        str, typer.Option("--format", "-f", help="json, csv, html or text.")
    ] = "json",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    include_sensitive: Annotated[
        bool, typer.Option(help="Include local identifiers after reviewing them.")
    ] = False,
    review: Annotated[
        bool, typer.Option(help="Print a preview before writing the requested report.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Explicitly confirm a reviewed detailed export for scripts."
        ),
    ] = False,
) -> None:
    """Export an offline report, redacting personal identifiers by default."""
    if format.lower() not in {"json", "csv", "html", "text", "terminal"}:
        raise typer.BadParameter("Choose json, csv, html or text.", param_hint="--format")
    if include_sensitive and not review:
        raise typer.BadParameter(
            "Sensitive export requires --review so identifiers can be reviewed before export.",
            param_hint="--include-sensitive",
        )
    try:
        snapshot = _collect()
        findings = evaluate([snapshot], _configuration())
        result = export_report(snapshot, findings, format, include_sensitive=include_sensitive)
        if review:
            typer.echo(
                export_report(snapshot, findings, "text", include_sensitive=include_sensitive)
            )
            if include_sensitive and not yes:
                typer.confirm(
                    "Include the reviewed usernames, paths, device identifiers and network addresses?",
                    abort=True,
                )
        if output is not None:
            output.write_text(result, encoding="utf-8")
            typer.echo(
                f"Saved {'detailed' if include_sensitive else 'redacted'} report to {output}."
            )
        else:
            typer.echo(result, nl=False)
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)


def _timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
            return parsed.timestamp()
        except ValueError as exc:
            raise typer.BadParameter(
                "Use an epoch timestamp or ISO datetime with timezone, such as 2026-10-09T12:00:00Z."
            ) from exc


@history_app.callback()
def history(
    ctx: typer.Context,
    range_name: Annotated[str, typer.Option("--range", help="15m, 1h, 24h, or 7d.")] = "15m",
    start: Annotated[
        str | None, typer.Option(help="Custom start epoch or ISO datetime with timezone.")
    ] = None,
    end: Annotated[
        str | None, typer.Option(help="Custom end epoch or ISO datetime with timezone.")
    ] = None,
    bucket_seconds: Annotated[int, typer.Option("--bucket", min=0)] = 0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    path: Annotated[
        Path | None, typer.Option("--path", help="Override the history database path.")
    ] = None,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    ranges = {"15m": 900, "1h": 3600, "24h": 86400, "7d": 604800}
    if range_name not in ranges:
        raise typer.BadParameter("Choose 15m, 1h, 24h or 7d.", param_hint="--range")
    start_time = _timestamp(start) if start is not None else time.time() - ranges[range_name]
    end_time = _timestamp(end)
    try:
        with HistoryStore(path or default_history_path(), _configuration().retention_days) as store:
            rows = store.query(start_time, end_time, bucket_seconds)
        _print(
            {
                "recording_enabled": _configuration().record_history,
                "samples": rows,
                "note": "No historical data"
                if not rows
                else "Values are averages when samples are aggregated; missing values remain null.",
            },
            json_output,
        )
    except (HistoryError, ValueError) as exc:
        _error(exc)


def _set_recording(enabled: bool) -> None:
    try:
        current = _configuration()
        save_config(validate_config(asdict(current) | {"record_history": enabled}))
        typer.echo(
            "History recording enabled for future Titan sessions."
            if enabled
            else "History recording paused for future Titan sessions. Existing history is retained."
        )
    except ConfigError as exc:
        _error(exc)


@history_app.command("enable")
def history_enable() -> None:
    """Opt in to recording selected metrics during future interactive sessions."""
    _set_recording(True)


@history_app.command("pause")
def history_pause() -> None:
    """Pause future recording without deleting previous observations."""
    _set_recording(False)


@config_app.callback()
def config(
    ctx: typer.Context, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    if ctx.invoked_subcommand is None:
        _print(
            {"path": str(default_config_path()), "settings": asdict(_configuration())}, json_output
        )


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a validated setting. Examples: theme arctic; record_history true."""
    try:
        if key == "theme":
            parsed: Any = value
        else:
            try:
                parsed = tomllib.loads("value = " + value)["value"]
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(
                    "Use a TOML scalar: true, false, an integer or a decimal number."
                ) from exc
        settings = validate_config(asdict(_configuration()) | {key: parsed})
        save_config(settings)
        typer.echo(f"Saved {key} = {parsed}.")
    except ConfigError as exc:
        _error(exc)


@config_app.command("reset")
def config_reset(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Restore defaults, including paused history recording."""
    if not yes:
        typer.confirm(
            "Reset Titan settings to defaults? Existing history will be retained.", abort=True
        )
    try:
        save_config(Config())
        typer.echo("Default Titan settings restored.")
    except ConfigError as exc:
        _error(exc)


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check installation and optional tools without elevation or network probes."""
    import psutil

    tools = {name: shutil.which(name) is not None for name in ("smartctl", "nvidia-smi", "ping")}
    if os.name == "nt":
        tools["powershell"] = shutil.which("powershell") is not None
    else:
        tools["lsblk"] = shutil.which("lsblk") is not None
    try:
        settings: dict[str, Any] = asdict(load_config())
        config_error = None
    except ConfigError as exc:
        settings, config_error = {}, str(exc)
    information = {
        "titan_version": _version(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "psutil": psutil.__version__,
        "optional_tools": tools,
        "configuration_error": config_error,
        "history_recording": settings.get("record_history"),
        "data_directory": str(default_data_dir()),
        "guidance": [
            "SMART health needs optional smartmontools and compatible drive/bridge support; some queries require elevated access.",
            "GPU metrics depend on installed vendor tooling and driver support. Missing optional tools do not prevent ordinary monitoring.",
            "Titan does not request elevation, change security settings, probe networks, or perform storage repairs automatically.",
        ],
    }
    _print(information, json_output)
    if config_error:
        raise typer.Exit(code=1)


def main() -> None:
    """Package console entry point."""
    from titan.core.config import default_data_dir
    from titan.core.logging import configure_logging

    configure_logging(default_data_dir() / "logs")
    app()


if __name__ == "__main__":
    main()
