import time
from types import SimpleNamespace as NS

from textual.widgets import DataTable, Input, Static

from titan.app import TitanApp
from titan.core.config import Config
from titan.core.models import Section, Snapshot


class Collector:
    traffic = NS(enabled=False)

    def collect(self):
        return Snapshot(
            time.time(),
            "Test",
            {
                "cpu": Section({"percent": 20, "model": "Test CPU"}),
                "memory": Section({"percent": 40, "total": 1024**3}),
                "storage": Section(
                    {
                        "drives": [{"device": "/dev/test", "model": "Test disk"}],
                        "disk_activity": [{"device": "test", "read_latency_ms": 5}],
                        "events": [
                            {
                                "timestamp": time.time(),
                                "event": "present at start",
                                "device": "/dev/test",
                                "model": "Test disk",
                                "identity_quality": "Test",
                            }
                        ],
                    }
                ),
                "processes": Section({"items": []}),
                "network": Section({"interfaces": []}),
                "thermal": Section(
                    {
                        "sensors": [{"label": "Test CPU", "temperature_c": 50, "critical_c": 100}],
                        "throttling": {"increments": {}},
                    }
                ),
            },
        )


async def test_new_pages_capture_load_compare_and_resize(monkeypatch, tmp_path):
    monkeypatch.setattr("titan.app.default_data_dir", lambda: tmp_path)
    app = TitanApp(Collector(), Config(), tmp_path / "config.toml", tmp_path / "history.db")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.timer.stop()
        app.action_navigate("monitoring")
        await pilot.pause()
        assert app.query_one("#disk-activity-table", DataTable).row_count == 1
        assert app.query_one("#event-table", DataTable).row_count == 1
        assert "50.0 C" in str(app.query_one("#thermal-result", Static).render())
        app.action_navigate("hardware")
        await pilot.pause()
        assert "/dev/test" in str(app.query_one("#hardware-art", Static).render())
        app.action_navigate("compare")
        await pilot.pause()
        await pilot.click("#snapshot-before")
        await app.workers.wait_for_complete()
        assert app.baseline is not None
        path = next((tmp_path / "snapshots").glob("before-*.json"))
        app.snapshot.sections["cpu"].data["percent"] = 30
        await pilot.click("#snapshot-after")
        await app.workers.wait_for_complete()
        assert app.query_one("#compare-table", DataTable).row_count > 10
        assert len(list((tmp_path / "snapshots").glob("*-comparison.json"))) == 1
        app.query_one("#snapshot-path", Input).value = str(path)
        await pilot.click("#snapshot-load")
        await app.workers.wait_for_complete()
        assert app.baseline["snapshot"]["sections"]["cpu"]["data"]["percent"] == 20
        await pilot.resize_terminal(60, 26)
        app.action_navigate("hardware")
        await pilot.pause()
        assert app.has_class("small")


async def test_stability_controls_and_reports_keep_result(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "titan.hardware.stability.probe",
        lambda *_: {"success": True, "stage": "tcp", "latency_ms": 4},
    )
    app = TitanApp(Collector(), Config(), tmp_path / "config.toml", tmp_path / "history.db")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.timer.stop()
        app.action_navigate("network")
        app.query_one("#network-target", Input).value = "localhost"
        app.query_one("#stability-duration", Input).value = "1"
        app.start_stability()
        await app.workers.wait_for_complete()
        assert app.stability.summary()["state"] == "complete"
        assert app.stability.summary()["attempts"] == 1
        assert app.snapshot.sections["network"].data["stability_test"]["median_ms"] == 4
        app.refresh_sample()
        await app.workers.wait_for_complete()
        assert app.snapshot.sections["network"].data["stability_test"]["state"] == "complete"
