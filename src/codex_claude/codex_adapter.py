from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import ContractError, ProcessError
from .state import Plan, Review
from .subprocess_runner import SubprocessRunner


@dataclass(frozen=True, slots=True)
class CodexResponse:
    thread_id: str
    value: Plan | Review


class CodexAdapter:
    def __init__(
        self,
        *,
        runner: SubprocessRunner,
        command_prefix: tuple[str, ...] = ("codex",),
        timeout: int = 600,
    ) -> None:
        self.runner = runner
        self.command_prefix = command_prefix
        self.timeout = timeout

    @staticmethod
    def _schema(name: str) -> Path:
        return Path(str(files("codex_claude").joinpath("schemas", name)))

    async def _invoke_once(
        self,
        *,
        repository: Path,
        artifact_dir: Path,
        prompt: str,
        schema_name: str,
        thread_id: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        output = artifact_dir / f"codex-{schema_name.removesuffix('.schema.json')}.json"
        output.unlink(missing_ok=True)
        if thread_id is None:
            argv = [
                *self.command_prefix,
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--output-schema",
                str(self._schema(schema_name)),
                "--output-last-message",
                str(output),
                "-",
            ]
        else:
            argv = [
                *self.command_prefix,
                "exec",
                "resume",
                "--json",
                "--ignore-user-config",
                "--output-schema",
                str(self._schema(schema_name)),
                "--output-last-message",
                str(output),
                thread_id,
                "-",
            ]
        events: list[dict[str, Any]] = []

        async def capture(event: dict[str, Any]) -> None:
            events.append(event)

        result = await self.runner.run(
            argv,
            cwd=repository,
            stdin=prompt,
            timeout=self.timeout,
            parse_jsonl=True,
            on_event=capture,
        )
        if result.returncode != 0:
            raise ProcessError(f"Codex failed with exit code {result.returncode}: {result.stderr}")
        started = [
            event.get("thread_id") for event in events if event.get("type") == "thread.started"
        ]
        resolved_thread = thread_id or next(
            (value for value in started if isinstance(value, str)), None
        )
        if resolved_thread is None:
            raise ContractError("Codex did not emit thread.started")
        if not output.is_file():
            raise ContractError("Codex did not write its structured final response")
        try:
            raw = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot parse Codex final response: {exc}") from exc
        if not isinstance(raw, dict):
            raise ContractError("Codex final response must be an object")
        return resolved_thread, raw

    async def _invoke(
        self,
        *,
        repository: Path,
        artifact_dir: Path,
        prompt: str,
        schema_name: str,
        thread_id: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        last_error: ProcessError | None = None
        for attempt in range(2):
            try:
                return await self._invoke_once(
                    repository=repository,
                    artifact_dir=artifact_dir,
                    prompt=prompt,
                    schema_name=schema_name,
                    thread_id=thread_id,
                )
            except ProcessError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1)
        assert last_error is not None
        raise last_error

    async def plan(self, *, repository: Path, artifact_dir: Path, prompt: str) -> CodexResponse:
        thread_id, raw = await self._invoke(
            repository=repository,
            artifact_dir=artifact_dir,
            prompt=prompt,
            schema_name="plan.schema.json",
        )
        return CodexResponse(thread_id, Plan.from_dict(raw))

    async def replan(
        self, *, repository: Path, artifact_dir: Path, thread_id: str, prompt: str
    ) -> CodexResponse:
        resolved_thread, raw = await self._invoke(
            repository=repository,
            artifact_dir=artifact_dir,
            prompt=prompt,
            schema_name="plan.schema.json",
            thread_id=thread_id,
        )
        return CodexResponse(resolved_thread, Plan.from_dict(raw))

    async def review(
        self, *, repository: Path, artifact_dir: Path, thread_id: str, prompt: str
    ) -> CodexResponse:
        resolved_thread, raw = await self._invoke(
            repository=repository,
            artifact_dir=artifact_dir,
            prompt=prompt,
            schema_name="review.schema.json",
            thread_id=thread_id,
        )
        return CodexResponse(resolved_thread, Review.from_dict(raw))


def build_planning_prompt(
    *,
    goal: str,
    requirements: str,
    repository_rules: str,
    replan_context: str = "",
) -> str:
    return f"""You are the read-only planning and review agent for codex-claude.

Security policy has highest priority. Do not edit files, mutate Git, publish, deploy,
read credentials, or propose commands that violate the controller policy.

Goal:
{goal}

Requirements:
{requirements}

Repository rules:
{repository_rules or "(none discovered)"}

Replan context:
{replan_context or "(initial plan)"}

Return only the structured plan required by the supplied JSON Schema. Make tasks small
enough for one Claude session, use repository-relative writeScopes, argv arrays for
verificationCommands, explicit dependencies and resource locks. Independent tasks may
set parallelSafe=true, but the controller makes the final scheduling decision.
"""


def build_review_prompt(*, task: str, evidence: str, final: bool = False) -> str:
    scope = "the complete integrated change" if final else f"task {task}"
    return f"""Review {scope} read-only. Do not edit files or mutate Git.
Use only the bounded evidence below and repository files. Return PASS, RETRY or BLOCKED
according to the supplied JSON Schema. RETRY needs a concrete instruction; BLOCKED needs
at least one finding.

Evidence:
{evidence}
"""
