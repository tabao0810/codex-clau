from __future__ import annotations

from pathlib import Path

import pytest

from codex_claude.errors import GitError
from codex_claude.git_inspector import GitInspector


def test_dirty_repository_is_rejected(git_repo: Path) -> None:
    inspector = GitInspector(git_repo)
    inspector.require_clean()
    (git_repo / "changed.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(GitError):
        inspector.require_clean()


def test_worktree_digest_changes(git_repo: Path) -> None:
    inspector = GitInspector(git_repo)
    before = inspector.worktree_digest()
    (git_repo / "README.md").write_text("updated", encoding="utf-8")
    assert inspector.worktree_digest() != before
