from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_claude.config import RunConfig
from codex_claude.controller import Controller, add_requirement
from codex_claude.git_inspector import GitInspector
from codex_claude.process_store import ProcessStore
from codex_claude.state import RevisionStatus, RunState, RunStatus, TaskStatus


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def wait_for_state(
    store: ProcessStore,
    predicate: Callable[[RunState], bool],
    *,
    timeout: float = 20,
) -> RunState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.load()
        if predicate(state):
            return state
        time.sleep(0.02)
    raise AssertionError("controller did not reach the expected state before timeout")


def test_full_fake_system_queues_add_at_safe_boundary(git_repo: Path) -> None:
    controller = Controller.start(
        repository=git_repo,
        prompt="Exercise the complete fake orchestration system",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex-full"),
        claude_prefix=fake_prefix("claude-full"),
    )
    store = ProcessStore(GitInspector(git_repo))

    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(controller.run)
        wait_for_state(
            store,
            lambda state: any(
                task.spec.id == "T06" and task.status == TaskStatus.RUNNING for task in state.tasks
            ),
        )
        assert add_requirement(git_repo, "Add the queued feature") == "R02"
        assert run.result(timeout=30) == 0

    state = store.load()
    tasks = {task.spec.id: task for task in state.tasks}
    assert state.status == RunStatus.COMPLETED
    assert [revision.status for revision in state.revisions] == [
        RevisionStatus.APPLIED,
        RevisionStatus.APPLIED,
    ]
    assert set(state.accepted_patch_ids) == {f"T{index:02d}" for index in range(1, 8)}
    assert tasks["T01"].worker_id != tasks["T02"].worker_id
    assert tasks["T03"].worker_id == tasks["T04"].worker_id == "W01"
    assert tasks["T05"].attempt == 2
    assert tasks["T06"].attempt == 1
    assert len(tasks["T06"].sessions) == 2
    assert tasks["T06"].sessions[0].used_percent == 60
    assert tasks["T06"].sessions[0].rotated
    assert tasks["T07"].spec.requirement_refs == ("R02",)
    assert all(task.status == TaskStatus.COMPLETED for task in tasks.values())
    assert GitInspector(git_repo).head == state.initial_head
    assert "process.md" not in GitInspector(git_repo).changed_paths()


def test_srs_run_resumes_from_preparing_checkpoint(git_repo: Path, tmp_path: Path) -> None:
    spec = tmp_path / "requirements.txt"
    spec.write_text("R01: Create the fake feature.\n", encoding="utf-8")
    started = Controller.start(
        repository=git_repo,
        prompt=None,
        spec=spec,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    checkpoint = started.store.load()
    assert checkpoint.status == RunStatus.PREPARING
    assert checkpoint.revisions[0].type == "srs"
    assert checkpoint.revisions[0].source_path == str(spec.resolve())

    resumed = Controller.resume(
        git_repo,
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    assert resumed.run() == 0
    completed = resumed.store.load()
    assert completed.status == RunStatus.COMPLETED
    assert completed.revisions[0].status == RevisionStatus.APPLIED
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "implemented\n"
