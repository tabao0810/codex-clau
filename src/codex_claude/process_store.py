from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from .errors import ConcurrentUpdateError, StateError
from .git_inspector import GitInspector
from .locking import FileLock
from .rendering import STATE_END, STATE_START, parse_process, render_process
from .state import RunState, utc_now


class ProcessStore:
    def __init__(self, inspector: GitInspector) -> None:
        self.inspector = inspector
        self.path = inspector.repository / "process.md"
        internal = inspector.common_dir / "codex-claude"
        self.state_lock_path = internal / "state.lock"
        self.ownership_lock_path = internal / "controller.lock"

    def ownership_lock(self) -> FileLock:
        return FileLock(self.ownership_lock_path)

    def state_lock(self, *, timeout: float = 5.0) -> FileLock:
        return FileLock(self.state_lock_path, timeout=timeout)

    def ensure_available(self) -> None:
        if self.inspector.is_tracked("process.md"):
            raise StateError("tracked process.md is owned by the repository")
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8")
            if STATE_START not in text or STATE_END not in text:
                raise StateError("existing process.md is not owned by codex-claude")

    def ensure_excluded(self) -> None:
        exclude = self.inspector.common_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        entries = {line.strip() for line in existing.splitlines()}
        if "/process.md" in entries:
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        self._atomic_write(exclude, f"{existing}{prefix}/process.md\n")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent, text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> RunState:
        if not self.path.is_file():
            raise StateError("process.md does not exist")
        try:
            state = parse_process(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(f"cannot read process.md: {exc}") from exc
        if Path(state.repository).resolve() != self.inspector.repository:
            raise StateError("process.md belongs to a different repository")
        return state

    def create(self, state: RunState) -> None:
        with self.state_lock():
            self.ensure_available()
            if self.path.exists():
                raise StateError("a process.md already exists; archive it before a new run")
            self.ensure_excluded()
            self._atomic_write(self.path, render_process(state))

    def save(self, state: RunState, *, expected_generation: int) -> RunState:
        with self.state_lock():
            current = self.load()
            if current.generation != expected_generation:
                raise ConcurrentUpdateError(
                    f"state generation changed: expected {expected_generation}, "
                    f"found {current.generation}"
                )
            state.generation = expected_generation + 1
            state.updated_at = utc_now()
            self._atomic_write(self.path, render_process(state))
            return state

    def update(self, mutator: Callable[[RunState], None]) -> RunState:
        with self.state_lock():
            state = self.load()
            generation = state.generation
            mutator(state)
            state.generation = generation + 1
            state.updated_at = utc_now()
            self._atomic_write(self.path, render_process(state))
            return state

    def archive(self, state: RunState) -> Path:
        archive = self.inspector.common_dir / "codex-claude" / state.run_id / "process.final.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            raise StateError(f"process archive already exists: {archive}")
        shutil.copyfile(self.path, archive)
        return archive
