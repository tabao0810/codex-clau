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


def run_scenario(git_repo: Path, scenario: str) -> tuple[int, ProcessStore]:
    controller = Controller.start(
        repository=git_repo,
        prompt=f"Run {scenario}",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix(f"codex-{scenario}"),
        claude_prefix=fake_prefix(f"claude-{scenario}"),
    )
    return controller.run(), ProcessStore(GitInspector(git_repo))


def test_verification_failure_retries_same_task(git_repo: Path) -> None:
    exit_code, store = run_scenario(git_repo, "retry")
    state = store.load()
    assert exit_code == 0
    assert state.tasks[0].attempt == 2
    assert state.tasks[0].status == TaskStatus.COMPLETED


def test_task_blocks_after_three_attempts(git_repo: Path) -> None:
    exit_code, store = run_scenario(git_repo, "alwaysfail")
    state = store.load()
    assert exit_code == 2
    assert state.status == RunStatus.BLOCKED
    assert state.tasks[0].attempt == 3
