from __future__ import annotations

import sys
from pathlib import Path

from codex_claude.config import RunConfig
from codex_claude.controller import Controller
from codex_claude.git_inspector import GitInspector
from codex_claude.process_store import ProcessStore
from codex_claude.state import RunStatus


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def test_two_independent_workers_complete(git_repo: Path) -> None:
    controller = Controller.start(
        repository=git_repo,
        prompt="Create two independent files",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex-parallel"),
        claude_prefix=fake_prefix("claude-parallel"),
    )
    assert controller.run() == 0
    state = ProcessStore(GitInspector(git_repo)).load()
    assert state.status == RunStatus.COMPLETED
    assert {task.worker_id for task in state.tasks} == {"W01", "W02"}
    assert (git_repo / "parallel-a.txt").is_file()
    assert (git_repo / "parallel-b.txt").is_file()


def test_context_rotation_opens_new_session_without_new_attempt(git_repo: Path) -> None:
    controller = Controller.start(
        repository=git_repo,
        prompt="Create feature with a context rotation",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude-context"),
    )
    assert controller.run() == 0
    state = ProcessStore(GitInspector(git_repo)).load()
    task = state.tasks[0]
    assert task.attempt == 1
    assert len(task.sessions) == 2
    assert task.sessions[0].rotated
    assert task.sessions[0].session_id != task.sessions[1].session_id
