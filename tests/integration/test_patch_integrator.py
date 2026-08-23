from __future__ import annotations

import sys
from pathlib import Path

from codex_claude.git_inspector import GitInspector
from codex_claude.patch_integrator import PatchIntegrator
from codex_claude.state import TaskSpec
from codex_claude.worktree_manager import WorktreeManager


def test_capture_and_apply_new_file(git_repo: Path) -> None:
    inspector = GitInspector(git_repo)
    manager = WorktreeManager(inspector, "run-1")
    worktree = manager.create(task_id="T01", worker_id="W01", initial_head=inspector.head)
    (worktree.path / "feature.txt").write_text("implemented\n", encoding="utf-8")
    task = TaskSpec(
        id="T01",
        title="Feature",
        objective="Feature",
        acceptance_criteria=("exists",),
        requirement_refs=("R01",),
        depends_on=(),
        write_scopes=("feature.txt",),
        resource_locks=(),
        parallel_safe=True,
        verification_commands=((sys.executable, "-c", "pass"),),
        relevant_paths=(),
    )
    integrator = PatchIntegrator(inspector, "run-1")
    patch = integrator.capture(worktree=worktree.path, task=task)
    integrator.apply(patch)
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "implemented\n"
    manager.cleanup(worktree, force=True)
    manager.cleanup_base()
