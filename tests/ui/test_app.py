import asyncio
import time
from dataclasses import replace

import pytest
from textual.command import CommandPalette
from textual.widgets import ContentSwitcher, DataTable, Input, Select

from titan.app import InfoDialog, TitanApp
from titan.core.config import Config
from titan.core.models import Section, Snapshot


class FakeCollector:
    def __init__(self):
        self.calls = 0

    def collect(self):
        self.calls += 1
        return Snapshot(
            time.time(),
            "Test platform",
            {
                "cpu": Section(
                    {
                        "percent": 24.0,
                        "per_core": [20.0, 28.0],
                        "logical_cores": 2,
                        "model": "Test CPU",
                    }
                ),
                "memory": Section(
                    {
                        "percent": 40.0,
                        "total": 1024**3,
                        "used": 400 * 1024**2,
                        "available": 624 * 1024**2,
                    }
                ),
                "processes": Section(
                    {
                        "items": [
                            {
                                "pid": 100,
                                "name": "Alpha",
                                "cpu_percent": 2.0,
                                "memory_bytes": 1000,
                                "memory_percent": 0.1,
                                "create_time": 100.0,
                            },
                            {
                                "pid": 101,
                                "name": "Beta",
                                "cpu_percent": 30.0,
                                "memory_bytes": 2000,
                                "memory_percent": 0.2,
                                "create_time": 101.0,
                            },
                        ]
                    }
                ),
                "storage": Section(
                    {"drives": [], "volumes": [], "read_bps": 123.0, "write_bps": 50.0},
                    unavailable=["SMART unavailable"],
                ),
                "network": Section({"interfaces": [], "download_bps": 100.0, "upload_bps": 20.0}),
                "gpu": Section({"items": []}, unavailable=["No supported adapter telemetry"]),
                "battery": Section({"percent": None}),
            },
        )


def make_app(tmp_path, collector=None):
    return TitanApp(
        collector=collector or FakeCollector(),
        config=Config(),
        config_path=tmp_path / "config.toml",
        history_path=tmp_path / "history.db",
    )


@pytest.mark.parametrize("dimensions", [(160, 50), (100, 34), (60, 26), (40, 16)])
async def test_navigation_resize_and_fallback(tmp_path, dimensions):
    app = make_app(tmp_path)
    async with app.run_test(size=dimensions) as pilot:
        await pilot.pause()
        await pilot.press("4")
        assert app.query_one(ContentSwitcher).current == "gpu"
        await pilot.press("8")
        assert app.query_one("#table-processes", DataTable).row_count == 2
        await pilot.press("9")
        assert app.page == "diagnostics"
        assert app.has_class("small") == (dimensions[0] < 80)
        await pilot.press("1")
        assert app.page == "overview"


async def test_input_does_not_trigger_shortcuts_and_search(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.press("q", "1", "2", "3", "r", "d")
        assert app.screen.query_one(Input).value == "q123rd"
        await pilot.press("escape")
        assert app.page == "overview"
        await pilot.press("slash")
        await pilot.press("b", "e", "t", "a", "enter")
        await pilot.pause()
        assert app.page == "processes"
        assert app.query_one("#table-processes", DataTable).row_count == 1
        assert app.query_one("#process-filter", Input).value == "beta"


async def test_palette_theme_sort_selection_and_confirmation(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
        await pilot.press("escape")
        app.action_navigate("settings")
        await pilot.pause(0.2)
        app.query_one("#theme-select", Select).value = "arctic"
        await pilot.pause(0.2)
        assert app.theme == "arctic"
        app.save_preferences()
        assert "arctic" in (tmp_path / "config.toml").read_text()
        app.action_navigate("processes")
        table = app.query_one("#table-processes", DataTable)
        assert app.selected_process()["pid"] == 101  # CPU descending
        app.sort_column["processes"] = "3"
        app.sort_reverse["processes"] = False
        app.update_table("processes")
        assert app.selected_process()["pid"] == 101  # identity preserved
        app.action_terminate()
        await pilot.pause()
        assert isinstance(app.screen, InfoDialog)
        assert "PID 101" in app.screen.body
        await pilot.press("escape")
        assert app.selected_process()["pid"] == 101
        table.move_cursor(row=0)


async def test_slow_collector_keeps_navigation_responsive(tmp_path):
    class SlowCollector(FakeCollector):
        def collect(self):
            time.sleep(0.2)
            return super().collect()

    app = make_app(tmp_path, SlowCollector())
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.press("2")
        assert app.page == "cpu"
        await asyncio.sleep(0.35)
        await pilot.pause()
        assert app.snapshot is not None


async def test_history_default_and_live_update(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not (tmp_path / "history.db").exists()
        before = len(app.samples)
        await pilot.press("r")
        await pilot.pause()
        assert len(app.samples) > before
        app.config = replace(app.config, record_history=True, history_interval_seconds=1)
        await pilot.press("r")
        await pilot.pause()
        assert (tmp_path / "history.db").exists()
        app.action_navigate("history")
        await pilot.pause()
        assert app.query_one("#history-table", DataTable).row_count > 0


async def test_collector_error_is_recoverable(tmp_path):
    class FailingCollector:
        def collect(self):
            raise PermissionError("sensitive path should not be displayed")

    app = make_app(tmp_path, FailingCollector())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.snapshot is None
        await pilot.press("9", "1")
        assert app.page == "overview"


async def test_running_app_resizes_and_restores_sidebar(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert app.query_one("#sidebar").display
        await pilot.resize_terminal(60, 26)
        await pilot.pause()
        assert app.has_class("small")
        assert not app.query_one("#sidebar").display
        assert app.query_one("#metrics").styles.grid_size_columns == 1
        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert app.has_class("medium")
        assert app.query_one("#sidebar").display
        assert app.query_one("#metrics").styles.grid_size_columns == 1
        await pilot.resize_terminal(100, 34)
        await pilot.pause()
        assert app.has_class("medium")
        assert app.query_one("#sidebar").display
        assert app.query_one("#metrics").styles.grid_size_columns == 2
        await pilot.resize_terminal(160, 50)
        await pilot.pause()
        assert not app.has_class("small") and not app.has_class("medium")
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.has_class("compact-header")
        assert not app.query_one("#logo").display
        assert app.query_one("#compact-logo").display
        await pilot.resize_terminal(160, 50)
        await pilot.pause()
        assert not app.has_class("compact-header")
        assert app.query_one("#logo").display
