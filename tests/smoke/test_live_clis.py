from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codex_claude.claude_adapter import ClaudeAdapter, build_task_prompt
from codex_claude.codex_adapter import CodexAdapter, build_planning_prompt
from codex_claude.controller import Controller
from codex_claude.git_inspector import GitInspector
from codex_claude.state import Plan, TaskSpec
from codex_claude.subprocess_runner import SubprocessRunner

LIVE_FLAG = "CODEX_CLAUDE_RUN_LIVE_TESTS"


def initialize_read_only_fixture(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Live Smoke"], cwd=path, check=True)
    (path / "README.md").write_text("# Read-only live smoke fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


@pytest.mark.live
def test_live_codex_and_claude_clis_read_without_editing(tmp_path: Path) -> None:
    if os.environ.get(LIVE_FLAG) != "1":
        pytest.skip(f"set {LIVE_FLAG}=1 to run authenticated live CLI smoke tests")
    if shutil.which("codex") is None or shutil.which("claude") is None:
        pytest.skip("Codex and Claude CLIs must both be installed")

    repository = initialize_read_only_fixture(tmp_path / "live-fixture")
    Controller._preflight_cli(("codex",), ("claude",))
    runner = SubprocessRunner(max_output_bytes=100_000)
    artifact_dir = repository / ".git" / "live-smoke"
    codex_result = asyncio.run(
        CodexAdapter(runner=runner, timeout=120).plan(
            repository=repository,
            artifact_dir=artifact_dir,
            prompt=build_planning_prompt(
                goal="Plan one read-only inspection of README.md; do not edit any files.",
                requirements="The smoke test must leave the Git working tree clean.",
                repository_rules="Do not edit files.",
            ),
        )
    )
    assert isinstance(codex_result.value, Plan)

    task = TaskSpec(
        id="SMOKE",
        title="Read the fixture",
        objective="Read README.md and finish without editing any file",
        acceptance_criteria=("The summary confirms README.md was inspected",),
        requirement_refs=("LIVE",),
        depends_on=(),
        write_scopes=("SMOKE_OUTPUT_DO_NOT_CREATE.txt",),
        resource_locks=(),
        parallel_safe=False,
        verification_commands=(),
        relevant_paths=("README.md",),
    )
    claude_result = asyncio.run(
        ClaudeAdapter(runner=runner, timeout=120).execute(
            worktree=repository,
            prompt=build_task_prompt(
                goal="Verify that the authenticated Claude CLI can inspect a fixture read-only.",
                task=task,
                requirements="Read README.md. Do not create, edit, or delete files.",
                repository_rules="This is a read-only smoke test.",
            ),
            task=task,
        )
    )
    assert claude_result.completed
    assert GitInspector(repository).changed_paths() == ()
