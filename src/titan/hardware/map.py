"""Terminal-safe ASCII inventory map. Links show inventory membership, not wiring."""

from __future__ import annotations

import re
import textwrap
from typing import Any

from titan.core.models import Section, Snapshot


def clean(value: Any) -> str:
    text = re.sub(
        r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value if value is not None else "Unavailable")
    )
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text)[:256]


def hardware_map(snapshot: Snapshot, width: int = 76) -> str:
    width = max(24, min(width, 160))
    lines: list[str] = []

    def line(prefix: str, value: Any) -> None:
        chunks = textwrap.wrap(
            clean(value), max(8, width - len(prefix)), break_long_words=True, break_on_hyphens=False
        ) or [""]
        lines.extend(
            (prefix if i == 0 else "|" + " " * (len(prefix) - 1)) + part
            for i, part in enumerate(chunks)
        )

    def data(key: str) -> dict[str, Any]:
        return snapshot.sections.get(key, Section()).data

    line("", "+-- TITAN / HARDWARE INVENTORY --+")
    line("| ", f"{snapshot.platform} / {data('cpu').get('model') or 'CPU model unavailable'}")
    line(
        "+-- ",
        f"COMPUTE: {data('cpu').get('physical_cores', '?')} cores / {data('cpu').get('logical_cores', '?')} threads",
    )
    ram = data("memory").get("total")
    line(
        "+-- ",
        f"MEMORY: {ram / 1024**3:.1f} GiB"
        if isinstance(ram, (int, float))
        else "MEMORY: Unavailable",
    )
    line("+-- ", "STORAGE")
    drives = data("storage").get("drives", [])
    if not drives:
        line("|   +-- ", "Physical inventory unavailable or empty")
    for drive in drives[:256]:
        line(
            "|   +-- ",
            f"{drive.get('device')} / {drive.get('model')} [{drive.get('interface', '?')}]",
        )
        for partition in drive.get("partitions", [])[:256]:
            line(
                "|   |   +-- ",
                f"{partition.get('device')} / {partition.get('filesystem') or 'Unknown filesystem'} / {partition.get('mountpoint') or 'No mountpoint reported'}",
            )
    line("+-- ", "GRAPHICS")
    for gpu in data("gpu").get("items", [])[:64]:
        line("|   +-- ", gpu.get("name") or "Unknown adapter")
    line("+-- ", "NETWORK INTERFACES")
    for interface in data("network").get("interfaces", [])[:256]:
        state = interface.get("is_up")
        line(
            "|   +-- ",
            f"{interface.get('name')} / {'Up' if state is True else 'Down' if state is False else 'State unavailable'} / {interface.get('speed_mbps') or '?'} Mb/s link",
        )
    line("+-- ", "Inventory membership; no physical bus wiring inferred.")
    return "\n".join(lines)
