from __future__ import annotations

import sys
from pathlib import Path

from codex_claude.config import RunConfig
from codex_claude.controller import Controller, add_requirement
from codex_claude.git_inspector import GitInspector
from codex_claude.process_store import ProcessStore
from codex_claude.state import RunStatus, TaskStatus


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def test_add_reopens_completed_run_and_resume_replans(git_repo: Path) -> None:
    first = Controller.start(
        repository=git_repo,
        prompt="Create first feature",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    assert first.run() == 0
    add_requirement(git_repo, "Create a second feature")
    pending = ProcessStore(GitInspector(git_repo)).load()
    assert pending.status == RunStatus.REPLANNING
    assert [revision.id for revision in pending.revisions] == ["R01", "R02"]

    resumed = Controller.resume(
        git_repo,
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    assert resumed.run() == 0
    completed = ProcessStore(GitInspector(git_repo)).load()
    assert completed.status == RunStatus.COMPLETED
    assert [task.spec.id for task in completed.tasks if task.status == TaskStatus.COMPLETED] == [
        "T01",
        "T02",
    ]
    assert (git_repo / "feature2.txt").is_file()
