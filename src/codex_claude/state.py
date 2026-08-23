from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar, cast

from .config import RunConfig
from .errors import StateError, ValidationError
from .security import normalize_repo_path, validate_command

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RunStatus(StrEnum):
    PREPARING = "PREPARING"
    PLANNING = "PLANNING"
    SCHEDULING = "SCHEDULING"
    EXECUTING = "EXECUTING"
    INTEGRATING = "INTEGRATING"
    REPLAN_PENDING = "REPLAN_PENDING"
    REPLANNING = "REPLANNING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    READY_TO_INTEGRATE = "READY_TO_INTEGRATE"
    INTEGRATING = "INTEGRATING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    INTEGRATION_BLOCKED = "INTEGRATION_BLOCKED"
    SUPERSEDED = "SUPERSEDED"


class RevisionStatus(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    APPLIED = "APPLIED"
    BLOCKED = "BLOCKED"


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PREPARING: frozenset(
        {
            RunStatus.PLANNING,
            RunStatus.REPLAN_PENDING,
            RunStatus.PAUSED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.PLANNING: frozenset(
        {
            RunStatus.SCHEDULING,
            RunStatus.REPLAN_PENDING,
            RunStatus.PAUSED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.SCHEDULING: frozenset(
        {
            RunStatus.EXECUTING,
            RunStatus.INTEGRATING,
            RunStatus.REPLAN_PENDING,
            RunStatus.PAUSED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.EXECUTING: frozenset(
        {
            RunStatus.SCHEDULING,
            RunStatus.INTEGRATING,
            RunStatus.REPLAN_PENDING,
            RunStatus.PAUSED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.INTEGRATING: frozenset(
        {
            RunStatus.SCHEDULING,
            RunStatus.REPLAN_PENDING,
            RunStatus.COMPLETED,
            RunStatus.PAUSED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.REPLAN_PENDING: frozenset(
        {RunStatus.REPLANNING, RunStatus.PAUSED, RunStatus.BLOCKED}
    ),
    RunStatus.REPLANNING: frozenset({RunStatus.SCHEDULING, RunStatus.PAUSED, RunStatus.BLOCKED}),
    RunStatus.COMPLETED: frozenset({RunStatus.REPLANNING}),
    RunStatus.PAUSED: frozenset(
        {RunStatus.PLANNING, RunStatus.REPLANNING, RunStatus.SCHEDULING, RunStatus.BLOCKED}
    ),
    RunStatus.BLOCKED: frozenset({RunStatus.REPLANNING, RunStatus.SCHEDULING}),
}

_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.SUPERSEDED}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.SUPERSEDED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.VERIFYING, TaskStatus.RETRYING, TaskStatus.BLOCKED}),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.REVIEWING, TaskStatus.RETRYING, TaskStatus.BLOCKED}
    ),
    TaskStatus.REVIEWING: frozenset(
        {
            TaskStatus.READY_TO_INTEGRATE,
            TaskStatus.RETRYING,
            TaskStatus.BLOCKED,
            TaskStatus.INTEGRATION_BLOCKED,
        }
    ),
    TaskStatus.READY_TO_INTEGRATE: frozenset(
        {TaskStatus.INTEGRATING, TaskStatus.INTEGRATION_BLOCKED}
    ),
    TaskStatus.INTEGRATING: frozenset({TaskStatus.COMPLETED, TaskStatus.INTEGRATION_BLOCKED}),
    TaskStatus.RETRYING: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.BLOCKED: frozenset({TaskStatus.RETRYING}),
    TaskStatus.INTEGRATION_BLOCKED: frozenset(),
    TaskStatus.SUPERSEDED: frozenset(),
}


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target == current:
        return
    if target not in _RUN_TRANSITIONS[current]:
        raise StateError(f"invalid run transition: {current} -> {target}")


def validate_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target == current:
        return
    if target not in _TASK_TRANSITIONS[current]:
        raise StateError(f"invalid task transition: {current} -> {target}")


def _strings(value: object, name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{name} must be an array of strings")
    result = tuple(cast(list[str], value))
    if nonempty and not result:
        raise ValidationError(f"{name} must not be empty")
    return result


def _commands(value: object, name: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be an array")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if not isinstance(command, list) or not command:
            raise ValidationError(f"{name} commands must be non-empty argv arrays")
        if any(not isinstance(arg, str) or not arg for arg in command):
            raise ValidationError(f"{name} argv values must be non-empty strings")
        commands.append(tuple(cast(list[str], command)))
    return tuple(commands)


def _require_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing:
        raise ValidationError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValidationError(f"{name} has unknown fields: {', '.join(sorted(extra))}")


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    requirement_refs: tuple[str, ...]
    depends_on: tuple[str, ...]
    write_scopes: tuple[str, ...]
    resource_locks: tuple[str, ...]
    parallel_safe: bool
    verification_commands: tuple[tuple[str, ...], ...]
    relevant_paths: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TaskSpec:
        fields = {
            "id",
            "title",
            "objective",
            "acceptanceCriteria",
            "requirementRefs",
            "dependsOn",
            "writeScopes",
            "resourceLocks",
            "parallelSafe",
            "verificationCommands",
            "relevantPaths",
        }
        _require_keys(value, fields, "task")
        for name in ("id", "title", "objective"):
            if not isinstance(value[name], str) or not cast(str, value[name]).strip():
                raise ValidationError(f"task.{name} must be a non-empty string")
        if not isinstance(value["parallelSafe"], bool):
            raise ValidationError("task.parallelSafe must be boolean")
        scopes = tuple(
            normalize_repo_path(scope)
            for scope in _strings(value["writeScopes"], "task.writeScopes", nonempty=True)
        )
        relevant = tuple(
            normalize_repo_path(path)
            for path in _strings(value["relevantPaths"], "task.relevantPaths")
        )
        verification_commands = _commands(
            value["verificationCommands"], "task.verificationCommands"
        )
        for command in verification_commands:
            validate_command(command)
        return cls(
            id=cast(str, value["id"]),
            title=cast(str, value["title"]),
            objective=cast(str, value["objective"]),
            acceptance_criteria=_strings(
                value["acceptanceCriteria"], "task.acceptanceCriteria", nonempty=True
            ),
            requirement_refs=_strings(value["requirementRefs"], "task.requirementRefs"),
            depends_on=_strings(value["dependsOn"], "task.dependsOn"),
            write_scopes=scopes,
            resource_locks=_strings(value["resourceLocks"], "task.resourceLocks"),
            parallel_safe=value["parallelSafe"],
            verification_commands=verification_commands,
            relevant_paths=relevant,
        )

    def to_contract(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "acceptanceCriteria": list(self.acceptance_criteria),
            "requirementRefs": list(self.requirement_refs),
            "dependsOn": list(self.depends_on),
            "writeScopes": list(self.write_scopes),
            "resourceLocks": list(self.resource_locks),
            "parallelSafe": self.parallel_safe,
            "verificationCommands": [list(command) for command in self.verification_commands],
            "relevantPaths": list(self.relevant_paths),
        }


@dataclass(frozen=True, slots=True)
class Plan:
    summary: str
    tasks: tuple[TaskSpec, ...]
    final_verification_commands: tuple[tuple[str, ...], ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Plan:
        _require_keys(value, {"summary", "tasks", "finalVerificationCommands"}, "plan")
        if not isinstance(value["summary"], str) or not value["summary"].strip():
            raise ValidationError("plan.summary must be a non-empty string")
        if not isinstance(value["tasks"], list) or not value["tasks"]:
            raise ValidationError("plan.tasks must be a non-empty array")
        tasks: list[TaskSpec] = []
        for item in cast(list[object], value["tasks"]):
            if not isinstance(item, dict):
                raise ValidationError("plan task must be an object")
            tasks.append(TaskSpec.from_dict(cast(dict[str, object], item)))
        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise ValidationError("plan task IDs must be unique")
        known = set(ids)
        for task in tasks:
            unknown = set(task.depends_on) - known
            if unknown:
                raise ValidationError(f"{task.id} has unknown dependencies: {sorted(unknown)}")
            if task.id in task.depends_on:
                raise ValidationError(f"{task.id} cannot depend on itself")
        final_commands = _commands(
            value["finalVerificationCommands"], "plan.finalVerificationCommands"
        )
        for command in final_commands:
            validate_command(command)
        return cls(
            summary=value["summary"],
            tasks=tuple(tasks),
            final_verification_commands=final_commands,
        )

    def to_contract(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "tasks": [task.to_contract() for task in self.tasks],
            "finalVerificationCommands": [
                list(command) for command in self.final_verification_commands
            ],
        }


@dataclass(frozen=True, slots=True)
class Review:
    verdict: str
    findings: tuple[str, ...]
    retry_instruction: str | None
    scope_change: tuple[str, ...] | None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Review:
        _require_keys(value, {"verdict", "findings", "retryInstruction", "scopeChange"}, "review")
        verdict = value["verdict"]
        if verdict not in {"PASS", "RETRY", "BLOCKED"}:
            raise ValidationError("review.verdict must be PASS, RETRY or BLOCKED")
        findings = _strings(value["findings"], "review.findings")
        retry = value["retryInstruction"]
        if retry is not None and not isinstance(retry, str):
            raise ValidationError("review.retryInstruction must be string or null")
        raw_scope = value["scopeChange"]
        scope = None
        if raw_scope is not None:
            scope = tuple(
                normalize_repo_path(item)
                for item in _strings(raw_scope, "review.scopeChange", nonempty=True)
            )
        if verdict == "RETRY" and (not isinstance(retry, str) or not retry.strip()):
            raise ValidationError("RETRY review requires retryInstruction")
        if verdict == "BLOCKED" and not findings:
            raise ValidationError("BLOCKED review requires at least one finding")
        return cls(verdict, findings, retry, scope)


@dataclass(slots=True)
class Revision:
    id: str
    type: str
    created_at: str
    sha256: str
    artifact_path: str
    summary: str
    status: RevisionStatus = RevisionStatus.QUEUED
    source_path: str | None = None
    size_bytes: int | None = None


@dataclass(slots=True)
class VerificationResult:
    argv: tuple[str, ...]
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_sha256: str = ""


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    model: str | None = None
    used_tokens: int | None = None
    context_window: int | None = None
    used_percent: float | None = None
    rotated: bool = False


@dataclass(slots=True)
class PatchRecord:
    artifact_path: str
    sha256: str
    changed_paths: list[str]
    integrated: bool = False


@dataclass(slots=True)
class TaskRecord:
    spec: TaskSpec
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    worker_id: str | None = None
    worktree_path: str | None = None
    sessions: list[SessionRecord] = field(default_factory=list)
    verification: list[VerificationResult] = field(default_factory=list)
    patch: PatchRecord | None = None
    blocker: str | None = None


@dataclass(slots=True)
class RunState:
    run_id: str
    repository: str
    initial_head: str
    goal: str
    config: RunConfig = field(default_factory=RunConfig)
    schema_version: int = SCHEMA_VERSION
    generation: int = 0
    status: RunStatus = RunStatus.PREPARING
    phase: str = "PREPARING"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    codex_thread_id: str | None = None
    revisions: list[Revision] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    final_verification_commands: list[list[str]] = field(default_factory=list)
    active_task_ids: list[str] = field(default_factory=list)
    accepted_patch_ids: list[str] = field(default_factory=list)
    working_tree_digest: str | None = None
    current_activity: str = ""
    last_integration: str = ""
    blocker: str | None = None

    def transition(self, target: RunStatus, *, activity: str | None = None) -> None:
        validate_run_transition(self.status, target)
        self.status = target
        self.phase = target.value
        self.updated_at = utc_now()
        if activity is not None:
            self.current_activity = activity

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["schema_version"] = self.schema_version
        value["status"] = self.status.value
        for revision in cast(list[dict[str, object]], value["revisions"]):
            revision["status"] = cast(RevisionStatus, revision["status"]).value
        for task in cast(list[dict[str, object]], value["tasks"]):
            task["status"] = cast(TaskStatus, task["status"]).value
            spec = cast(dict[str, object], task["spec"])
            task["spec"] = TaskSpec(
                id=cast(str, spec["id"]),
                title=cast(str, spec["title"]),
                objective=cast(str, spec["objective"]),
                acceptance_criteria=tuple(cast(list[str], spec["acceptance_criteria"])),
                requirement_refs=tuple(cast(list[str], spec["requirement_refs"])),
                depends_on=tuple(cast(list[str], spec["depends_on"])),
                write_scopes=tuple(cast(list[str], spec["write_scopes"])),
                resource_locks=tuple(cast(list[str], spec["resource_locks"])),
                parallel_safe=cast(bool, spec["parallel_safe"]),
                verification_commands=tuple(
                    tuple(command)
                    for command in cast(list[list[str]], spec["verification_commands"])
                ),
                relevant_paths=tuple(cast(list[str], spec["relevant_paths"])),
            ).to_contract()
        value["config"] = self.config.to_dict()
        return cast(dict[str, object], value)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RunState:
        version = value.get("schema_version", value.get("schemaVersion"))
        if version != SCHEMA_VERSION:
            raise StateError(
                f"unsupported process schema version {version!r}; expected {SCHEMA_VERSION}"
            )
        try:
            revisions = [
                Revision(
                    id=cast(str, item["id"]),
                    type=cast(str, item["type"]),
                    created_at=cast(str, item["created_at"]),
                    sha256=cast(str, item["sha256"]),
                    artifact_path=cast(str, item["artifact_path"]),
                    summary=cast(str, item["summary"]),
                    status=RevisionStatus(cast(str, item["status"])),
                    source_path=cast(str | None, item.get("source_path")),
                    size_bytes=cast(int | None, item.get("size_bytes")),
                )
                for item in cast(list[dict[str, object]], value.get("revisions", []))
            ]
            task_records: list[TaskRecord] = []
            for item in cast(list[dict[str, object]], value.get("tasks", [])):
                spec = TaskSpec.from_dict(cast(dict[str, object], item["spec"]))
                sessions = [
                    SessionRecord(**cast(dict[str, Any], session))
                    for session in cast(list[dict[str, object]], item.get("sessions", []))
                ]
                verification = [
                    VerificationResult(
                        argv=tuple(cast(list[str], result["argv"])),
                        exit_code=cast(int | None, result["exit_code"]),
                        duration_seconds=float(cast(int | float, result["duration_seconds"])),
                        timed_out=cast(bool, result["timed_out"]),
                        stdout_tail=cast(str, result.get("stdout_tail", "")),
                        stderr_tail=cast(str, result.get("stderr_tail", "")),
                        output_sha256=cast(str, result.get("output_sha256", "")),
                    )
                    for result in cast(list[dict[str, object]], item.get("verification", []))
                ]
                raw_patch = cast(dict[str, object] | None, item.get("patch"))
                patch = (
                    PatchRecord(
                        artifact_path=cast(str, raw_patch["artifact_path"]),
                        sha256=cast(str, raw_patch["sha256"]),
                        changed_paths=cast(list[str], raw_patch["changed_paths"]),
                        integrated=cast(bool, raw_patch.get("integrated", False)),
                    )
                    if raw_patch
                    else None
                )
                task_records.append(
                    TaskRecord(
                        spec=spec,
                        status=TaskStatus(cast(str, item["status"])),
                        attempt=cast(int, item.get("attempt", 0)),
                        worker_id=cast(str | None, item.get("worker_id")),
                        worktree_path=cast(str | None, item.get("worktree_path")),
                        sessions=sessions,
                        verification=verification,
                        patch=patch,
                        blocker=cast(str | None, item.get("blocker")),
                    )
                )
            raw_config = cast(dict[str, Any], value.get("config", {}))
            return cls(
                schema_version=SCHEMA_VERSION,
                generation=cast(int, value.get("generation", 0)),
                run_id=cast(str, value["run_id"]),
                repository=str(Path(cast(str, value["repository"])).resolve()),
                initial_head=cast(str, value["initial_head"]),
                goal=cast(str, value["goal"]),
                config=RunConfig.from_dict(raw_config),
                status=RunStatus(cast(str, value["status"])),
                phase=cast(str, value.get("phase", value["status"])),
                created_at=cast(str, value["created_at"]),
                updated_at=cast(str, value["updated_at"]),
                codex_thread_id=cast(str | None, value.get("codex_thread_id")),
                revisions=revisions,
                tasks=task_records,
                final_verification_commands=cast(
                    list[list[str]], value.get("final_verification_commands", [])
                ),
                active_task_ids=cast(list[str], value.get("active_task_ids", [])),
                accepted_patch_ids=cast(list[str], value.get("accepted_patch_ids", [])),
                working_tree_digest=cast(str | None, value.get("working_tree_digest")),
                current_activity=cast(str, value.get("current_activity", "")),
                last_integration=cast(str, value.get("last_integration", "")),
                blocker=cast(str | None, value.get("blocker")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError(f"invalid process state: {exc}") from exc


_EnumT = TypeVar("_EnumT", bound=StrEnum)


def enum_values(values: Iterable[_EnumT]) -> list[str]:
    return [value.value for value in values]
