"""Transport models: a missing measurement is None, never an invented zero."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Section:
    data: dict[str, Any] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Snapshot:
    timestamp: float
    platform: str
    sections: dict[str, Section] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    id: str
    title: str
    severity: str
    evidence: str
    explanation: str
    possible_causes: list[str]
    next_step: str
    confidence: str = "medium"
    requires_admin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
