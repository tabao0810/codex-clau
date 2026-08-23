from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codex_claude.config import RunConfig
from codex_claude.controller import Controller
from codex_claude.errors import StateError


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def test_resume_rejects_changed_srs(git_repo: Path, tmp_path: Path) -> None:
    spec = tmp_path / "requirements.md"
    spec.write_text("# Requirement\nOriginal\n", encoding="utf-8")
    Controller.start(
        repository=git_repo,
        prompt=None,
        spec=spec,
        config=RunConfig(),
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    spec.write_text("# Requirement\nChanged\n", encoding="utf-8")
    with pytest.raises(StateError, match="SRS digest changed"):
        Controller.resume(
            git_repo,
            codex_prefix=fake_prefix("codex"),
            claude_prefix=fake_prefix("claude"),
        )
