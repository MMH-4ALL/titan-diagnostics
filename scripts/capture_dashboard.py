"""Capture the actual app with real local measurements, never demonstration data."""

import argparse
import asyncio
from pathlib import Path

from titan.app import TitanApp
from titan.core.config import Config


async def capture(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    app = TitanApp(config=Config())
    async with app.run_test(size=(128, 42)) as pilot:
        for _ in range(60):
            await asyncio.sleep(0.5)
            if len(app.samples) >= 12:
                break
        await pilot.pause()
        if len(app.samples) < 2:
            raise RuntimeError("Real measurements were not collected; no capture was saved")
        app.save_screenshot("overview.svg", str(directory))
        for page in ("monitoring", "hardware", "compare"):
            app.action_navigate(page)
            await pilot.pause()
            app.save_screenshot(page + ".svg", str(directory))
        app.action_navigate("overview")
        await pilot.resize_terminal(100, 34)
        await pilot.pause()
        app.save_screenshot("overview-medium.svg", str(directory))
        await pilot.resize_terminal(60, 26)
        await pilot.pause()
        app.save_screenshot("overview-small.svg", str(directory))
        print(f"Captured actual application after {len(app.samples)} real observations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    asyncio.run(capture(arguments.directory))
