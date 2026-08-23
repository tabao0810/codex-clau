from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .errors import IntegrationError
from .git_inspector import GitInspector
from .security import changed_paths_within_scopes
from .state import PatchRecord, TaskSpec


def _git_bytes(cwd: Path, *args: str, check: bool = True) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise IntegrationError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return completed.stdout


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PatchIntegrator:
    def __init__(self, inspector: GitInspector, run_id: str) -> None:
        self.inspector = inspector
        self.run_id = run_id

    def capture(self, *, worktree: Path, task: TaskSpec) -> PatchRecord:
        untracked_raw = _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
        untracked = [
            item.decode("utf-8", errors="strict") for item in untracked_raw.split(b"\0") if item
        ]
        if untracked:
            _git_bytes(worktree, "add", "--intent-to-add", "--", *untracked)
        changed_raw = _git_bytes(worktree, "diff", "--name-only", "-z")
        changed = sorted(
            {
                item.decode("utf-8", errors="strict").replace("\\", "/")
                for item in changed_raw.split(b"\0")
                if item
            }
        )
        if not changed:
            raise IntegrationError(f"{task.id} produced no source changes")
        if not changed_paths_within_scopes(changed, task.write_scopes):
            outside = [
                path
                for path in changed
                if not changed_paths_within_scopes((path,), task.write_scopes)
            ]
            raise IntegrationError(
                f"{task.id} changed paths outside writeScopes: {', '.join(outside)}"
            )
        patch = _git_bytes(worktree, "diff", "--binary", "--full-index")
        if not patch:
            raise IntegrationError(f"{task.id} produced an empty patch")
        relative = Path("codex-claude") / self.run_id / "patches" / f"{task.id}.patch"
        artifact = self.inspector.common_dir / relative
        if artifact.exists():
            existing = artifact.read_bytes()
            if existing != patch:
                raise IntegrationError(
                    f"patch artifact already exists with other content: {task.id}"
                )
        else:
            _atomic_bytes(artifact, patch)
        return PatchRecord(
            artifact_path=relative.as_posix(),
            sha256=hashlib.sha256(patch).hexdigest(),
            changed_paths=changed,
        )

    def artifact_path(self, record: PatchRecord) -> Path:
        return self.inspector.common_dir / Path(record.artifact_path)

    def verify_artifact(self, record: PatchRecord) -> Path:
        path = self.artifact_path(record)
        if not path.is_file():
            raise IntegrationError(f"patch artifact is missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record.sha256:
            raise IntegrationError(f"patch digest mismatch: {path}")
        return path

    def check(self, record: PatchRecord) -> None:
        path = self.verify_artifact(record)
        completed = subprocess.run(
            ["git", "apply", "--check", "--binary", str(path)],
            cwd=self.inspector.repository,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise IntegrationError(
                "patch cannot be applied cleanly: "
                + completed.stderr.decode("utf-8", errors="replace").strip()
            )

    def is_already_applied(self, record: PatchRecord) -> bool:
        path = self.verify_artifact(record)
        completed = subprocess.run(
            ["git", "apply", "--reverse", "--check", "--binary", str(path)],
            cwd=self.inspector.repository,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0

    def apply(self, record: PatchRecord) -> None:
        if self.is_already_applied(record):
            record.integrated = True
            return
        self.check(record)
        path = self.verify_artifact(record)
        completed = subprocess.run(
            ["git", "apply", "--binary", str(path)],
            cwd=self.inspector.repository,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise IntegrationError(
                "patch integration failed: "
                + completed.stderr.decode("utf-8", errors="replace").strip()
            )
        record.integrated = True
