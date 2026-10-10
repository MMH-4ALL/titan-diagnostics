"""Validated, atomic TOML configuration with platform-native paths."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Configuration could not be read, validated, or persisted."""


@dataclass(frozen=True)
class Config:
    refresh_seconds: float = 1.0
    theme: str = "titan-dark"
    record_history: bool = False
    retention_days: int = 7
    history_interval_seconds: float = 10.0
    cpu_threshold: float = 90.0
    memory_threshold: float = 90.0
    sustained_seconds: float = 10.0


THEMES = ("titan-dark", "arctic", "matrix", "monochrome", "high-contrast")


def default_data_dir() -> Path:
    if os.environ.get("TITAN_DATA_DIR"):
        return Path(os.environ["TITAN_DATA_DIR"]).expanduser()
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Titan"
        )
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "titan"


def default_config_path() -> Path:
    if os.name == "nt":
        return default_data_dir() / "config.toml"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "titan"
        / "config.toml"
    )


def default_history_path() -> Path:
    return default_data_dir() / "history.sqlite3"


def validate_config(values: dict[str, Any]) -> Config:
    allowed = {field.name for field in fields(Config)}
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError(f"Unknown configuration setting(s): {', '.join(sorted(unknown))}.")
    merged = asdict(Config()) | values
    if type(merged["record_history"]) is not bool:
        raise ConfigError("record_history must be true or false.")
    if merged["theme"] not in THEMES:
        raise ConfigError(f"theme must be one of: {', '.join(THEMES)}.")
    ranges = {
        "refresh_seconds": (0.25, 60.0),
        "history_interval_seconds": (1.0, 3600.0),
        "cpu_threshold": (1.0, 100.0),
        "memory_threshold": (1.0, 100.0),
        "sustained_seconds": (1.0, 3600.0),
    }
    for key, (minimum, maximum) in ranges.items():
        value = merged[key]
        if type(value) not in (int, float) or not minimum <= value <= maximum:
            raise ConfigError(f"{key} must be a number between {minimum:g} and {maximum:g}.")
        merged[key] = float(value)
    days = merged["retention_days"]
    if type(days) is not int or not 1 <= days <= 365:
        raise ConfigError("retention_days must be an integer between 1 and 365.")
    return Config(**merged)


def load_config(path: str | Path | None = None) -> Config:
    location = Path(path) if path is not None else default_config_path()
    try:
        if not location.exists():
            return Config()
        with location.open("rb") as stream:
            values = tomllib.load(stream)
        return validate_config(values)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"Cannot read Titan configuration at {location}: {exc}. Correct the file or run titan config reset."
        ) from exc


def save_config(config: Config, path: str | Path | None = None) -> None:
    validated = validate_config(asdict(config))
    location = Path(path) if path is not None else default_config_path()
    lines = ["# Titan settings. History recording is opt-in."]
    for key, value in asdict(validated).items():
        if isinstance(value, str):
            encoded = json.dumps(value)
        elif isinstance(value, bool):
            encoded = "true" if value else "false"
        else:
            encoded = str(value)
        lines.append(f"{key} = {encoded}")
    temporary: Path | None = None
    try:
        location.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=location.parent, delete=False, newline="\n"
        ) as stream:
            temporary = Path(stream.name)
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, location)
    except OSError as exc:
        raise ConfigError(f"Cannot save Titan configuration at {location}: {exc}.") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
