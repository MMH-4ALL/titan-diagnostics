import time

from textual.widgets import Button, ContentSwitcher, Select, Static

from titan.app import TitanApp
from titan.core.config import THEMES, Config, load_config
from titan.core.models import Section, Snapshot


class Collector:
    def collect(self):
        return Snapshot(
            time.time(),
            "Test platform",
            {
                "cpu": Section({"percent": 20, "model": "Test CPU"}),
                "memory": Section({"percent": 40, "total": 1024**3}),
                "storage": Section({"drives": [], "volumes": []}),
                "processes": Section({"items": []}),
                "network": Section({"interfaces": []}),
            },
        )


async def test_themes_click_apply_save_and_restore(tmp_path):
    path = tmp_path / "config.toml"
    app = TitanApp(Collector(), Config(), path, tmp_path / "history.db")
    async with app.run_test(size=(160, 55)) as pilot:
        await pilot.pause()
        app.timer.stop()
        app.query_one("#nav-themes", Button).scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#nav-themes")
        await pilot.pause()
        assert app.page == "themes"
        assert app.query_one(ContentSwitcher).current == "themes"
        assert len(THEMES) == 8
        assert {button.id for button in app.query(".theme-choice")} == {
            f"theme-{name}" for name in THEMES
        }
        for name in THEMES:
            button = app.query_one(f"#theme-{name}", Button)
            button.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(button)
            await pilot.pause()
            assert app.theme == app.config.theme == name
            assert app.query_one("#theme-select", Select).value == name
            assert load_config(path).theme == name
            assert button.has_class("active")
            assert len(app.query(".theme-choice.active")) == 1
            assert "[ ACTIVE ]" in str(app.query_one("#theme-current", Static).render())

    restored = TitanApp(Collector(), load_config(path), path, tmp_path / "history.db")
    async with restored.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        assert restored.theme == restored.config.theme == "paper"
        assert restored.query_one("#theme-select", Select).value == "paper"


async def test_themes_keyboard_after_narrow_resize(tmp_path):
    path = tmp_path / "config.toml"
    app = TitanApp(Collector(), Config(), path, tmp_path / "history.db")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.timer.stop()
        await pilot.resize_terminal(40, 16)
        await pilot.pause()
        assert not app.query_one("#sidebar").display
        await pilot.press("slash", "t", "h", "e", "m", "e", "s", "enter")
        await pilot.pause()
        assert app.page == "themes"
        paper = app.query_one("#theme-paper", Button)
        paper.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert paper.visible and paper.region.overlaps(app.screen.region)
        assert app.theme == app.config.theme == load_config(path).theme == "paper"
        app.action_navigate("settings")
        await pilot.pause()
        app.query_one("#theme-select", Select).value = "ocean"
        await pilot.pause()
        assert app.theme == app.config.theme == "ocean"
        assert app.query_one("#theme-ocean", Button).has_class("active")
        assert "Ocean" in str(app.query_one("#theme-current", Static).render())
