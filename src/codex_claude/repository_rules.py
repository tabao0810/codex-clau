from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError
from .security import normalize_repo_path

_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md")
_MAX_INSTRUCTION_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class RepositoryRule:
    path: str
    sha256: str
    content: str


def _parents_for_path(root: Path, relative: str) -> list[Path]:
    normalized = normalize_repo_path(relative)
    target = root / Path(normalized.rstrip("/"))
    current = target if normalized.endswith("/") else target.parent
    parents: list[Path] = []
    while current == root or root in current.parents:
        parents.append(current)
        if current == root:
            break
        current = current.parent
    parents.reverse()
    return parents


def load_repository_rules(root: Path, relevant_paths: Iterable[str]) -> tuple[RepositoryRule, ...]:
    root = root.resolve()
    directories = {root}
    for relative in relevant_paths:
        directories.update(_parents_for_path(root, relative))
    found: list[RepositoryRule] = []
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        for name in _INSTRUCTION_NAMES:
            path = directory / name
            if not path.is_file():
                continue
            data = path.read_bytes()
            if len(data) > _MAX_INSTRUCTION_BYTES:
                raise ValidationError(f"repository instruction file is too large: {path}")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(f"repository instruction must be UTF-8: {path}") from exc
            found.append(
                RepositoryRule(
                    path=path.relative_to(root).as_posix(),
                    sha256=hashlib.sha256(data).hexdigest(),
                    content=content,
                )
            )
    return tuple(found)
