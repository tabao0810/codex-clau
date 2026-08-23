from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .errors import GitError
from .security import normalize_repo_path


class GitInspector:
    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()
        root = self._run("rev-parse", "--show-toplevel").strip()
        self.root = Path(root).resolve()
        if self.root != self.repository:
            self.repository = self.root

    def _run(self, *args: str, input_bytes: bytes | None = None, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repository,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args)} failed: {detail}")
        return completed.stdout.decode("utf-8", errors="replace")

    @property
    def common_dir(self) -> Path:
        raw = self._run("rev-parse", "--git-common-dir").strip()
        path = Path(raw)
        return (self.repository / path).resolve() if not path.is_absolute() else path.resolve()

    @property
    def head(self) -> str:
        return self._run("rev-parse", "HEAD").strip()

    def is_tracked(self, path: str) -> bool:
        normalized = normalize_repo_path(path, directory_hint=False)
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", normalized],
            cwd=self.repository,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0

    def status_paths(self) -> tuple[str, ...]:
        output = self._run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries = output.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                break
            if len(entry) < 4:
                raise GitError("unexpected git status output")
            code = entry[:2]
            path = entry[3:]
            if "R" in code or "C" in code:
                index += 1
                if index >= len(entries) or not entries[index]:
                    raise GitError("unexpected rename/copy status output")
                path = entries[index]
            paths.append(path.replace("\\", "/"))
            index += 1
        return tuple(paths)

    def require_clean(self) -> None:
        paths = self.status_paths()
        if paths:
            raise GitError(f"working tree must be clean before a new run: {', '.join(paths)}")

    def untracked_paths(self) -> tuple[str, ...]:
        output = self._run("ls-files", "--others", "--exclude-standard", "-z")
        return tuple(entry.replace("\\", "/") for entry in output.split("\0") if entry)

    def changed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.status_paths())))

    def diff_binary(self, *, cached: bool = False) -> bytes:
        args = ["diff", "--binary", "--no-ext-diff"]
        if cached:
            args.append("--cached")
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repository,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(completed.stderr.decode("utf-8", errors="replace").strip())
        return completed.stdout

    def worktree_digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.head.encode())
        digest.update(b"\0tracked\0")
        digest.update(self.diff_binary())
        digest.update(b"\0cached\0")
        digest.update(self.diff_binary(cached=True))
        for relative in sorted(self.untracked_paths()):
            if relative == "process.md":
                continue
            digest.update(b"\0untracked\0")
            digest.update(relative.encode("utf-8"))
            path = self.repository / Path(relative)
            if path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()


def discover_repository(path: Path) -> GitInspector:
    if not path.exists() or not path.is_dir():
        raise GitError(f"repository directory does not exist: {path}")
    return GitInspector(path)
