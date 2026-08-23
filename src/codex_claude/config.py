from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class RunConfig:
    max_workers: int = 2
    claude_context_limit: float = 55.0
    codex_timeout: int = 600
    claude_timeout: int = 1800
    verification_timeout: int = 900
    max_task_attempts: int = 3
    max_context_rotations: int = 3
    max_output_bytes: int = 1_000_000
    terminal_mode: str = "auto"

    def __post_init__(self) -> None:
        positive_ints = {
            "max_workers": self.max_workers,
            "codex_timeout": self.codex_timeout,
            "claude_timeout": self.claude_timeout,
            "verification_timeout": self.verification_timeout,
            "max_task_attempts": self.max_task_attempts,
            "max_context_rotations": self.max_context_rotations,
            "max_output_bytes": self.max_output_bytes,
        }
        for name, value in positive_ints.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"{name} must be a positive integer")
        if not 1 <= self.claude_context_limit <= 100:
            raise ValidationError("claude_context_limit must be between 1 and 100")
        if self.terminal_mode not in {"auto", "visible", "hidden"}:
            raise ValidationError("terminal_mode must be auto, visible or hidden")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunConfig:
        return cls(**value)
