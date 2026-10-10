"""Cancellable, bounded TCP stability sampling of one explicitly chosen endpoint."""

from __future__ import annotations

import math
import socket
import statistics
import threading
import time
from collections import deque
from typing import Any

from titan.hardware.network import _resolve, _validate_target


def probe(target: str, port: int, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        addresses = _resolve(target, port, timeout)
    except (OSError, TimeoutError) as exc:
        return {"success": False, "stage": "dns", "error": type(exc).__name__, "latency_ms": None}
    if not addresses:
        return {"success": False, "stage": "dns", "error": "No addresses", "latency_ms": None}
    error = "TimeoutError"
    for family, kind, protocol, _, address in addresses[:8]:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        try:
            with socket.socket(family, kind, protocol) as connection:
                connection.settimeout(remaining)
                tcp_started = time.monotonic()
                connection.connect(address)
            return {
                "success": True,
                "stage": "tcp",
                "latency_ms": (time.monotonic() - tcp_started) * 1000,
            }
        except OSError as exc:
            error = type(exc).__name__
    return {"success": False, "stage": "tcp", "error": error, "latency_ms": None}


class StabilityTest:
    def __init__(
        self,
        target: str,
        port: int = 443,
        duration: float = 60,
        interval: float = 1,
        timeout: float = 2,
    ) -> None:
        self.target = _validate_target(target)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Port must be 1-65535.")
        for name, value, low, high in (
            ("duration", duration, 1, 3600),
            ("interval", interval, 1, 60),
            ("timeout", timeout, 0.2, 10),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError(f"{name} must be {low}-{high} seconds.")
        self.port, self.duration, self.interval, self.timeout = port, duration, interval, timeout
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.samples: deque[dict[str, Any]] = deque(maxlen=3600)
        self.state = "ready"
        self.started: float | None = None

    def run(self) -> dict[str, Any]:
        self.state = "running"
        self.started = time.time()
        start = time.monotonic()
        while not self.cancel.is_set() and time.monotonic() - start < self.duration:
            at = time.monotonic()
            remaining = self.duration - (at - start)
            sample = {
                "timestamp": time.time(),
                **probe(self.target, self.port, min(self.timeout, max(0.2, remaining))),
            }
            with self.lock:
                self.samples.append(sample)
            self.cancel.wait(
                min(
                    max(0, self.interval - (time.monotonic() - at)),
                    max(0, self.duration - (time.monotonic() - start)),
                )
            )
        self.state = "stopped" if self.cancel.is_set() else "complete"
        return self.summary()

    def stop(self) -> None:
        self.cancel.set()

    def summary(self) -> dict[str, Any]:
        with self.lock:
            samples = list(self.samples)
        latencies = [s["latency_ms"] for s in samples if s["success"]]
        ordered = sorted(latencies)
        failures = sum(not s["success"] for s in samples)
        return {
            "target": self.target,
            "port": self.port,
            "state": self.state,
            "started": self.started,
            "duration_seconds": self.duration,
            "attempts": len(samples),
            "failures": failures,
            "failure_percent": 100 * failures / len(samples) if samples else None,
            "dns_failures": sum(s.get("stage") == "dns" for s in samples),
            "median_ms": statistics.median(ordered) if ordered else None,
            "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else None,
            "jitter_ms": sum(abs(b - a) for a, b in zip(latencies, latencies[1:], strict=False))
            / (len(latencies) - 1)
            if len(latencies) > 1
            else None,
            "samples": samples,
            "note": "TCP connection success and handshake time, not packet loss or bandwidth. DNS is checked each probe. Refused connections can indicate a closed service. No application payload is sent; connections may appear in the endpoint's logs.",
        }
