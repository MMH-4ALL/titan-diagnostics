"""Local network metadata and bounded, explicitly requested connectivity tests."""

from __future__ import annotations

import ipaddress
import json
import math
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from titan.core.models import Section

_RESOLVER_SLOT = threading.BoundedSemaphore(1)


def collect_network_metadata() -> dict[str, Any]:
    """No probes: inspect only the operating system's configured DNS and route."""
    if sys.platform == "win32":
        command = (
            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
            "$d=@(Get-DnsClientServerAddress | ForEach-Object {$_.ServerAddresses} | Select-Object -Unique); "
            "$g=Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | "
            "Select-Object -First 1 -ExpandProperty NextHop; "
            "@{dns=$d;gateway=$g}|ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                return {
                    "dns": [],
                    "gateway": None,
                    "unavailable": ["DNS and gateway configuration could not be read."],
                }
            data = json.loads(result.stdout)
            return {"dns": data.get("dns") or [], "gateway": data.get("gateway"), "unavailable": []}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {
                "dns": [],
                "gateway": None,
                "unavailable": ["DNS and gateway configuration could not be read."],
            }
    dns: list[str] = []
    gateway = None
    unavailable = []
    try:
        for line in Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                dns.append(fields[1])
    except OSError:
        unavailable.append("DNS configuration is inaccessible.")
    try:
        for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                gateway = str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1]))
                break
    except (OSError, ValueError):
        unavailable.append("IPv4 default gateway is inaccessible.")
    return {"dns": list(dict.fromkeys(dns)), "gateway": gateway, "unavailable": unavailable}


def _validate_target(target: str) -> str:
    if not isinstance(target, str) or target != target.strip() or len(target) > 253 or not target:
        raise ValueError("Enter a hostname or IP address, without spaces or a URL path.")
    try:
        return str(ipaddress.ip_address(target))
    except ValueError:
        try:
            hostname = target.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("The hostname is invalid.") from exc
        labels = hostname.rstrip(".").split(".")
        if not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise ValueError(
                "Enter only a hostname or IP address; URLs and command options are not accepted."
            ) from None
        return hostname


def _resolve(target: str, port: int, timeout: float) -> list[Any]:
    # getaddrinfo has no portable timeout. One bounded daemon worker avoids an
    # unbounded thread pool and cannot keep application shutdown waiting.
    if not _RESOLVER_SLOT.acquire(blocking=False):
        raise TimeoutError("A previous DNS lookup is still finishing; retry later.")
    result_queue: queue.Queue[Any] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            try:
                result_queue.put((True, socket.getaddrinfo(target, port, type=socket.SOCK_STREAM)))
            except Exception as exc:
                result_queue.put((False, exc))
        finally:
            _RESOLVER_SLOT.release()

    threading.Thread(target=worker, name="titan-dns", daemon=True).start()
    try:
        success, result = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("DNS resolution timed out.") from exc
    if not success:
        raise result
    return result


def network_test(target: str, port: int = 443, count: int = 4, timeout: float = 3) -> Section:
    """Resolve and probe only the selected target. No scanning or automatic probes."""
    target = _validate_target(target)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("The TCP port must be between 1 and 65535.")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 5:
        raise ValueError("Choose between 1 and 5 connectivity samples.")
    if not math.isfinite(timeout) or not 0.2 <= timeout <= 10:
        raise ValueError("The timeout must be between 0.2 and 10 seconds.")
    data: dict[str, Any] = {"target": target, "port": port, "timestamp": time.time()}
    unavailable = []
    started = time.monotonic()
    try:
        resolved = _resolve(target, port, timeout)
        addresses = list(dict.fromkeys(str(address[4][0]) for address in resolved))
        if not resolved:
            raise socket.gaierror("No address was returned.")
        data["dns"] = {
            "success": True,
            "addresses": addresses,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except (OSError, TimeoutError) as exc:
        data["dns"] = {"success": False, "addresses": [], "error": type(exc).__name__}
        data["tcp"] = {"success": None, "latency_ms": None}
        data["ping"] = {"available": False, "packet_loss_percent": None}
        return Section(
            data=data,
            unavailable=["TCP and ICMP were not attempted because DNS resolution failed."],
            error="DNS resolution failed or timed out.",
        )
    family, socket_type, protocol, _, address = resolved[0]
    started = time.monotonic()
    try:
        with socket.socket(family, socket_type, protocol) as connection:
            connection.settimeout(timeout)
            connection.connect(address)
        data["tcp"] = {"success": True, "latency_ms": round((time.monotonic() - started) * 1000, 2)}
    except OSError as exc:
        data["tcp"] = {"success": False, "latency_ms": None, "error": type(exc).__name__}
    ping = shutil.which("ping")
    if ping is None:
        data["ping"] = {"available": False, "packet_loss_percent": None}
        unavailable.append(
            "The ping utility is unavailable; ICMP latency and packet loss were not measured."
        )
    else:
        replies, completed, latencies = 0, 0, []
        ambiguous = 0
        for _ in range(count):
            if sys.platform == "win32":
                args = [ping, "-n", "1", "-w", str(max(1, int(timeout * 1000))), addresses[0]]
            else:
                args = [ping, "-n", "-c", "1", "-W", str(max(1, math.ceil(timeout))), addresses[0]]
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=timeout + 1,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                completed += 1
                continue
            except OSError:
                unavailable.append("ICMP probes could not run on this platform.")
                break
            completed += 1
            # Some Windows ping errors return zero. Require an echo response
            # marker; IPv4 has TTL, whereas Windows IPv6 replies use latency.
            match = re.search(
                r"(?:time|zeit|temps|время)\s*[=<]\s*([\d.,]+)\s*(?:ms|мс)",
                result.stdout,
                re.IGNORECASE,
            )
            reply = result.returncode == 0 and (
                "ttl=" in result.stdout.lower()
                or "bytes from" in result.stdout.lower()
                or (sys.platform == "win32" and ":" in addresses[0] and match is not None)
            )
            if reply:
                replies += 1
                if match:
                    latencies.append(float(match[1].replace(",", ".")))
            elif (
                sys.platform == "win32"
                and ":" in addresses[0]
                and result.returncode == 0
                and not re.search(
                    r"unreachable|недоступ|timed out|превышен", result.stdout, re.IGNORECASE
                )
            ):
                ambiguous += 1
                unavailable.append(
                    "Unrecognized IPv6 ping output; packet loss cannot be measured reliably."
                )
        data["ping"] = {
            "available": completed > 0 and ambiguous == 0,
            "attempts": completed,
            "replies": replies,
            "packet_loss_percent": None
            if completed == 0 or ambiguous
            else round(100 * (completed - replies) / completed, 2),
            "latencies_ms": latencies,
            "stability": "All probes replied"
            if completed and completed == replies
            else "One or more probes did not reply",
            "note": "A short ICMP sample; firewalls may block ping even when TCP connectivity works. Sub-millisecond Windows readouts are stored at the reported upper-bound resolution; unrecognized localized IPv6 output requires manual review.",
        }
    return Section(data=data, unavailable=unavailable)


run_network_test = network_test
