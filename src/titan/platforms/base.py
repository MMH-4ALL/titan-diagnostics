"""The platform boundary never modifies storage or requests elevation."""

from __future__ import annotations

import platform
from typing import Any, Protocol

from titan.core.models import Section


class SystemProvider(Protocol):
    def collect_system(self) -> Section: ...
    def collect_storage(self) -> Section: ...
    def collect_gpu(self) -> Section: ...
    def collect_battery_metadata(self) -> dict[str, Any]: ...


class UnsupportedProvider:
    def collect_system(self) -> Section:
        return Section(
            data={"os": platform.system(), "architecture": platform.machine()},
            unavailable=["Platform-specific inventory is unsupported"],
        )

    def collect_storage(self) -> Section:
        return Section(data={"drives": []}, unavailable=["Physical drive inventory"])

    def collect_gpu(self) -> Section:
        return Section(data={"items": []}, unavailable=["GPU inventory and sensors"])

    def collect_battery_metadata(self) -> dict[str, Any]:
        return {"batteries": [], "unavailable": ["Battery capacity and cycles"]}


def get_provider(system: str | None = None) -> SystemProvider:
    system = system or platform.system()
    if system == "Windows":
        from titan.platforms.windows import WindowsProvider

        return WindowsProvider()
    if system == "Linux":
        from titan.platforms.linux import LinuxProvider

        return LinuxProvider()
    return UnsupportedProvider()
