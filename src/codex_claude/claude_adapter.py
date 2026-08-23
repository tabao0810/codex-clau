from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import ContractError, ProcessError
from .security import validate_command
from .state import SessionRecord, TaskSpec
from .subprocess_runner import SubprocessRunner


@dataclass(frozen=True, slots=True)
class ClaudeResult:
    session: SessionRecord
    summary: str
    completed: bool
    scope_change_required: bool
    remaining_work: tuple[str, ...]
    raw_result: str


def calculate_context_usage(result: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    model_usage = result.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None, None, None
    model, raw = next(iter(model_usage.items()))
    if not isinstance(model, str) or not isinstance(raw, dict):
        return None, None, None
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def integer(*names: str, source: dict[str, Any]) -> int:
        for name in names:
            value = source.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return 0

    used = (
        integer("input_tokens", "inputTokens", source=usage)
        + integer("cache_creation_input_tokens", "cacheCreationInputTokens", source=usage)
        + integer("cache_read_input_tokens", "cacheReadInputTokens", source=usage)
    )
    if used == 0:
        used = (
            integer("inputTokens", "input_tokens", source=raw)
            + integer("cacheCreationInputTokens", "cache_creation_input_tokens", source=raw)
            + integer("cacheReadInputTokens", "cache_read_input_tokens", source=raw)
        )
    window = integer("contextWindow", "context_window", source=raw)
    return model, used or None, window or None


def _allowed_bash_patterns(commands: tuple[tuple[str, ...], ...]) -> list[str]:
    patterns = [
        "Bash(git status *)",
        "Bash(git diff *)",
        "Bash(git log *)",
        "Bash(git show *)",
        "Bash(git rev-parse *)",
    ]
    for command in commands:
        validated = validate_command(command)
        patterns.append(f"Bash({shlex.join(validated)})")
    return patterns


class ClaudeAdapter:
    def __init__(
        self,
        *,
        runner: SubprocessRunner,
        command_prefix: tuple[str, ...] = ("claude",),
        timeout: int = 1800,
        context_limit: float = 55,
    ) -> None:
        self.runner = runner
        self.command_prefix = command_prefix
        self.timeout = timeout
        self.context_limit = context_limit

    @staticmethod
    def _schema_text() -> str:
        path = Path(str(files("codex_claude").joinpath("schemas", "claude-result.schema.json")))
        return path.read_text(encoding="utf-8")

    async def _execute_once(
        self,
        *,
        worktree: Path,
        prompt: str,
        task: TaskSpec,
        session_id: str | None = None,
    ) -> ClaudeResult:
        allowed = ["Read", "Edit", "Write", "Glob", "Grep"]
        allowed.extend(_allowed_bash_patterns(task.verification_commands))
        argv = [
            *self.command_prefix,
            "-p",
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Read,Edit,Write,Glob,Grep,Bash",
            "--allowedTools",
            ",".join(allowed),
            "--disallowedTools",
            "WebFetch,WebSearch",
            "--json-schema",
            self._schema_text(),
        ]
        if session_id is not None:
            argv.extend(["--resume", session_id])
        events: list[dict[str, Any]] = []

        async def capture(event: dict[str, Any]) -> None:
            events.append(event)

        result = await self.runner.run(
            argv,
            cwd=worktree,
            stdin=prompt,
            timeout=self.timeout,
            parse_jsonl=True,
            on_event=capture,
        )
        if result.returncode != 0:
            raise ProcessError(f"Claude failed with exit code {result.returncode}: {result.stderr}")
        result_events = [event for event in events if event.get("type") == "result"]
        if not result_events:
            raise ContractError("Claude did not emit a result event")
        final = result_events[-1]
        if final.get("is_error") is True:
            raise ProcessError(f"Claude reported an error: {final.get('result', '')}")
        resolved_session = final.get("session_id")
        if not isinstance(resolved_session, str):
            for event in events:
                candidate = event.get("session_id")
                if isinstance(candidate, str):
                    resolved_session = candidate
                    break
        if not isinstance(resolved_session, str):
            raise ContractError("Claude did not emit a session ID")
        structured = final.get("structured_output")
        if not isinstance(structured, dict):
            raw_result = final.get("result", "")
            if not isinstance(raw_result, str):
                raise ContractError("Claude result is not text")
            try:
                structured = json.loads(raw_result)
            except json.JSONDecodeError as exc:
                raise ContractError("Claude did not emit structured_output") from exc
        expected = {"summary", "completed", "scopeChangeRequired", "remainingWork"}
        if set(structured) != expected:
            raise ContractError("Claude structured result has unexpected fields")
        if (
            not isinstance(structured["summary"], str)
            or not isinstance(structured["completed"], bool)
            or not isinstance(structured["scopeChangeRequired"], bool)
            or not isinstance(structured["remainingWork"], list)
            or any(not isinstance(item, str) for item in structured["remainingWork"])
        ):
            raise ContractError("Claude structured result has invalid field types")
        model, used, window = calculate_context_usage(final)
        percent = used / window * 100 if used is not None and window else None
        session = SessionRecord(
            session_id=resolved_session,
            model=model,
            used_tokens=used,
            context_window=window,
            used_percent=percent,
            rotated=percent is not None and percent >= self.context_limit,
        )
        raw_result_value = final.get("result", "")
        return ClaudeResult(
            session=session,
            summary=structured["summary"],
            completed=structured["completed"],
            scope_change_required=structured["scopeChangeRequired"],
            remaining_work=tuple(structured["remainingWork"]),
            raw_result=raw_result_value if isinstance(raw_result_value, str) else "",
        )

    async def execute(
        self,
        *,
        worktree: Path,
        prompt: str,
        task: TaskSpec,
        session_id: str | None = None,
    ) -> ClaudeResult:
        last_error: ProcessError | None = None
        for attempt in range(2):
            try:
                return await self._execute_once(
                    worktree=worktree,
                    prompt=prompt,
                    task=task,
                    session_id=session_id,
                )
            except ProcessError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1)
        assert last_error is not None
        raise last_error


def build_task_prompt(
    *,
    goal: str,
    task: TaskSpec,
    requirements: str,
    repository_rules: str,
    dependency_evidence: str = "",
    retry_context: str = "",
) -> str:
    return f"""Implement exactly one task inside this isolated Git worktree.

Overall goal:
{goal}

Task {task.id}: {task.title}
Objective: {task.objective}
Acceptance criteria:
{chr(10).join(f"- {item}" for item in task.acceptance_criteria)}

Relevant requirement excerpts:
{requirements}

Dependency evidence:
{dependency_evidence or "(none)"}

Repository rules:
{repository_rules or "(none discovered)"}

Relevant paths: {", ".join(task.relevant_paths)}
Write scopes: {", ".join(task.write_scopes)}
Verification argv: {json.dumps([list(command) for command in task.verification_commands])}

Retry/session handoff:
{retry_context or "(first attempt)"}

Only edit paths owned by Write scopes. Do not edit process.md. Do not commit, push,
reset, clean, checkout, stash, create worktrees, publish, deploy, read credentials, or run
destructive filesystem/database commands. If another path is necessary, do not edit it;
return scopeChangeRequired=true and describe it in remainingWork. The controller runs and
judges verification. Return the structured result required by --json-schema.
"""
