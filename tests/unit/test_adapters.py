from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from codex_claude.claude_adapter import ClaudeAdapter, calculate_context_usage
from codex_claude.codex_adapter import CodexAdapter
from codex_claude.state import Plan, TaskSpec
from codex_claude.subprocess_runner import SubprocessRunner


def fake_prefix() -> tuple[str, ...]:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    return (sys.executable, str(script))


def task() -> TaskSpec:
    return TaskSpec(
        id="T01",
        title="Feature",
        objective="Create feature",
        acceptance_criteria=("file exists",),
        requirement_refs=("R01",),
        depends_on=(),
        write_scopes=("feature.txt",),
        resource_locks=(),
        parallel_safe=True,
        verification_commands=((sys.executable, "-c", "pass"),),
        relevant_paths=("README.md",),
    )


def test_codex_adapter_parses_plan(git_repo: Path, tmp_path: Path) -> None:
    adapter = CodexAdapter(
        runner=SubprocessRunner(),
        command_prefix=(*fake_prefix(), "codex"),
        timeout=5,
    )
    response = asyncio.run(
        adapter.plan(repository=git_repo, artifact_dir=tmp_path / "artifacts", prompt="plan")
    )
    assert response.thread_id == "thread-1"
    assert isinstance(response.value, Plan)
    assert response.value.tasks[0].id == "T01"


def test_claude_adapter_parses_usage(git_repo: Path) -> None:
    adapter = ClaudeAdapter(
        runner=SubprocessRunner(),
        command_prefix=(*fake_prefix(), "claude"),
        timeout=5,
    )
    result = asyncio.run(adapter.execute(worktree=git_repo, prompt="implement", task=task()))
    assert result.completed
    assert result.session.used_tokens == 80
    assert result.session.used_percent == 8


def test_context_usage_without_model_data_is_unknown() -> None:
    assert calculate_context_usage({"usage": {"input_tokens": 10}}) == (None, None, None)
