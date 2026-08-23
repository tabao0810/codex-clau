from __future__ import annotations

import sys
from pathlib import Path

from codex_claude.config import RunConfig
from codex_claude.controller import Controller
from codex_claude.git_inspector import GitInspector
from codex_claude.process_store import ProcessStore
from codex_claude.state import RunStatus, TaskStatus


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def test_controller_completes_fake_run(git_repo: Path) -> None:
    controller = Controller.start(
        repository=git_repo,
        prompt="Create the fake feature",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    assert controller.run() == 0
    state = ProcessStore(GitInspector(git_repo)).load()
    assert state.status == RunStatus.COMPLETED
    assert state.tasks[0].status == TaskStatus.COMPLETED
    assert state.tasks[0].attempt == 1
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "implemented\n"
    assert GitInspector(git_repo).head == state.initial_head
