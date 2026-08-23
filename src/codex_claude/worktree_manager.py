from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError
from .git_inspector import GitInspector


@dataclass(frozen=True, slots=True)
class WorkerWorktree:
    task_id: str
    worker_id: str
    path: Path


class WorktreeManager:
    def __init__(self, inspector: GitInspector, run_id: str) -> None:
        self.inspector = inspector
        self.run_id = run_id
        self._base_dir: Path | None = None

    @property
    def base_dir(self) -> Path:
        if self._base_dir is None:
            self._base_dir = Path(tempfile.mkdtemp(prefix=f"codex-claude-{self.run_id}-"))
        return self._base_dir

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or self.inspector.repository,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed: "
                f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
            )
        return completed.stdout.decode("utf-8", errors="replace")

    def create(
        self,
        *,
        task_id: str,
        worker_id: str,
        initial_head: str,
        accepted_patches: tuple[Path, ...] = (),
    ) -> WorkerWorktree:
        path = self.base_dir / f"{task_id}-{worker_id}"
        if path.exists():
            raise GitError(f"worker worktree path already exists: {path}")
        self._git("worktree", "add", "--detach", str(path), initial_head)
        try:
            for patch in accepted_patches:
                self._git("apply", "--index", "--binary", str(patch), cwd=path)
        except BaseException:
            self.cleanup_path(path, force=True)
            raise
        return WorkerWorktree(task_id=task_id, worker_id=worker_id, path=path)

    def cleanup(self, worktree: WorkerWorktree, *, force: bool = False) -> None:
        self.cleanup_path(worktree.path, force=force)

    def cleanup_path(self, path: Path, *, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self._git(*args)

    def cleanup_base(self) -> None:
        if (
            self._base_dir is not None
            and self._base_dir.exists()
            and not any(self._base_dir.iterdir())
        ):
            self._base_dir.rmdir()
            self._base_dir = None
