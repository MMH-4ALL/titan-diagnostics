"""Offline reports with sensitive identifiers redacted unless explicitly requested."""

from __future__ import annotations

import csv
import html
import io
import json
import platform
import re
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from titan.core.models import Finding, Snapshot

SENSITIVE_KEYS = frozenset(
    {
        "username",
        "user",
        "hostname",
        "host_name",
        "computer_name",
        "serial",
        "serial_number",
        "serialnumber",
        "wwn",
        "uuid",
        "guid",
        "mac",
        "mac_address",
        "address",
        "addresses",
        "ip",
        "ipv4",
        "ipv6",
        "ip_address",
        "local_address",
        "remote_address",
        "laddr",
        "raddr",
        "exe",
        "executable",
        "executable_path",
        "path",
        "mountpoint",
        "mount_point",
        "mountpoints",
        "device",
        "device_id",
        "device_name",
        "instance_name",
        "pnp_device_id",
        "name",
        "label",
        "volume_label",
        "target",
        "dns_servers",
        "dns",
        "gateway",
        "cmdline",
        "command_line",
        "command",
        "manufacturer_serial",
    }
)
REDACTED = "[redacted]"


def _is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or any(
        token in normalized for token in ("serial", "password", "token", "credential", "secret")
    )


def _identifiers(value: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if _is_sensitive(key):
        if isinstance(value, str) and value:
            found.add(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item:
                    found.add(item)
                elif isinstance(item, dict):
                    found |= _identifiers(item)
    if isinstance(value, dict):
        for name, item in value.items():
            found |= _identifiers(item, str(name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _identifiers(item)
    return found


def _scrub_text(value: str, identifiers: set[str]) -> str:
    result = value
    for identifier in sorted(identifiers, key=len, reverse=True):
        if len(identifier) >= 3:
            result = re.sub(re.escape(identifier), lambda _: REDACTED, result, flags=re.IGNORECASE)
    result = re.sub(r"\b[A-Za-z]:[\\/][^\s<>\"']*", REDACTED, result)
    result = re.sub(
        r"(?<!\w)/(?:home|Users|media|mnt|run/media|tmp|var)/[^\s<>\"']*", REDACTED, result
    )
    result = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", REDACTED, result)
    result = re.sub(r"(?<!\w)(?:[A-Fa-f0-9]{1,4}:){2,}[A-Fa-f0-9:]+(?:%[\w]+)?", REDACTED, result)
    result = re.sub(r"(?<!\w)(?:[A-Fa-f0-9]{2}[:-]){5}[A-Fa-f0-9]{2}(?!\w)", REDACTED, result)
    result = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", REDACTED, result)
    return result


def _redact(value: Any, identifiers: set[str], key: str = "") -> Any:
    if _is_sensitive(key) and value is not None:
        return REDACTED
    if isinstance(value, dict):
        # Interface names and drive keys may themselves carry identifiers.
        if key in {"interfaces", "per_disk", "devices", "adapters"}:
            return {
                f"item_{index + 1}": _redact(item, identifiers)
                for index, item in enumerate(value.values())
            }
        return {str(name): _redact(item, identifiers, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, identifiers) for item in value]
    if isinstance(value, str):
        return _scrub_text(value, identifiers)
    return value


def _tool_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        if name == "titan-diagnostics":
            from titan import __version__

            return __version__
        return "not installed"


def report_payload(
    snapshot: Snapshot, findings: list[Finding], include_sensitive: bool = False
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "collected_at": datetime.fromtimestamp(snapshot.timestamp, UTC).isoformat(),
        "privacy": "sensitive identifiers included"
        if include_sensitive
        else "sensitive identifiers redacted",
        "tools": {
            "titan": _tool_version("titan-diagnostics"),
            "python": platform.python_version(),
            "psutil": _tool_version("psutil"),
        },
        "snapshot": asdict(snapshot),
        "findings": [asdict(finding) for finding in findings],
        "interpretation": "Read-only observations and rule-based findings; unavailable metrics are not zero. SMART results cannot guarantee future drive reliability. No universal health score is used.",
    }
    if include_sensitive:
        return payload
    identifiers = _identifiers(payload)
    return _redact(payload, identifiers)


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [
            row
            for key, item in value.items()
            for row in _flatten(item, f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(value, list):
        return [
            row for index, item in enumerate(value) for row in _flatten(item, f"{prefix}[{index}]")
        ]
    return [(prefix, value)]


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "unavailable" if value is None else value


def export_processes(snapshot: Snapshot, include_sensitive: bool = False) -> str:
    """Export one process per CSV row; prevent spreadsheet formula injection."""
    section = snapshot.sections.get("processes")
    if section is None or section.error:
        raise ValueError(section.error if section else "Process information is unavailable.")
    payload = report_payload(snapshot, [], include_sensitive=include_sensitive)
    columns: tuple[str, ...] = (
        "pid",
        "name",
        "cpu_percent",
        "memory_bytes",
        "memory_percent",
        "exe",
        "create_time",
        "ppid",
        "username",
    )
    columns += ("io_read_bps", "io_write_bps", "download_bps", "upload_bps")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    for item in payload["snapshot"]["sections"]["processes"]["data"].get("items", []):
        writer.writerow([_csv_safe(item.get(column)) for column in columns])
    return stream.getvalue()


def export_report(
    snapshot: Snapshot,
    findings: list[Finding],
    format: str = "json",
    include_sensitive: bool = False,
) -> str:
    payload = report_payload(snapshot, findings, include_sensitive)
    output_format = format.lower()
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output_format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["metric", "value"])
        writer.writerows((_csv_safe(key), _csv_safe(value)) for key, value in _flatten(payload))
        return stream.getvalue()
    if output_format in {"text", "terminal"}:
        lines = [
            "TITAN · SYSTEM DIAGNOSTIC REPORT",
            f"Collected: {payload['collected_at']}",
            f"Privacy: {payload['privacy']}",
            f"Platform: {payload['snapshot']['platform']}",
            "",
        ]
        for name, section in payload["snapshot"]["sections"].items():
            lines.extend([name.upper(), json.dumps(section["data"], indent=2, ensure_ascii=False)])
            if section["unavailable"]:
                lines.append("Unavailable: " + "; ".join(section["unavailable"]))
            if section["error"]:
                lines.append("Collection error: " + section["error"])
            lines.append("")
        lines.append("DIAGNOSTIC FINDINGS")
        if not payload["findings"]:
            lines.append(
                "No configured rule triggered in the collected observations. This is not a guarantee of hardware health."
            )
        for finding in payload["findings"]:
            lines.extend(
                [
                    f"[{finding['severity'].upper()}] {finding['title']}",
                    f"Evidence: {finding['evidence']}",
                    f"Explanation: {finding['explanation']}",
                    "Possible causes: " + "; ".join(finding["possible_causes"]),
                    f"Next step: {finding['next_step']}",
                    f"Confidence: {finding['confidence']}; elevated access needed: {finding['requires_admin']}",
                    "",
                ]
            )
        lines.append(payload["interpretation"])
        return "\n".join(lines) + "\n"
    if output_format != "html":
        raise ValueError("Report format must be json, csv, html, or text.")
    panels = []
    for name, section in payload["snapshot"]["sections"].items():
        rows = "".join(
            f"<tr><th>{html.escape(key)}</th><td>{html.escape('unavailable' if value is None else str(value))}</td></tr>"
            for key, value in _flatten(section["data"])
        )
        notices = "".join(
            f"<p class='notice'>Unavailable: {html.escape(str(value))}</p>"
            for value in section["unavailable"]
        )
        if section["error"]:
            notices += f"<p class='notice'>Collection error: {html.escape(section['error'])}</p>"
        panels.append(
            f"<section><h2>{html.escape(name.title())}</h2>{notices}<table>{rows or '<tr><td>No accessible metrics in this section.</td></tr>'}</table></section>"
        )
    cards = []
    for finding in payload["findings"]:
        cards.append(
            f"<article><p class='badge'>{html.escape(finding['severity'].upper())} · {html.escape(finding['confidence'])} confidence</p><h3>{html.escape(finding['title'])}</h3><p><b>Evidence:</b> {html.escape(finding['evidence'])}</p><p>{html.escape(finding['explanation'])}</p><p><b>Possible causes:</b> {html.escape('; '.join(finding['possible_causes']))}</p><p><b>Next step:</b> {html.escape(finding['next_step'])}</p><p>Elevated access needed: {'yes' if finding['requires_admin'] else 'no'}</p></article>"
        )
    empty = "<p>No configured rule triggered in these observations. This is not a guarantee of hardware health.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>Titan system report</title>
<style>:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#090d14;color:#e8f0fa;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1200px;margin:auto;padding:40px 24px}}header{{border-bottom:1px solid #1b2636;padding-bottom:24px;margin-bottom:24px}}h1{{font-size:44px;letter-spacing:.14em;margin:0;color:#38bdf8}}h2{{font-size:22px}}h3{{margin:4px 0 16px}}.subtitle,.meta{{color:#94a3b8}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:20px}}section,article{{background:#121a26;border:1px solid #1b2636;border-radius:14px;padding:22px;margin-bottom:20px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;vertical-align:top;padding:9px 0;border-bottom:1px solid #1b2636;overflow-wrap:anywhere}}th{{width:50%;font-weight:500;color:#94a3b8;padding-right:14px}}.badge{{color:#fbbf24;font-size:12px;letter-spacing:.06em}}.notice{{color:#fbbf24}}footer{{color:#94a3b8;font-size:13px;border-top:1px solid #1b2636;padding-top:20px}}@media print{{:root{{color-scheme:light}}body{{background:white;color:#111}}section,article{{background:white;border-color:#ccc;break-inside:avoid}}th,.meta,.subtitle,footer{{color:#444}}}}</style></head>
<body><main><header><h1>TITAN</h1><p class="subtitle">System observations &amp; evidence-based diagnostics</p><p class="meta">{html.escape(payload["collected_at"])} · {html.escape(payload["snapshot"]["platform"])}<br>{html.escape(payload["privacy"])}</p></header><h2>Diagnostic findings</h2>{"".join(cards) or empty}<div class="grid">{"".join(panels)}</div><footer><p>{html.escape(payload["interpretation"])}</p><p>Tool versions: {html.escape(json.dumps(payload["tools"]))}. Offline report; no external resources or uploads.</p></footer></main></body></html>\n"""
