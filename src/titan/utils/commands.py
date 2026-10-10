"""Bounded, shell-free execution for optional, read-only hardware tools."""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0


def run_command(args: Sequence[str], timeout: float = 5.0) -> CommandResult:
    """Run a fixed argument list; timeout cleanup is handled by subprocess.run.

    No collector interpolates user input into a shell. Do not include complete
    stderr in logs: vendor diagnostics may contain device serial numbers.
    """
    if not args or not all(isinstance(arg, str) and "\x00" not in arg for arg in args):
        return CommandResult(error="Invalid command arguments")
    if timeout <= 0:
        return CommandResult(error="Hardware query deadline exceeded")
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        return CommandResult(error=f"{os.path.basename(args[0])} is not installed")
    except PermissionError:
        return CommandResult(error="Permission denied while starting hardware query")
    except subprocess.TimeoutExpired:
        return CommandResult(error=f"Hardware query timed out after {timeout:g}s")
    except (OSError, ValueError):
        return CommandResult(error="Hardware query could not be started")
    if len(result.stdout) > 8_000_000:
        return CommandResult(returncode=result.returncode, error="Hardware response too large")
    return CommandResult(result.stdout, result.stderr, result.returncode)


def json_object(result: CommandResult) -> dict[str, Any] | None:
    """Decode structured data even when smartctl has set a health status bit."""
    if result.error:
        return None
    try:
        value = json.loads(result.stdout.lstrip("\ufeff").strip())
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def integer(value: Any) -> int | None:
    value = number(value)
    return int(value) if value is not None else None
