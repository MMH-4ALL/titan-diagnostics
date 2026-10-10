import asyncio
import time

from textual.widgets import DataTable, Input

from titan.app import TitanApp
from titan.core.config import Config
from titan.core.models import Section, Snapshot


class ActionCollector:
    def collect(self):
        return Snapshot(
            time.time(),
            "Test",
            {
                "cpu": Section({"percent": 20.0, "per_core": [15.0, 25.0]}),
                "storage": Section(
                    {
                        "drives": [
                            {
                                "id": "disk0",
                                "device": "/dev/pd0",
                                "model": "Test disk",
                                "size": 1000,
                            }
                        ]
                    }
                ),
                "processes": Section(
                    {
                        "items": [
                            {
                                "pid": 123,
                                "name": "Test process",
                                "create_time": 1.0,
                                "memory_bytes": 1000,
                            }
                        ]
                    }
                ),
                "network": Section({"interfaces": []}),
            },
        )


async def test_drive_health_user_action_and_cache(monkeypatch, tmp_path):
    calls = []

    def health(section):
        calls.append(1)
        section.data["drives"][0]["health"] = {
            "status": "Warning reported",
            "reported_passed": False,
        }

    monkeypatch.setattr("titan.app.add_smart_health", health)
    app = TitanApp(ActionCollector(), Config(), tmp_path / "config.toml", tmp_path / "history.db")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not calls  # no automatic SMART queries
        await pilot.press("d")
        await pilot.pause()
        assert len(calls) == 1
        assert app.data("storage")["drives"][0]["health"]["reported_passed"] is False
        await pilot.press("r", "d")
        await pilot.pause()
        assert len(calls) == 1
        assert app.data("storage")["drives"][0]["health"]["reported_passed"] is False
        app.action_navigate("cpu")
        await pilot.pause()
        await pilot.click("#cpu-mode")
        await pilot.pause()
        assert app.query_one("#table-cpu", DataTable).row_count == 1
        await pilot.pause(0.5)
        await pilot.click("#cpu-mode")
        await pilot.pause()
        assert app.query_one("#table-cpu", DataTable).row_count == 2


async def test_process_csv_export_and_failed_selected_network_test(monkeypatch, tmp_path):
    monkeypatch.setattr("titan.app.default_data_dir", lambda: tmp_path)

    def selected_test(target):
        assert target == "localhost"
        return Section(
            {
                "target": target,
                "timestamp": time.time(),
                "dns": {"success": True},
                "tcp": {"success": False, "error": "TimeoutError"},
            }
        )

    monkeypatch.setattr("titan.app.network_test", selected_test)
    app = TitanApp(ActionCollector(), Config(), tmp_path / "config.toml", tmp_path / "history.db")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.action_navigate("processes")
        await pilot.pause()
        await pilot.click("#process-export")
        await app.workers.wait_for_complete()
        await pilot.pause()
        reports = list((tmp_path / "reports").glob("*.csv"))
        assert len(reports) == 1
        assert reports[0].read_text().startswith("pid,name,")
        app.action_navigate("network")
        app.query_one("#network-target", Input).value = "localhost"
        app.network_test()
        await pilot.pause()
        assert app.data("network")["diagnostic_test"]["success"] is False
        assert app.requested_network_test["error"] == "TimeoutError"
        await asyncio.sleep(0)
        assert not app.network_testing


async def test_enter_on_process_table_opens_inspection(monkeypatch, tmp_path):
    from titan.app import InfoDialog

    monkeypatch.setattr(
        "titan.app.inspect_process", lambda pid, created: {"pid": pid, "create_time": created}
    )
    app = TitanApp(ActionCollector(), Config(), tmp_path / "config.toml", tmp_path / "history.db")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("8", "enter")
        await pilot.pause()
        assert isinstance(app.screen, InfoDialog)
        assert "PID 123" in app.screen.dialog_title
