"""Keyboard-first terminal workspace; collection always runs off the UI thread."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Collapsible,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    Sparkline,
    Static,
    Switch,
)

from titan.art import COMPONENT_ART, WORDMARK
from titan.core.collectors import Collector
from titan.core.config import (
    Config,
    ConfigError,
    default_data_dir,
    default_history_path,
    load_config,
    save_config,
)
from titan.core.diagnostics import evaluate
from titan.core.health_trends import HealthTrendError, HealthTrends
from titan.core.history import HistoryError, HistoryStore
from titan.core.models import Section, Snapshot
from titan.core.monitoring import drive_identity
from titan.core.processes import inspect_process, terminate_process
from titan.core.snapshots import compare_snapshots, load_snapshot, save_snapshot, snapshot_payload
from titan.hardware.map import hardware_map
from titan.hardware.network import network_test
from titan.hardware.stability import StabilityTest
from titan.hardware.storage import add_smart_health
from titan.reports.export import export_processes, export_report

PAGES = [
    ("overview", "Overview"),
    ("cpu", "CPU"),
    ("memory", "Memory"),
    ("gpu", "GPU"),
    ("storage", "Storage"),
    ("network", "Network"),
    ("battery", "Battery"),
    ("processes", "Processes"),
    ("diagnostics", "Diagnostics"),
    ("history", "History"),
    ("reports", "Reports"),
    ("settings", "Settings"),
    ("monitoring", "Monitoring"),
    ("hardware", "Hardware map"),
    ("compare", "Snapshots"),
]


def number(value: Any, suffix: str = "", precision: int = 1) -> str:
    return "Unavailable" if value is None else f"{value:,.{precision}f}{suffix}"


def size(value: Any) -> str:
    if value is None:
        return "Unavailable"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:,.1f} {unit}"
        amount /= 1024
    return "Unavailable"


class MetricCard(Vertical):
    def __init__(self, title: str, id: str, component: str) -> None:
        super().__init__(id=id, classes="metric-card")
        self.border_title = title
        self.component = component

    def compose(self) -> ComposeResult:
        with Horizontal(classes="metric-readout"):
            yield Static(Text(COMPONENT_ART[self.component], no_wrap=True), classes="component-art")
            with Vertical(classes="metric-reading"):
                yield Static("Collecting…", classes="metric-value")
                yield Static("Waiting for first measurement", classes="metric-note")
        yield Sparkline([])
        yield Static("", classes="trace-placeholder")
        yield Static("WAITING FOR READINGS", classes="trace-label")

    def update_metric(self, value: str, note: str, values: list[float]) -> None:
        self.query_one(".metric-value", Static).update(Text(value))
        self.query_one(".metric-note", Static).update(Text(note))
        graph = self.query_one(Sparkline)
        graph.data = values
        graph.display = len(values) >= 2
        self.query_one(".trace-placeholder", Static).display = len(values) < 2
        self.query_one(".trace-label", Static).update(
            Text(
                f"{len(values):03} READINGS / RELATIVE SCALE"
                if len(values) >= 2
                else "001 READINGS / TRACE PENDING"
                if values
                else "NO READINGS YET"
            )
        )


class InfoDialog(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, title: str, body: str, confirm: bool = False) -> None:
        super().__init__()
        self.dialog_title, self.body, self.confirm = title, body, confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.dialog_title), classes="page-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(Text(self.body))
            with Horizontal(classes="button-row"):
                yield Button("Cancel" if self.confirm else "Close", id="dialog-cancel")
                if self.confirm:
                    yield Button("Terminate process", id="dialog-confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#dialog-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "dialog-confirm")


class SearchDialog(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label("SEARCH TITAN", classes="eyebrow")
            yield Input(placeholder="Page name or process name…", id="global-query")
            yield Static("Enter opens a matching page; other text searches processes.")
            yield Static(" · ".join(label for _, label in PAGES), id="search-matches")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Changed)
    def changed(self, event: Input.Changed) -> None:
        matches = [label for _, label in PAGES if event.value.lower() in label.lower()]
        self.query_one("#search-matches", Static).update(
            Text(" · ".join(matches) or "Search running processes")
        )

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        matches = [key for key, label in PAGES if value.lower() in label.lower()]
        self.dismiss((matches[0], "") if matches and value else ("processes", value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TitanScreen(Screen):
    def on_resize(self, event: events.Resize) -> None:
        if isinstance(self.app, TitanApp):
            self.app.adapt_layout(event.size.width, event.size.height)


class TitanApp(App[None]):
    TITLE = "TITAN"
    SUB_TITLE = "System health & diagnostics"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS = [
        *[
            Binding(str(i), f"navigate('{key}')", label, show=False)
            for i, (key, label) in enumerate(PAGES[:9], 1)
        ],
        Binding("slash", "search", "Search"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "diagnose", "Diagnose"),
        Binding("question_mark", "help", "Help"),
        Binding("f1", "help", "Help", show=False),
        Binding("q", "quit", "Quit"),
        Binding("x", "terminate", "Terminate", show=False),
        Binding("enter", "inspect", "Inspect", show=False),
    ]
    CSS = """
    Screen { background: $background; color: $text; }
    #masthead { height: 8; padding: 1 2 0 2; background: $background; border-bottom: ascii $primary; }
    #brand { width: 1fr; }
    #logo { color: $primary; text-style: bold; height: 5; }
    #compact-logo { display: none; color: $primary; text-style: bold; height: 1; }
    #tagline { color: $text-muted; height: 1; }
    #status { width: 36; content-align: right middle; color: $text; }
    #workspace { height: 1fr; }
    #sidebar { width: 22; padding: 1; background: $background; border-right: ascii $boost; }
    #nav-heading { height: 2; color: $text-muted; }
    #nav-guide { height: 4; margin-top: 1; color: $text-muted; }
    #sidebar Button { width: 100%; min-width: 0; height: 2; border: none; background: transparent; color: $text-muted; text-align: left; content-align: left middle; padding: 0; }
    #sidebar Button:hover { background: $boost; color: $text; }
    #sidebar Button.active { background: $primary 15%; color: $primary; text-style: bold; }
    #pages { width: 1fr; }
    .page { padding: 1 2; }
    .page-title { height: 2; text-style: bold; color: $primary; border-bottom: ascii $boost; }
    .eyebrow { color: $primary; text-style: bold; height: 1; }
    .intro { color: $text-muted; margin-bottom: 1; height: auto; }
    #metrics { grid-size: 2 2; grid-gutter: 1; height: 19; }
    .metric-card { background: $background; border: ascii $primary 50%; border-title-color: $primary; border-title-style: bold; padding: 1 2; height: 9; }
    .metric-readout { height: 3; }
    .component-art { width: 11; height: 3; color: $primary; }
    .metric-reading { width: 1fr; height: 3; }
    .metric-value { text-style: bold; color: $text; height: 1; }
    .metric-note { color: $text-muted; height: 2; }
    .metric-card Sparkline { height: 1; margin: 0; display: none; }
    .trace-placeholder { height: 1; }
    .trace-label { color: $text-muted; height: 1; }
    Sparkline { height: 2; margin-bottom: 1; }
    .insights { border: ascii $boost; background: $background; padding: 1 2; height: auto; min-height: 5; margin-top: 1; }
    .detail-summary { height: auto; margin-bottom: 1; color: $primary; }
    DataTable { height: 1fr; min-height: 8; max-height: 28; background: $surface; }
    .page > DataTable { height: 20; }
    Collapsible { margin-top: 1; background: $surface; }
    .button-row { height: auto; margin: 1 0; }
    .button-row Button { margin-right: 1; min-width: 12; }
    Input { margin-bottom: 1; }
    .settings-row { height: auto; margin-bottom: 1; }
    .settings-row Label { width: 26; content-align: left middle; }
    .settings-row Select { width: 1fr; }
    #diagnostic-list, #network-result { height: auto; }
    #history-graph { height: 6; border: ascii $boost; }
    #record-state { color: $text-muted; }
    .advanced-readout { height: auto; border: ascii $boost; padding: 1; margin-bottom: 1; }
    #hardware-art { height: auto; color: $primary; text-style: bold; }
    #stability-result, #traffic-state, #compare-result { height: auto; }
    #stability-graph { height: 3; }
    #disk-activity-table, #event-table { height: 6; min-height: 3; }
    #compare-table { height: 12; }
    Footer { background: $surface; }
    .medium #sidebar Button { height: 1; }
    .medium #status { width: 32; }
    .medium .page { padding: 1; }
    .small #sidebar { display: none; }
    .stacked #metrics { grid-size: 1 4; height: 39; }
    .compact-header #masthead { layout: vertical; height: 4; padding: 0 1; }
    .compact-header #brand { width: 100%; height: 1; }
    .compact-header #logo, .compact-header #tagline { display: none; }
    .compact-header #compact-logo { display: block; }
    .compact-header #status { width: 100%; height: 2; content-align: left middle; }
    .small .page { padding: 1; }
    .small .button-row { layout: vertical; }
    .small .button-row Button { width: 100%; margin-bottom: 1; }
    .small .settings-row { layout: vertical; }
    .small .settings-row Label { width: 100%; }
    .small .settings-row Select { width: 100%; }
    InfoDialog, SearchDialog { align: center middle; background: $background 75%; }
    #dialog, #search-dialog { width: 80%; max-width: 90; height: auto; max-height: 90%; background: $surface; border: ascii $primary; padding: 1 2; }
    #dialog-body { height: auto; max-height: 24; }
    """

    def __init__(
        self,
        collector: Any = None,
        config: Config | None = None,
        config_path: Path | None = None,
        history_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self.startup_error = ""
        try:
            self.config = config or load_config(config_path)
        except ConfigError as exc:
            self.config = Config()
            self.startup_error = str(exc)
        self.collector = collector or Collector()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="titan-collector")
        self.history_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="titan-history"
        )
        self.samples: deque[Snapshot] = deque(maxlen=900)
        self.snapshot: Snapshot | None = None
        self.history_path = history_path or default_history_path()
        self.history: HistoryStore | None = None
        self.last_record = 0.0
        self.collecting = False
        self.page = "overview"
        self.per_core_mode = True
        self.sort_column: dict[str, str] = {}
        self.sort_reverse: dict[str, bool] = {}
        self.process_rows: dict[str, dict[str, Any]] = {}
        self.drive_health: dict[tuple[str, str], dict[str, Any]] = {}
        self.smart_check_at = 0.0
        self.smart_collecting = False
        self.network_testing = False
        self.requested_network_test: dict[str, Any] | None = None
        self.last_analysis = 0.0
        self.health_tracking = False
        self.health_trends = HealthTrends()
        self.stability: StabilityTest | None = None
        self.baseline: dict[str, Any] | None = None
        self.setup_themes()
        self.theme = self.config.theme

    def setup_themes(self) -> None:
        palettes = [
            ("titan-dark", "#E6B86A", "#8FAE99", "#0C0F0D", "#171D19", "#E4E8DF", True),
            ("arctic", "#0369A1", "#2563EB", "#F0F6FC", "#FFFFFF", "#0F172A", False),
            ("matrix", "#4ADE80", "#86EFAC", "#06110A", "#0F2115", "#E0FFE9", True),
            ("monochrome", "#D4D4D4", "#A3A3A3", "#111111", "#202020", "#F5F5F5", True),
            ("high-contrast", "#FFFF00", "#00FFFF", "#000000", "#000000", "#FFFFFF", True),
        ]
        for name, primary, secondary, background, surface, foreground, dark in palettes:
            self.register_theme(
                Theme(
                    name=name,
                    primary=primary,
                    secondary=secondary,
                    background=background,
                    surface=surface,
                    foreground=foreground,
                    success="#34D399",
                    warning="#FBBF24",
                    error="#FB7185",
                    dark=dark,
                )
            )

    def get_default_screen(self) -> Screen:
        return TitanScreen()

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            with Vertical(id="brand"):
                yield Static(Text(WORDMARK, no_wrap=True), id="logo")
                yield Static(Text("[ TITAN ] / LOCAL DIAGNOSTICS"), id="compact-logo")
                yield Label("SYSTEM DIAGNOSTICS / OFFLINE CONSOLE", id="tagline")
            yield Static("Starting collectors…\nRecording off by default", id="status")
        with Horizontal(id="workspace"):
            with VerticalScroll(id="sidebar"):
                yield Static(Text("[ NAVIGATION ]"), id="nav-heading")
                for index, (key, label) in enumerate(PAGES):
                    prefix = str(index + 1) if index < 9 else "+"
                    yield Button(
                        Text(
                            f"[{prefix}] {'Hardware' if key == 'hardware' else label}",
                            no_wrap=True,
                            overflow="ellipsis",
                        ),
                        id=f"nav-{key}",
                        classes="active" if index == 0 else "",
                    )
                yield Static(
                    Text("-----------------\n/  Search\n^P Commands\n?  Key guide"), id="nav-guide"
                )
            with ContentSwitcher(initial="overview", id="pages"):
                with VerticalScroll(id="overview", classes="page"):
                    yield Label(Text("[ 01 / SYSTEM OVERVIEW ]"), classes="page-title")
                    yield Static("Live readings. Local analysis. No uploads.", classes="intro")
                    with Grid(id="metrics"):
                        yield MetricCard("CPU / COMPUTE", id="cpu-card", component="cpu")
                        yield MetricCard(
                            "MEMORY / WORKING SET", id="memory-card", component="memory"
                        )
                        yield MetricCard("STORAGE / I/O", id="storage-card", component="storage")
                        yield MetricCard(
                            "NETWORK / TRANSFER", id="network-card", component="network"
                        )
                    yield Static(
                        "Collecting evidence for system insights…",
                        id="overview-insights",
                        classes="insights",
                    )
                for index, (key, label) in enumerate(PAGES[1:8], 2):
                    with VerticalScroll(id=key, classes="page"):
                        yield Label(Text(f"[ {index:02} / {label.upper()} ]"), classes="page-title")
                        yield Static("Collecting…", id=f"summary-{key}", classes="detail-summary")
                        if key in {"cpu", "memory", "gpu", "network"}:
                            yield Static(
                                "Recent observed values · graph scales to this window",
                                classes="intro",
                            )
                            yield Sparkline([], id=f"graph-{key}")
                        if key == "cpu":
                            yield Button("Show aggregate usage", id="cpu-mode")
                        if key == "processes":
                            yield Input(placeholder="Filter by name or PID…", id="process-filter")
                            with Horizontal(classes="button-row"):
                                yield Button("Inspect", id="process-inspect")
                                yield Button("Terminate…", id="process-terminate", variant="error")
                                yield Button("Export CSV", id="process-export")
                            with Horizontal(classes="settings-row"):
                                yield Label("Trace process TCP traffic")
                                yield Switch(False, id="traffic-switch")
                            yield Static(
                                "Tracing off. Windows needs existing EStats rights; Linux needs optional BCC tcptop and BPF permissions.",
                                id="traffic-state",
                            )
                        if key == "storage":
                            yield Button("Check external drives", id="storage-diagnose")
                        yield DataTable(id=f"table-{key}", cursor_type="row", zebra_stripes=True)
                        if key == "network":
                            yield Input(
                                placeholder="Your chosen hostname or IP (no URL)",
                                id="network-target",
                            )
                            yield Button("Test selected endpoint", id="network-test")
                            yield Static(
                                "No connectivity tests run automatically.", id="network-result"
                            )
                            yield Label("TCP STABILITY / YOUR SELECTED ENDPOINT", classes="eyebrow")
                            yield Input(
                                "443", placeholder="TCP port (1-65535)", id="stability-port"
                            )
                            yield Input(
                                "60",
                                placeholder="Duration seconds (1-3600)",
                                id="stability-duration",
                            )
                            with Horizontal(classes="button-row"):
                                yield Button("Start stability test", id="stability-start")
                                yield Button("Stop test", id="stability-stop", disabled=True)
                            yield Sparkline([], id="stability-graph")
                            yield Static(
                                "No test running. Measures TCP handshakes, not bandwidth or packet loss.",
                                id="stability-result",
                            )
                        with Collapsible(title="Measurement details & limitations", collapsed=True):
                            yield Static("Waiting for measurements", id=f"details-{key}")
                with VerticalScroll(id="diagnostics", classes="page"):
                    yield Label(Text("[ 09 / DIAGNOSTICS ]"), classes="page-title")
                    yield Static(
                        "Findings explain observations and possible causes. They are not hardware guarantees.",
                        classes="intro",
                    )
                    yield Button("Analyze collected evidence", id="diagnose-now", variant="primary")
                    yield Static("Collecting evidence…", id="diagnostic-list", classes="insights")
                with VerticalScroll(id="history", classes="page"):
                    yield Label(Text("[ 10 / PERFORMANCE HISTORY ]"), classes="page-title")
                    yield Static(
                        "History is opt-in. Only aggregate metrics are written locally, at the configured interval.",
                        classes="intro",
                    )
                    with Horizontal(classes="button-row"):
                        for span, label in [
                            (0, "Live"),
                            (900, "15 min"),
                            (3600, "1 hour"),
                            (86400, "24 hours"),
                            (604800, "7 days"),
                        ]:
                            yield Button(label, id=f"history-{span}")
                    yield Static("Recording paused", id="record-state")
                    yield Sparkline([], id="history-graph")
                    yield DataTable(id="history-table", zebra_stripes=True)
                with VerticalScroll(id="reports", classes="page"):
                    yield Label(Text("[ 11 / REPORT EXPORT ]"), classes="page-title")
                    yield Static(
                        "Exports redact network addresses, usernames, serials and personal paths. Review every file before sharing. Files stay on this computer.",
                        classes="intro",
                    )
                    with Horizontal(classes="button-row"):
                        for format_name in ("html", "json", "csv", "text"):
                            yield Button(
                                format_name.upper(),
                                id=f"export-{format_name}",
                                variant="primary" if format_name == "html" else "default",
                            )
                    yield Static(
                        "Use the CLI to select an output path or review a detailed report.",
                        id="report-result",
                        classes="insights",
                    )
                with VerticalScroll(id="settings", classes="page"):
                    yield Label(Text("[ 12 / SETTINGS ]"), classes="page-title")
                    yield Static(
                        "Choose a theme to preview it immediately. Save applies your preferences to the next launch.",
                        classes="intro",
                    )
                    with Horizontal(classes="settings-row"):
                        yield Label("Theme preview")
                        yield Select(
                            [
                                (label, key)
                                for key, label in [
                                    ("titan-dark", "Titan Dark"),
                                    ("arctic", "Arctic"),
                                    ("matrix", "Matrix"),
                                    ("monochrome", "Monochrome"),
                                    ("high-contrast", "High Contrast"),
                                ]
                            ],
                            value=self.config.theme,
                            allow_blank=False,
                            id="theme-select",
                        )
                    with Horizontal(classes="settings-row"):
                        yield Label("Record performance history")
                        yield Switch(self.config.record_history, id="record-switch")
                    yield Input(
                        str(self.config.refresh_seconds),
                        placeholder="Refresh seconds (0.5–60)",
                        id="refresh-seconds",
                    )
                    with Horizontal(classes="button-row"):
                        yield Button("Save preferences", id="settings-save", variant="primary")
                        yield Button("Reset defaults", id="settings-reset")
                    yield Static(
                        "Keyboard: 1–9 pages · / search · Ctrl+P commands · ? help", classes="intro"
                    )
                with VerticalScroll(id="monitoring", classes="page"):
                    yield Label(Text("[ 13 / MONITORING ]"), classes="page-title")
                    yield Static(
                        "Session drive events, per-device I/O and direct thermal evidence. Inventory is polled every 10 seconds; events between polls may be missed.",
                        classes="intro",
                    )
                    yield Label("DISK RESPONSE / INTERVAL MEASUREMENTS", classes="eyebrow")
                    yield DataTable(id="disk-activity-table", zebra_stripes=True)
                    yield Static("Waiting for counters", id="disk-activity-note", classes="intro")
                    yield Label("DRIVE CONNECTION TIMELINE / THIS SESSION", classes="eyebrow")
                    yield DataTable(id="event-table", zebra_stripes=True)
                    with Horizontal(classes="settings-row"):
                        yield Label("Track SMART every 5 minutes")
                        yield Switch(False, id="health-track-switch")
                    yield Button("Read drive health now", id="health-read")
                    yield Static(
                        "Two successful reads of the same drive are needed for a trend. Tracking stores hashed identity and numerical counters locally, for up to one year / 4096 points.",
                        id="health-trend-result",
                        classes="advanced-readout",
                    )
                    yield Label("THERMAL SENSORS / DIRECT EVENT EVIDENCE", classes="eyebrow")
                    yield Static(
                        "Waiting for sensors", id="thermal-result", classes="advanced-readout"
                    )
                with VerticalScroll(id="hardware", classes="page"):
                    yield Label(Text("[ 14 / HARDWARE MAP ]"), classes="page-title")
                    yield Static(
                        "Drives, partitions, adapters and interfaces. The branches describe inventory membership.",
                        classes="intro",
                    )
                    yield Static(Text("Collecting inventory..."), id="hardware-art")
                with VerticalScroll(id="compare", classes="page"):
                    yield Label(Text("[ 15 / BEFORE + AFTER ]"), classes="page-title")
                    yield Static(
                        "Capture a baseline, make your change, then capture again. Redacted snapshots stay on this computer. Point samples do not prove causation.",
                        classes="intro",
                    )
                    with Horizontal(classes="button-row"):
                        yield Button("Capture before", id="snapshot-before")
                        yield Button("Capture after + compare", id="snapshot-after")
                    yield Input(placeholder="Path to a saved Titan snapshot", id="snapshot-path")
                    yield Button("Load as baseline", id="snapshot-load")
                    yield Static(
                        "No baseline selected.", id="compare-result", classes="advanced-readout"
                    )
                    yield DataTable(id="compare-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.init_tables()
        self.adapt_layout()
        self.timer = self.set_interval(self.config.refresh_seconds, self.refresh_sample)
        self.refresh_sample()
        if self.startup_error:
            self.notify(self.startup_error, severity="warning", timeout=15)

    def init_tables(self) -> None:
        columns = {
            "cpu": ["Core", "Usage %"],
            "memory": ["PID", "Process", "RAM", "%"],
            "gpu": ["GPU", "Usage %", "Memory", "Temp °C", "Power W"],
            "storage": ["Kind", "Device / volume", "Capacity", "Used %", "Status"],
            "network": ["Interface", "State", "Download/s", "Upload/s", "Link Mb/s"],
            "battery": ["Measurement", "Value"],
            "processes": [
                "PID",
                "Process",
                "CPU %",
                "RAM",
                "RAM %",
                "I/O read/s",
                "I/O write/s",
                "TCP down/s",
                "TCP up/s",
            ],
        }
        for key, labels in columns.items():
            table = self.query_one(f"#table-{key}", DataTable)
            for i, label in enumerate(labels):
                table.add_column(label, key=str(i))
        self.query_one("#history-table", DataTable).add_columns(
            "Time", "CPU %", "RAM %", "Download/s", "Upload/s"
        )
        self.query_one("#disk-activity-table", DataTable).add_columns(
            "Device",
            "Read/s",
            "Write/s",
            "Read ms/op",
            "Write ms/op",
            "Read IOPS",
            "Write IOPS",
            "Queue",
        )
        self.query_one("#event-table", DataTable).add_columns(
            "Observed at", "Event", "Device", "Model", "Identity"
        )
        self.query_one("#compare-table", DataTable).add_columns(
            "Measurement", "Before", "After", "Change", "Unit"
        )

    def on_resize(self) -> None:
        self.adapt_layout()

    def adapt_layout(self, width: int | None = None, height: int | None = None) -> None:
        width = self.size.width if width is None else width
        height = self.size.height if height is None else height
        self.set_class(width < 80, "small")
        self.set_class(80 <= width < 120, "medium")
        self.set_class(width < 96, "stacked")
        self.set_class(width < 80 or height < 28, "compact-header")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.focused, Input) and action in {
            "quit",
            "refresh",
            "diagnose",
            "navigate",
            "search",
            "help",
            "terminate",
            "inspect",
        }:
            return False
        return True

    def action_navigate(self, key: str) -> None:
        if key not in dict(PAGES):
            return
        self.page = key
        self.query_one("#pages", ContentSwitcher).current = key
        self.set_focus(None)
        for button in self.query("#sidebar Button"):
            button.set_class(button.id == f"nav-{key}", "active")
        if key == "processes":
            self.query_one("#table-processes", DataTable).focus()
        elif key == "history":
            self.show_history(0)
        if self.snapshot:
            self.render_snapshot()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id or ""
        if key.startswith("nav-"):
            self.action_navigate(key[4:])
        elif key in {"diagnose-now", "storage-diagnose"}:
            self.action_diagnose()
        elif key == "process-inspect":
            self.action_inspect()
        elif key == "process-terminate":
            self.action_terminate()
        elif key == "process-export":
            self.export("csv", process_only=True)
        elif key == "cpu-mode":
            self.per_core_mode = not self.per_core_mode
            event.button.label = (
                "Show aggregate usage" if self.per_core_mode else "Show per-core usage"
            )
            self.update_table("cpu")
        elif key.startswith("export-"):
            self.export(key[7:])
        elif key.startswith("history-"):
            self.show_history(int(key[8:]))
        elif key == "settings-save":
            self.save_preferences()
        elif key == "settings-reset":
            self.config = Config()
            self.query_one("#theme-select", Select).value = self.config.theme
            self.query_one("#record-switch", Switch).value = False
            self.query_one("#refresh-seconds", Input).value = str(self.config.refresh_seconds)
            self.save_preferences()
        elif key == "network-test":
            self.network_test()
        elif key == "health-read":
            self.read_drive_health()
        elif key == "stability-start":
            self.start_stability()
        elif key == "stability-stop" and self.stability:
            self.stability.stop()
        elif key in {"snapshot-before", "snapshot-after", "snapshot-load"}:
            self.capture_comparison(key)

    @on(Switch.Changed, "#traffic-switch")
    def change_traffic(self, event: Switch.Changed) -> None:
        traffic = getattr(self.collector, "traffic", None)
        if traffic is not None:
            traffic.enabled = event.value
        self.action_refresh()

    @on(Switch.Changed, "#health-track-switch")
    def change_health_tracking(self, event: Switch.Changed) -> None:
        self.health_tracking = event.value
        if event.value:
            self.load_health_history()

    @work(group="health-history", exit_on_error=False)
    async def load_health_history(self) -> None:
        try:
            self.health_trends = await asyncio.to_thread(
                HealthTrends, default_data_dir() / "health.sqlite3"
            )
            self.smart_check_at = 0
            self.read_drive_health()
        except Exception as exc:
            self.health_tracking = False
            self.query_one("#health-track-switch", Switch).value = False
            self.notify(
                f"Health tracking could not open its local store ({type(exc).__name__}).",
                severity="error",
            )

    @on(Select.Changed, "#theme-select")
    def change_theme(self, event: Select.Changed) -> None:
        if event.value != self.query_one("#theme-select", Select).value:
            return
        if isinstance(event.value, str):
            self.theme = event.value
            self.config = replace(self.config, theme=event.value)

    @on(Switch.Changed, "#record-switch")
    def change_recording(self, event: Switch.Changed) -> None:
        self.config = replace(self.config, record_history=event.value)
        self.notify(
            "Local aggregate history recording on" if event.value else "History recording paused"
        )

    def save_preferences(self) -> None:
        try:
            refresh = float(self.query_one("#refresh-seconds", Input).value)
            if not 0.5 <= refresh <= 60:
                raise ValueError("Refresh interval must be 0.5–60 seconds")
            self.config = replace(self.config, refresh_seconds=refresh)
            save_config(self.config, self.config_path)
            self.timer.stop()
            self.timer = self.set_interval(refresh, self.refresh_sample)
            self.notify("Preferences saved")
        except (ValueError, OSError, ConfigError) as exc:
            self.notify(str(exc), severity="error")

    def action_refresh(self) -> None:
        self.refresh_sample()

    @work(group="collect", exit_on_error=False)
    async def refresh_sample(self) -> None:
        if self.collecting:
            return
        self.collecting = True
        try:
            snap = await asyncio.get_running_loop().run_in_executor(
                self.executor, self.collector.collect
            )
            self.snapshot = snap
            self.apply_requested_diagnostics(snap)
            if self.health_tracking and time.time() - self.smart_check_at >= 300:
                self.read_drive_health()
            # Keep full processes for the interactive table, but bound the
            # diagnostic evidence to the heaviest processes in each interval.
            sections = dict(snap.sections)
            processes = snap.sections.get("processes", Section())
            items = processes.data.get("items", [])
            tracked = {
                (item.get("pid"), item.get("create_time")): item
                for item in [
                    *sorted(items, key=lambda p: p.get("memory_bytes") or 0, reverse=True)[:64],
                    *sorted(items, key=lambda p: p.get("cpu_percent") or 0, reverse=True)[:32],
                ]
            }
            sections["processes"] = Section(
                {**processes.data, "items": list(tracked.values())},
                processes.unavailable,
                processes.error,
            )
            self.samples.append(Snapshot(snap.timestamp, snap.platform, sections))
            process_window = max(
                75, self.config.sustained_seconds + 3 * self.config.refresh_seconds
            )
            for old in self.samples:
                if snap.timestamp - old.timestamp > process_window:
                    old.sections["processes"] = Section({"items": [], "history_trimmed": True})
            if (
                self.config.record_history
                and snap.timestamp - self.last_record >= self.config.history_interval_seconds
            ):
                try:
                    if self.history is None:
                        self.history = HistoryStore(
                            self.history_path, retention_days=self.config.retention_days
                        )
                    await asyncio.get_running_loop().run_in_executor(
                        self.history_executor, self.history.record, snap
                    )
                    self.last_record = snap.timestamp
                except (HistoryError, OSError) as exc:
                    self.config = replace(self.config, record_history=False)
                    self.query_one("#record-switch", Switch).value = False
                    self.notify(f"Recording paused: {exc}", severity="error", timeout=15)
            self.render_snapshot()
        except Exception as exc:
            self.notify(
                f"Collection failed ({type(exc).__name__}); press R to retry.", severity="error"
            )
        finally:
            self.collecting = False

    def data(self, key: str) -> dict[str, Any]:
        return self.snapshot.sections.get(key, Section()).data if self.snapshot else {}

    def series(self, section: str, field: str) -> list[float]:
        return [
            float(s.sections[section].data[field])
            for s in self.samples
            if section in s.sections
            and isinstance(s.sections[section].data.get(field), (int, float))
        ][-120:]

    def render_snapshot(self) -> None:
        if self.snapshot is None:
            return
        cpu, memory, storage, network = (
            self.data(key) for key in ("cpu", "memory", "storage", "network")
        )
        self.query_one("#status", Static).update(
            Text(
                f"{self.snapshot.platform.upper()} / {time.strftime('%H:%M:%S')}\nHISTORY {'RECORDING' if self.config.record_history else 'PAUSED'} / {len(self.samples)} SAMPLES"
            )
        )
        self.query_one("#cpu-card", MetricCard).update_metric(
            number(cpu.get("percent"), "%"),
            f"{cpu.get('logical_cores', '?')} threads / {number(cpu.get('freq_mhz'), ' MHz', 0)}",
            self.series("cpu", "percent"),
        )
        self.query_one("#memory-card", MetricCard).update_metric(
            number(memory.get("percent"), "%"),
            f"Used {size(memory.get('used'))} / {size(memory.get('total'))}",
            self.series("memory", "percent"),
        )
        self.query_one("#storage-card", MetricCard).update_metric(
            f"{size(storage.get('read_bps'))}/s",
            f"Read / write {size(storage.get('write_bps'))}/s",
            self.series("storage", "read_bps"),
        )
        self.query_one("#network-card", MetricCard).update_metric(
            f"↓ {size(network.get('download_bps'))}/s",
            f"↑ {size(network.get('upload_bps'))}/s · all interfaces",
            self.series("network", "download_bps"),
        )
        for key, _ in PAGES[1:8]:
            if key != self.page:
                continue
            section = self.snapshot.sections.get(
                key, Section(unavailable=["Collector did not return this section"])
            )
            summary = self.section_summary(key)
            if section.error:
                summary += f"\nCollection issue: {section.error}"
            if section.unavailable:
                summary += "\nUnavailable: " + "; ".join(section.unavailable)
            self.query_one(f"#summary-{key}", Static).update(Text(summary))
            notes = (
                self.section_notes(key)
                + "\n\n"
                + json.dumps(section.data, indent=2, ensure_ascii=False, default=str)
            )
            self.query_one(f"#details-{key}", Static).update(Text(notes))
            self.update_table(key)
        for key, field in [("cpu", "percent"), ("memory", "percent"), ("network", "download_bps")]:
            self.query_one(f"#graph-{key}", Sparkline).data = self.series(key, field)
        gpu_values = []
        for sample in self.samples:
            readings = [
                gpu.get("utilization_percent")
                for gpu in sample.sections.get("gpu", Section()).data.get("items", [])
            ]
            valid = [float(value) for value in readings if isinstance(value, (int, float))]
            if valid:
                gpu_values.append(sum(valid) / len(valid))
        self.query_one("#graph-gpu", Sparkline).data = gpu_values[-120:]
        if self.page == "diagnostics" or time.monotonic() - self.last_analysis >= 5:
            self.render_findings()
        self.render_advanced()

    def render_advanced(self) -> None:
        if self.snapshot is None:
            return
        if self.page == "hardware":
            self.query_one("#hardware-art", Static).update(
                Text(
                    hardware_map(self.snapshot, max(24, self.query_one("#pages").size.width - 6)),
                    no_wrap=True,
                )
            )
        if self.page == "monitoring":
            table = self.query_one("#disk-activity-table", DataTable)
            table.clear()
            for row in self.data("storage").get("disk_activity", []):
                table.add_row(
                    Text(str(row.get("device"))),
                    size(row.get("read_bps")),
                    size(row.get("write_bps")),
                    number(row.get("read_latency_ms")),
                    number(row.get("write_latency_ms")),
                    number(row.get("read_iops")),
                    number(row.get("write_iops")),
                    number(row.get("queue_depth")),
                )
            self.query_one("#disk-activity-note", Static).update(
                Text(
                    self.data("storage").get(
                        "disk_activity_note", "Per-device counters unavailable."
                    )
                )
            )
            events = self.query_one("#event-table", DataTable)
            events.clear()
            for event in reversed(self.data("storage").get("events", [])[-100:]):
                events.add_row(
                    time.strftime("%H:%M:%S", time.localtime(event["timestamp"])),
                    Text(event["event"]),
                    Text(str(event.get("device"))),
                    Text(str(event.get("model"))),
                    Text(str(event.get("identity_quality"))),
                )
            thermal = self.data("thermal")
            lines = [f"Source: {thermal.get('source', 'Unavailable')}"]
            for sensor in thermal.get("sensors", []):
                lines.append(
                    f"{sensor.get('label')}: {number(sensor.get('temperature_c'), ' C')} / high {number(sensor.get('high_c'), ' C')} / critical {number(sensor.get('critical_c'), ' C')}"
                )
            throttle = thermal.get("throttling", {})
            lines.append(throttle.get("note", "Thermal throttle counters unavailable."))
            for name, value in throttle.get("increments", {}).items():
                lines.append(f"Observed thermal events: {name} +{value}")
            lines.extend(self.snapshot.sections.get("thermal", Section()).unavailable)
            self.query_one("#thermal-result", Static).update(Text("\n".join(lines)))
            trend_lines = [
                f"{'TRACKING / LOCAL STORAGE' if self.health_tracking else 'MANUAL READS / THIS SESSION'} / {len(self.health_trends.points)} successful health points"
            ]
            for point in list(self.health_trends.points)[-12:]:
                trend_lines.append(
                    f"{time.strftime('%m-%d %H:%M', time.localtime(point['timestamp']))} / {point['identity'][:8]} / "
                    + " / ".join(f"{k}={v:g}" for k, v in point["metrics"].items())
                )
            for alert in self.health_trends.alerts:
                trend_lines.append(
                    f"CHANGE / {alert['device']} / {alert['metric']}: {alert['before']:g} -> {alert['after']:g}"
                )
            trend_lines.append(
                "Only successful SMART reads are compared. Temperature variation is not a deterioration alert. Weak drive identities may not distinguish a replacement of the same model/size."
            )
            self.query_one("#health-trend-result", Static).update(Text("\n".join(trend_lines)))
        if self.page == "processes":
            traffic = self.snapshot.sections.get("process_traffic", Section())
            text = traffic.data.get("scope", "TCP tracing off or unsupported.")
            if traffic.error:
                text += "\n" + traffic.error
            text += "\n" + "; ".join(traffic.unavailable)
            text += "\n" + self.data("processes").get(
                "io_basis", "Process I/O counters unavailable."
            )
            self.query_one("#traffic-state", Static).update(Text(text))
        if self.stability:
            result = self.stability.summary()
            self.query_one("#stability-result", Static).update(
                Text(
                    f"{result['state'].upper()} / {result['target']}:{result['port']} / {result['attempts']} probes\nFailures {result['failures']} / {number(result['failure_percent'], '%')} / DNS failures {result['dns_failures']}\nMedian {number(result['median_ms'], ' ms')} / p95 {number(result['p95_ms'], ' ms')} / jitter {number(result['jitter_ms'], ' ms')}\n{result['note']}"
                )
            )
            self.query_one("#stability-graph", Sparkline).data = [
                s["latency_ms"] for s in result["samples"] if s["success"]
            ][-120:]

    @work(group="stability", exit_on_error=False)
    async def start_stability(self) -> None:
        if self.stability and self.stability.state == "running":
            return
        try:
            self.stability = StabilityTest(
                self.query_one("#network-target", Input).value.strip(),
                int(self.query_one("#stability-port", Input).value),
                float(self.query_one("#stability-duration", Input).value),
            )
            self.query_one("#stability-start", Button).disabled = True
            self.query_one("#stability-stop", Button).disabled = False
            result = await asyncio.to_thread(self.stability.run)
            if self.snapshot:
                self.snapshot.sections.setdefault("network", Section()).data["stability_test"] = (
                    result
                )
            self.render_advanced()
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
        finally:
            self.query_one("#stability-start", Button).disabled = False
            self.query_one("#stability-stop", Button).disabled = True

    @work(group="comparison", exclusive=True, exit_on_error=False)
    async def capture_comparison(self, action: str) -> None:
        if self.snapshot is None:
            return
        try:
            if action == "snapshot-load":
                path = Path(self.query_one("#snapshot-path", Input).value).expanduser()
                self.baseline = await asyncio.to_thread(load_snapshot, path)
            else:
                label = "before" if action == "snapshot-before" else "after"
                snap = copy.deepcopy(self.snapshot)
                path = default_data_dir() / "snapshots" / f"{label}-{time.time_ns()}.json"
                await asyncio.to_thread(save_snapshot, snap, path)
                if label == "before":
                    self.baseline = snapshot_payload(snap)
                elif self.baseline:
                    result = compare_snapshots(self.baseline, snapshot_payload(snap))
                    table = self.query_one("#compare-table", DataTable)
                    table.clear()
                    for row in result["measurements"]:
                        table.add_row(
                            row["metric"],
                            number(row["before"]),
                            number(row["after"]),
                            number(row["change"]),
                            row["unit"],
                        )
                    comparison_path = path.with_name(path.stem + "-comparison.json")
                    await asyncio.to_thread(
                        comparison_path.write_text, json.dumps(result, indent=2), encoding="utf-8"
                    )
                    self.query_one("#compare-result", Static).update(
                        Text(
                            f"Saved after: {path}\nComparison: {comparison_path}\n{result['elapsed_seconds']:.1f} seconds between observations.\n"
                            + (
                                "PLATFORM CHANGED; compare the same computer.\n"
                                if result["platform_changed"]
                                else ""
                            )
                            + "\n".join(
                                f"{item['kind']}: +{item['added']} / -{item['removed']} inventory identities"
                                for item in result["inventory_changes"]
                            )
                            + "\n"
                            + "\n".join(
                                f"{item['field']}: {item['before']} -> {item['after']}"
                                for item in result["metadata_changes"]
                            )
                            + f"\n{result['note']}"
                        )
                    )
                    return
                else:
                    self.notify(
                        "Capture or load a baseline first. The after snapshot was saved.",
                        severity="warning",
                    )
            self.query_one("#compare-result", Static).update(
                Text(
                    f"Baseline: {path}\nReady for an after capture."
                    if self.baseline
                    else f"Saved: {path}"
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify(f"Snapshot operation failed: {exc}", severity="error")

    def section_summary(self, key: str) -> str:
        data = self.data(key)
        if key == "cpu":
            return f"{data.get('model', 'CPU model unavailable')}\n{number(data.get('percent'), '%')} total · {data.get('physical_cores', '?')} cores / {data.get('logical_cores', '?')} threads · Temperature {number(data.get('temperature_c'), ' °C')}"
        if key == "memory":
            return f"{size(data.get('used'))} used · {size(data.get('available'))} available · {size(data.get('total'))} physical RAM\nSwap/pagefile {size(data.get('swap_used'))} / {size(data.get('swap_total'))}"
        if key == "storage":
            return f"{len(data.get('drives', []))} physical drives · {len(data.get('volumes', []))} mounted filesystems\nRead {size(data.get('read_bps'))}/s · write {size(data.get('write_bps'))}/s"
        if key == "network":
            return f"↓ {size(data.get('download_bps'))}/s · ↑ {size(data.get('upload_bps'))}/s\n{len(data.get('interfaces', []))} interfaces · {len(data.get('connections', []))} accessible connections"
        if key == "gpu":
            return f"{len(data.get('items', []))} graphics adapters detected. Unavailable telemetry remains blank."
        if key == "battery":
            if data.get("percent") is None:
                return "DESKTOP POWER\nNo battery charge measurement available. See details for the platform's power information."
            return f"{number(data.get('percent'), '%')} · {'Plugged in' if data.get('plugged') else 'On battery'}\nRuntime estimate: {number(data.get('seconds_left'), ' seconds', 0)} · Wear {number(data.get('wear_percent'), '%')}"
        return f"{len(data.get('items', []))} processes · CPU uses percent of one logical core; it may exceed 100%.\nClick a column to sort · Enter inspect · X request termination"

    @staticmethod
    def section_notes(key: str) -> str:
        notes = {
            "cpu": "CPU temperatures require platform sensor access. Frequency or temperature alone does not prove throttling. Graphs use the latest 120 measurements.",
            "memory": "Operating systems use spare RAM for cache. Available memory is more useful than unused memory; high usage alone does not prove a leak.",
            "storage": "Physical devices and mounted filesystems are separate. SMART is optional and is not a guarantee against failure. Titan never initializes, formats, repairs or mounts drives. Preserve unrecovered data first.",
            "gpu": "Vendor tools and driver support determine telemetry. NVIDIA uses nvidia-smi; Linux may expose AMD/Intel metrics through sysfs. Adapter detection does not imply all sensors are available.",
            "network": "Rates are local counter deltas, not internet speed-test results. Tests contact only the endpoint you enter. Connection visibility depends on permissions.",
            "battery": "Charge and runtime use psutil. Capacity, wear and cycles use platform battery records where accessible. Runtime is an estimate, not a guaranteed lifetime.",
            "processes": "Executable paths and start times require permissions. Titan confirms termination, rechecks PID and start time and protects known critical processes. Unsaved work in a terminated application can be lost.",
        }
        return notes.get(key, "")

    def update_table(self, key: str) -> None:
        data = self.data(key)
        rows: list[tuple[str, list[Any]]] = []
        if key == "cpu":
            rows = (
                [
                    (str(i), [f"Core {i}", value])
                    for i, value in enumerate(data.get("per_core") or [])
                ]
                if self.per_core_mode
                else [("aggregate", ["All cores", data.get("percent")])]
            )
        elif key in {"memory", "processes"}:
            query = (
                self.query_one("#process-filter", Input).value.lower() if key == "processes" else ""
            )
            items = self.data("processes").get("items", [])
            if key == "memory":
                items = sorted(items, key=lambda p: p.get("memory_bytes") or 0, reverse=True)[:30]
            self.process_rows = (
                {f"{p['pid']}:{p.get('create_time')}": p for p in items}
                if key == "processes"
                else self.process_rows
            )
            for p in items:
                if (
                    query
                    and query not in str(p.get("name", "")).lower()
                    and query not in str(p["pid"])
                ):
                    continue
                values = [
                    p["pid"],
                    p.get("name", "?"),
                    p.get("cpu_percent"),
                    p.get("memory_bytes"),
                    p.get("memory_percent"),
                    p.get("io_read_bps"),
                    p.get("io_write_bps"),
                    p.get("download_bps"),
                    p.get("upload_bps"),
                ]
                if key == "memory":
                    values = [
                        p["pid"],
                        p.get("name", "?"),
                        p.get("memory_bytes"),
                        p.get("memory_percent"),
                    ]
                rows.append((f"{p['pid']}:{p.get('create_time')}", values))
        elif key == "gpu":
            rows = [
                (
                    str(i),
                    [
                        p.get("name", "?"),
                        p.get("utilization_percent"),
                        p.get("memory_used_bytes"),
                        p.get("temperature_c"),
                        p.get("power_w"),
                    ],
                )
                for i, p in enumerate(data.get("items", []))
            ]
        elif key == "storage":
            for i, d in enumerate(data.get("drives", [])):
                health = d.get("health") or {}
                status = "Health unavailable"
                if isinstance(health, dict):
                    status = health.get("status", "Health unavailable")
                rows.append(
                    (
                        f"drive-{i}",
                        [
                            "Physical",
                            d.get("model") or d.get("name") or d.get("id"),
                            d.get("size"),
                            None,
                            status,
                        ],
                    )
                )
            for i, d in enumerate(data.get("volumes", [])):
                rows.append(
                    (
                        f"vol-{i}",
                        [
                            "Mounted",
                            d.get("mountpoint"),
                            d.get("total"),
                            d.get("percent"),
                            d.get("filesystem"),
                        ],
                    )
                )
        elif key == "network":
            rows = [
                (
                    str(i),
                    [
                        d.get("name"),
                        "Unavailable"
                        if d.get("is_up") is None
                        else "Up"
                        if d.get("is_up")
                        else "Down",
                        d.get("download_bps"),
                        d.get("upload_bps"),
                        d.get("speed_mbps"),
                    ],
                )
                for i, d in enumerate(data.get("interfaces", []))
            ]
        elif key == "battery":
            rows = [
                (str(i), [k.replace("_", " ").title(), str(v) if v is not None else "Unavailable"])
                for i, (k, v) in enumerate(data.items())
                if not isinstance(v, (dict, list))
            ]
        table = self.query_one(f"#table-{key}", DataTable)
        selected = None
        if table.row_count and table.cursor_row < table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        column = int(self.sort_column.get(key, "2" if key == "processes" else "0"))
        reverse = self.sort_reverse.get(key, key == "processes")
        rows.sort(
            key=lambda row: (
                row[1][column] is not None,
                row[1][column]
                if isinstance(row[1][column], (int, float))
                else str(row[1][column] or "").lower(),
            ),
            reverse=reverse,
        )
        table.clear()
        for row_key, values in rows:
            formatted = []
            for i, value in enumerate(values):
                bytes_column = (
                    (key == "processes" and i in {3, 5, 6, 7, 8})
                    or (key == "memory" and i == 2)
                    or (key == "storage" and i == 2)
                    or (key == "gpu" and i == 2)
                    or (key == "network" and i in {2, 3})
                )
                formatted.append(
                    Text(
                        size(value)
                        if bytes_column
                        else number(value)
                        if isinstance(value, float)
                        else "Unavailable"
                        if value is None
                        else str(value)
                    )
                )
            table.add_row(*formatted, key=row_key)
        if selected:
            for i, (row_key, _) in enumerate(rows):
                if row_key == selected:
                    table.move_cursor(row=i, animate=False)
                    break

    @on(DataTable.HeaderSelected)
    def sort_table(self, event: DataTable.HeaderSelected) -> None:
        key = (event.data_table.id or "").removeprefix("table-")
        if key in {p for p, _ in PAGES[1:8]}:
            column = event.column_key.value or "0"
            self.sort_reverse[key] = (
                not self.sort_reverse.get(key, False)
                if self.sort_column.get(key) == column
                else True
            )
            self.sort_column[key] = column
            self.update_table(key)

    @on(Input.Changed, "#process-filter")
    def filter_processes(self) -> None:
        if self.snapshot:
            self.update_table("processes")

    @on(DataTable.RowSelected, "#table-processes")
    def inspect_selected_row(self) -> None:
        self.action_inspect()

    def render_findings(self) -> None:
        self.last_analysis = time.monotonic()
        findings = evaluate(list(self.samples), self.config)
        text = Text()
        if not findings:
            text.append("No rule findings in the available evidence.\n", style="bold")
            text.append(
                f"Observing {len(self.samples)} samples. Sustained rules require {self.config.sustained_seconds:g} seconds of evidence. Missing sensors are not evidence of health."
            )
        for finding in findings:
            colors = {"critical": "#FB7185", "warning": "#FBBF24", "attention": "#38BDF8"}
            text.append(
                f"{finding.severity.upper()} / {finding.title}\n",
                style=f"bold {colors.get(finding.severity.lower(), '#94A3B8')}",
            )
            text.append(
                f"Evidence: {finding.evidence}\n{finding.explanation}\nPossible causes: {', '.join(finding.possible_causes)}\nNext: {finding.next_step}\nConfidence: {finding.confidence} · Elevated access: {'required' if finding.requires_admin else 'not required'}\n\n"
            )
        self.query_one("#diagnostic-list", Static).update(text)
        overview = Text("[ EVIDENCE LOG ]\n", style="bold")
        overview.append(
            "\n".join(f"{f.severity.upper()} · {f.title}" for f in findings[:4])
            or "No findings in current evidence · open Diagnostics for coverage and limitations.",
            style="",
        )
        self.query_one("#overview-insights", Static).update(overview)

    def action_diagnose(self) -> None:
        self.action_navigate("diagnostics")
        self.render_findings()
        self.read_drive_health()

    def apply_requested_diagnostics(self, snapshot: Snapshot) -> None:
        storage = snapshot.sections.get("storage")
        if storage and time.time() - self.smart_check_at < 60:
            for drive in storage.data.get("drives", []):
                identity = (drive_identity(drive), str(drive.get("device")))
                if identity in self.drive_health:
                    drive["health"] = copy.deepcopy(self.drive_health[identity])
            storage.data["health_trend_alerts"] = copy.deepcopy(self.health_trends.alerts)
        if self.stability:
            snapshot.sections.setdefault("network", Section()).data["stability_test"] = (
                self.stability.summary()
            )
        network = snapshot.sections.get("network")
        if (
            network
            and self.requested_network_test
            and time.time() - self.requested_network_test.get("timestamp", 0) < 300
        ):
            network.data["diagnostic_test"] = copy.deepcopy(self.requested_network_test)

    @work(group="smart", exit_on_error=False)
    async def read_drive_health(self) -> None:
        if self.smart_collecting:
            return
        if not self.snapshot or "storage" not in self.snapshot.sections:
            return
        if time.time() - self.smart_check_at < 60:
            return
        section = copy.deepcopy(self.snapshot.sections["storage"])
        if not section.data.get("drives"):
            self.notify(
                "Physical drive inventory is unavailable; see Storage for the reason.",
                severity="warning",
            )
            return
        self.notify("Reading optional drive health…")
        self.smart_collecting = True
        try:
            await asyncio.to_thread(add_smart_health, section)
            self.smart_check_at = time.time()
            try:
                await asyncio.to_thread(
                    self.health_trends.record, section, persist=self.health_tracking
                )
            except HealthTrendError as exc:
                self.health_tracking = False
                self.query_one("#health-track-switch", Switch).value = False
                self.notify(str(exc), severity="error")
            self.drive_health = {
                (drive_identity(drive), str(drive.get("device"))): {
                    **drive["health"],
                    "collected_at": self.smart_check_at,
                }
                for drive in section.data.get("drives", [])
                if isinstance(drive.get("health"), dict)
            }
            if self.snapshot:
                self.apply_requested_diagnostics(self.snapshot)
                for reason in section.unavailable:
                    if reason not in self.snapshot.sections["storage"].unavailable:
                        self.snapshot.sections["storage"].unavailable.append(reason)
                self.render_snapshot()
            self.notify("Drive-health check finished; unavailable metrics remain identified.")
        except Exception as exc:
            self.notify(f"Drive-health query failed ({type(exc).__name__})", severity="warning")
        finally:
            self.smart_collecting = False

    def action_help(self) -> None:
        self.push_screen(
            InfoDialog(
                f"Titan help / {dict(PAGES)[self.page]}",
                "1–9: monitoring pages\n/: search pages and processes\nCtrl+P: command palette\nR: refresh   D: diagnose   Q: quit\nTab / Shift+Tab: move focus   Enter: activate\nClick table headers to sort. Scroll for more data.\nProcesses: Enter inspects; X opens termination confirmation.\nSettings: themes preview immediately; save persists them.\nHistory is off until you choose to record it.\nStorage checks are read-only. Optional sensors may need vendor tools or permissions.\n\n"
                + self.section_notes(self.page),
            )
        )

    def action_search(self) -> None:
        self.push_screen(SearchDialog(), self.search_result)

    def search_result(self, result: tuple[str, str] | None) -> None:
        if result:
            self.action_navigate(result[0])
            if result[0] == "processes":
                self.query_one("#process-filter", Input).value = result[1]

    def selected_process(self) -> dict[str, Any] | None:
        if self.page != "processes":
            return None
        table = self.query_one("#table-processes", DataTable)
        if not table.row_count:
            return None
        return self.process_rows.get(
            str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        )

    @work(group="inspect", exit_on_error=False)
    async def action_inspect(self) -> None:
        process = self.selected_process()
        if process:
            try:
                details = await asyncio.to_thread(
                    inspect_process, process["pid"], process["create_time"]
                )
                self.push_screen(
                    InfoDialog(
                        f"{process['name']} / PID {process['pid']}",
                        json.dumps(details, indent=2, default=str),
                    )
                )
            except Exception as exc:
                self.notify(str(exc), severity="warning")

    def action_terminate(self) -> None:
        process = self.selected_process()
        if process:

            def handle_confirmation(confirmed: bool | None) -> None:
                self.confirm_terminate(dict(process), bool(confirmed))

            self.push_screen(
                InfoDialog(
                    "Confirm process termination",
                    f"{process['name']} / PID {process['pid']}\n\nTermination can lose unsaved work. Titan revalidates this process identity and refuses protected processes.\n\nStart time: {process.get('create_time')}",
                    confirm=True,
                ),
                handle_confirmation,
            )

    @work(group="terminate", exit_on_error=False)
    async def confirm_terminate(self, process: dict[str, Any], confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            await asyncio.to_thread(
                terminate_process, process["pid"], process["create_time"], confirmed=True
            )
            self.notify("Termination requested")
            self.refresh_sample()
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=10)

    @work(group="network", exit_on_error=False)
    async def network_test(self) -> None:
        if self.network_testing:
            self.notify("The selected endpoint test is still running")
            return
        target = self.query_one("#network-target", Input).value.strip()
        self.network_testing = True
        self.query_one("#network-result", Static).update("Testing selected endpoint…")
        try:
            result = await asyncio.to_thread(network_test, target)
            success = (
                result.data.get("dns", {}).get("success") is True
                and result.data.get("tcp", {}).get("success") is True
            )
            self.requested_network_test = {
                **result.data,
                "success": success,
                "error": result.error or result.data.get("tcp", {}).get("error"),
            }
            if self.snapshot:
                self.apply_requested_diagnostics(self.snapshot)
                self.render_findings()
            self.query_one("#network-result", Static).update(
                Text(
                    json.dumps(result.data, indent=2, default=str)
                    + "\n"
                    + "; ".join(result.unavailable)
                )
            )
        except Exception as exc:
            self.query_one("#network-result", Static).update(Text(str(exc)))
        finally:
            self.network_testing = False

    @work(group="history", exclusive=True, exit_on_error=False)
    async def show_history(self, span: int) -> None:
        try:
            if span == 0:
                rows = [
                    {
                        "timestamp": s.timestamp,
                        "cpu_percent": s.sections.get("cpu", Section()).data.get("percent"),
                        "memory_percent": s.sections.get("memory", Section()).data.get("percent"),
                        "download_bps": s.sections.get("network", Section()).data.get(
                            "download_bps"
                        ),
                        "upload_bps": s.sections.get("network", Section()).data.get("upload_bps"),
                    }
                    for s in self.samples
                ]
            else:
                if self.history is None:
                    self.history = HistoryStore(
                        self.history_path, retention_days=self.config.retention_days
                    )
                rows = await asyncio.get_running_loop().run_in_executor(
                    self.history_executor,
                    partial(
                        self.history.query,
                        start=time.time() - span,
                        end=time.time(),
                        bucket_seconds=max(1, span // 240),
                    ),
                )
            table = self.query_one("#history-table", DataTable)
            table.clear()
            for row in rows[-240:]:
                table.add_row(
                    time.strftime("%m-%d %H:%M:%S", time.localtime(row["timestamp"])),
                    number(row.get("cpu_percent")),
                    number(row.get("memory_percent")),
                    size(row.get("network_download_bps", row.get("download_bps"))),
                    size(row.get("network_upload_bps", row.get("upload_bps"))),
                )
            self.query_one("#history-graph", Sparkline).data = [
                float(row.get("cpu_percent") or 0)
                for row in rows
                if row.get("cpu_percent") is not None
            ][-240:]
            self.query_one("#record-state", Static).update(
                f"{'Recording' if self.config.record_history else 'Paused'} · {len(rows)} records in range · CPU graph"
                if rows
                else "No recorded data in this range. Enable recording in Settings to start collecting history."
            )
        except (HistoryError, OSError) as exc:
            self.query_one("#record-state", Static).update(Text(str(exc)))

    @work(group="export", exit_on_error=False)
    async def export(self, format_name: str, process_only: bool = False) -> None:
        if self.snapshot is None:
            self.notify("Wait for the first measurement", severity="warning")
            return
        try:
            if process_only:
                report = await asyncio.to_thread(export_processes, self.snapshot)
            else:
                report = await asyncio.to_thread(
                    export_report,
                    self.snapshot,
                    evaluate(list(self.samples), self.config),
                    format_name,
                )
            directory = default_data_dir() / "reports"
            directory.mkdir(parents=True, exist_ok=True)
            suffix = "txt" if format_name == "text" else format_name
            path = (
                directory
                / f"titan-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06}.{suffix}"
            )
            await asyncio.to_thread(path.write_text, report, encoding="utf-8")
            self.query_one("#report-result", Static).update(
                Text(f"Saved redacted report:\n{path}\nReview before sharing.")
            )
            self.notify(f"Report saved: {path}", timeout=10)
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")

    def get_system_commands(self, screen: Screen) -> Any:
        yield from super().get_system_commands(screen)
        for key, label in PAGES:
            yield SystemCommand(
                f"Open {label}", "Navigate to this workspace", partial(self.action_navigate, key)
            )
        yield SystemCommand(
            "Analyze system evidence", "Run offline diagnostic rules", self.action_diagnose
        )
        yield SystemCommand(
            "Export redacted HTML report",
            "Save an offline report locally",
            partial(self.export, "html"),
        )

    async def on_unmount(self) -> None:
        if self.stability:
            self.stability.stop()
        traffic = getattr(self.collector, "traffic", None)
        if traffic:
            traffic.enabled = False
        if hasattr(self, "timer"):
            self.timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.history:
            await asyncio.get_running_loop().run_in_executor(
                self.history_executor, self.history.close
            )
        self.history_executor.shutdown(wait=False, cancel_futures=True)
