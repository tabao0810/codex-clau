from __future__ import annotations

import sys
from pathlib import Path

from codex_claude.config import RunConfig
from codex_claude.controller import Controller
from codex_claude.git_inspector import GitInspector
from codex_claude.patch_integrator import PatchIntegrator
from codex_claude.process_store import ProcessStore
from codex_claude.state import (
    RunState,
    RunStatus,
    TaskRecord,
    TaskSpec,
    TaskStatus,
)
from codex_claude.worktree_manager import WorktreeManager


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def test_resume_recovers_patch_applied_before_state_write(git_repo: Path) -> None:
    inspector = GitInspector(git_repo)
    run_id = "crash-recovery"
    manager = WorktreeManager(inspector, run_id)
    worktree = manager.create(
        task_id="T01",
        worker_id="W01",
        initial_head=inspector.head,
    )
    (worktree.path / "feature.txt").write_text("implemented\n", encoding="utf-8")
    task_spec = TaskSpec(
        id="T01",
        title="Feature",
        objective="Create feature",
        acceptance_criteria=("feature exists",),
        requirement_refs=("R01",),
        depends_on=(),
        write_scopes=("feature.txt",),
        resource_locks=(),
        parallel_safe=True,
        verification_commands=(),
        relevant_paths=(),
    )
    integrator = PatchIntegrator(inspector, run_id)
    patch = integrator.capture(worktree=worktree.path, task=task_spec)
    baseline_digest = inspector.worktree_digest()
    state = RunState(
        run_id=run_id,
        repository=str(git_repo),
        initial_head=inspector.head,
        goal="Recover",
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        status=RunStatus.INTEGRATING,
        phase=RunStatus.INTEGRATING.value,
        codex_thread_id="thread-1",
        tasks=[
            TaskRecord(
                spec=task_spec,
                status=TaskStatus.READY_TO_INTEGRATE,
                attempt=1,
                worker_id="W01",
                worktree_path=str(worktree.path),
                patch=patch,
            )
        ],
        final_verification_commands=[
            [
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('feature.txt').is_file()",
            ]
        ],
        active_task_ids=["T01"],
        working_tree_digest=baseline_digest,
    )
    store = ProcessStore(inspector)
    store.create(state)
    integrator.apply(patch)

    resumed = Controller.resume(
        git_repo,
        codex_prefix=fake_prefix("codex"),
        claude_prefix=fake_prefix("claude"),
    )
    assert resumed.run() == 0
    recovered = store.load()
    assert recovered.status == RunStatus.COMPLETED
    assert recovered.tasks[0].status == TaskStatus.COMPLETED
    assert not worktree.path.exists()
