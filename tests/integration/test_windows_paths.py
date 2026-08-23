from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codex_claude.config import RunConfig
from codex_claude.controller import Controller
from codex_claude.errors import ValidationError
from codex_claude.git_inspector import GitInspector
from codex_claude.security import normalize_repo_path
from codex_claude.subprocess_runner import SubprocessRunner


def fake_prefix(mode: str) -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script), mode)


def initialize_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# Unicode fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path


def test_unicode_filename_round_trips_in_repository_path_with_spaces(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path / "repository with spaces - thử nghiệm")
    controller = Controller.start(
        repository=repository,
        prompt="Create a Unicode path",
        spec=None,
        config=RunConfig(codex_timeout=10, claude_timeout=10, verification_timeout=10),
        codex_prefix=fake_prefix("codex-unicode"),
        claude_prefix=fake_prefix("claude-unicode"),
    )
    assert controller.run() == 0
    target = repository / "tính năng" / "đã xong.txt"
    assert target.read_text(encoding="utf-8") == "implemented\n"
    assert GitInspector(repository).changed_paths() == ("tính năng/đã xong.txt",)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive semantics only")
def test_windows_drive_separators_and_executable_path_with_spaces(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path / "windows repository")
    inspector = GitInspector(repository)
    assert inspector.repository.drive
    assert normalize_repo_path(r"src\tính_năng\file.txt") == "src/tính_năng/file.txt"
    with pytest.raises(ValidationError, match="absolute paths are forbidden"):
        normalize_repo_path(r"C:\source\file.txt")

    git = shutil.which("git")
    assert git is not None
    if " " not in git:
        pytest.skip("the installed Git executable path does not contain a space")
    result = asyncio.run(
        SubprocessRunner().run(
            (git, "--version"),
            cwd=repository,
            timeout=10,
        )
    )
    assert result.returncode == 0
    assert result.stdout.startswith("git version")
