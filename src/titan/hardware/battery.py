"""Battery capacity metadata without estimating unsupported measurements."""

from __future__ import annotations

from typing import Any


def wear_percent(design: float | None, full: float | None) -> float | None:
    if design is None or full is None or design <= 0 or full <= 0:
        return None
    # Firmware may report a full capacity slightly above design; not negative wear.
    return round(max(0.0, (1.0 - full / design) * 100.0), 1)


def collect_battery_metadata() -> dict[str, Any]:
    from titan.platforms.base import get_provider

    return get_provider().collect_battery_metadata()
