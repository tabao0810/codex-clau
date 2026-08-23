from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .claude_adapter import ClaudeAdapter, ClaudeResult, build_task_prompt
from .codex_adapter import CodexAdapter, build_planning_prompt, build_review_prompt
from .config import RunConfig
from .errors import (
    GitError,
    IntegrationError,
    OrchestratorError,
    ProcessError,
    StateError,
)
from .git_inspector import GitInspector, discover_repository
from .input_loader import LoadedInput, load_input, load_prompt, load_spec
from .patch_integrator import PatchIntegrator
from .process_store import ProcessStore
from .repository_rules import load_repository_rules
from .requirements import create_revision, read_revision_content
from .scheduler import Scheduler
from .security import redact, sanitize_environment
from .state import (
    PatchRecord,
    Plan,
    Review,
    RevisionStatus,
    RunState,
    RunStatus,
    TaskRecord,
    TaskStatus,
    VerificationResult,
    validate_task_transition,
)
from .subprocess_runner import SubprocessRunner
from .verifier import Verifier
from .worktree_manager import WorkerWorktree, WorktreeManager


@dataclass(slots=True)
class TaskOutcome:
    task_id: str
    worktree: WorkerWorktree
    patch: PatchRecord | None = None
    blocker: str | None = None

    @property
    def accepted(self) -> bool:
        return self.patch is not None and self.blocker is None


class Controller:
    def __init__(
        self,
        *,
        inspector: GitInspector,
        store: ProcessStore,
        state: RunState,
        codex: CodexAdapter,
        claude: ClaudeAdapter,
        verifier: Verifier,
    ) -> None:
        self.inspector = inspector
        self.repository = inspector.repository
        self.store = store
        self.state = state
        self.codex = codex
        self.claude = claude
        self.verifier = verifier
        self.worktrees = WorktreeManager(inspector, state.run_id)
        self.patches = PatchIntegrator(inspector, state.run_id)
        self._codex_lock = asyncio.Lock()

    @staticmethod
    def _run_id() -> str:
        return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")

    @classmethod
    def start(
        cls,
        *,
        repository: Path,
        prompt: str | None,
        spec: Path | None,
        config: RunConfig,
        codex_prefix: tuple[str, ...] = ("codex",),
        claude_prefix: tuple[str, ...] = ("claude",),
    ) -> Controller:
        loaded = load_input(prompt, spec)
        inspector = discover_repository(repository)
        inspector.require_clean()
        store = ProcessStore(inspector)
        store.ensure_available()
        if store.path.exists():
            previous = store.load()
            if previous.status != RunStatus.COMPLETED:
                raise StateError(f"run {previous.run_id} is {previous.status}; use add or resume")
            store.archive(previous)
            store.path.unlink()
        cls._require_executable(codex_prefix[0], "Codex")
        cls._require_executable(claude_prefix[0], "Claude")
        cls._preflight_cli(codex_prefix, claude_prefix)
        run_id = cls._run_id()
        revision = create_revision(
            loaded,
            git_common_dir=inspector.common_dir,
            run_id=run_id,
            revision_number=1,
        )
        state = RunState(
            run_id=run_id,
            repository=str(inspector.repository),
            initial_head=inspector.head,
            goal=loaded.summary if loaded.type == "srs" else loaded.text,
            config=config,
            revisions=[revision],
            working_tree_digest=inspector.worktree_digest(),
            current_activity="Preparing the initial Codex plan.",
        )
        store.create(state)
        runner = SubprocessRunner(max_output_bytes=config.max_output_bytes)
        return cls(
            inspector=inspector,
            store=store,
            state=state,
            codex=CodexAdapter(
                runner=runner,
                command_prefix=codex_prefix,
                timeout=config.codex_timeout,
            ),
            claude=ClaudeAdapter(
                runner=runner,
                command_prefix=claude_prefix,
                timeout=config.claude_timeout,
                context_limit=config.claude_context_limit,
            ),
            verifier=Verifier(runner, timeout=config.verification_timeout),
        )

    @classmethod
    def resume(
        cls,
        repository: Path,
        *,
        codex_prefix: tuple[str, ...] = ("codex",),
        claude_prefix: tuple[str, ...] = ("claude",),
    ) -> Controller:
        inspector = discover_repository(repository)
        store = ProcessStore(inspector)
        state = store.load()
        if state.status == RunStatus.COMPLETED:
            raise StateError("run is already COMPLETED; use add to append a requirement")
        if inspector.head != state.initial_head:
            raise GitError("repository HEAD changed since the run started")
        for revision in state.revisions:
            if revision.type == "srs" and revision.source_path:
                current = load_spec(Path(revision.source_path))
                if current.sha256 != revision.sha256:
                    raise StateError(f"SRS digest changed for {revision.id}")
        cls._require_executable(codex_prefix[0], "Codex")
        cls._require_executable(claude_prefix[0], "Claude")
        cls._preflight_cli(codex_prefix, claude_prefix)
        runner = SubprocessRunner(max_output_bytes=state.config.max_output_bytes)
        controller = cls(
            inspector=inspector,
            store=store,
            state=state,
            codex=CodexAdapter(
                runner=runner,
                command_prefix=codex_prefix,
                timeout=state.config.codex_timeout,
            ),
            claude=ClaudeAdapter(
                runner=runner,
                command_prefix=claude_prefix,
                timeout=state.config.claude_timeout,
                context_limit=state.config.claude_context_limit,
            ),
            verifier=Verifier(runner, timeout=state.config.verification_timeout),
        )
        controller._validate_resume_artifacts()
        return controller

    @staticmethod
    def _require_executable(executable: str, display_name: str) -> None:
        if Path(executable).is_absolute() and Path(executable).is_file():
            return
        if shutil.which(executable) is None:
            raise ProcessError(f"{display_name} CLI executable was not found: {executable}")

    @staticmethod
    def _preflight_command(argv: tuple[str, ...], required: tuple[str, ...] = ()) -> str:
        try:
            completed = subprocess.run(
                argv,
                env=sanitize_environment(),
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProcessError(f"preflight could not run {argv[0]}: {exc}") from exc
        output = completed.stdout.decode("utf-8", errors="replace")
        error = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise ProcessError(f"preflight failed for {argv[0]}: {redact(error or output).strip()}")
        if any(token not in output for token in required):
            raise ProcessError(
                f"{argv[0]} does not expose the CLI features required by codex-claude"
            )
        return output

    @classmethod
    def _preflight_cli(
        cls,
        codex_prefix: tuple[str, ...],
        claude_prefix: tuple[str, ...],
    ) -> None:
        codex_name = Path(codex_prefix[0]).name.lower().removesuffix(".exe")
        claude_name = Path(claude_prefix[0]).name.lower().removesuffix(".exe")
        if codex_name == "codex" and len(codex_prefix) == 1:
            cls._preflight_command(
                (*codex_prefix, "exec", "--help"),
                ("--json", "--output-schema", "--sandbox", "--ignore-user-config"),
            )
            cls._preflight_command((*codex_prefix, "login", "status"))
        if claude_name == "claude" and len(claude_prefix) == 1:
            cls._preflight_command(
                (*claude_prefix, "--help"),
                (
                    "--output-format",
                    "--permission-mode",
                    "--json-schema",
                    "--resume",
                    "--allowedTools",
                    "--safe-mode",
                ),
            )
            auth = cls._preflight_command((*claude_prefix, "auth", "status", "--json"))
            try:
                auth_state = json.loads(auth)
            except json.JSONDecodeError as exc:
                raise ProcessError("Claude auth status did not return JSON") from exc
            if not isinstance(auth_state, dict) or auth_state.get("loggedIn") is not True:
                raise ProcessError("Claude CLI is not logged in")

    @property
    def artifact_dir(self) -> Path:
        return self.inspector.common_dir / "codex-claude" / self.state.run_id / "agent"

    def _update(self, mutator: Callable[[RunState], None]) -> RunState:
        self.state = self.store.update(mutator)
        return self.state

    @staticmethod
    def _task(state: RunState, task_id: str) -> TaskRecord:
        for task in state.tasks:
            if task.spec.id == task_id:
                return task
        raise StateError(f"task is missing from process state: {task_id}")

    def _requirements_text(self, state: RunState | None = None) -> str:
        current = state or self.state
        sections = []
        for revision in current.revisions:
            content = read_revision_content(revision, self.inspector.common_dir)
            sections.append(f"## {revision.id} ({revision.type})\n{content}")
        return "\n\n".join(sections)

    def _rules_text(self, relevant_paths: tuple[str, ...] = ("README.md",)) -> str:
        rules = load_repository_rules(self.repository, relevant_paths)
        return "\n\n".join(f"## {rule.path}\n{rule.content}" for rule in rules)

    def _validate_resume_artifacts(self) -> None:
        for task in self.state.tasks:
            if task.patch is not None:
                self.patches.verify_artifact(task.patch)
        active_integration = any(
            task.status in {TaskStatus.READY_TO_INTEGRATE, TaskStatus.INTEGRATING}
            for task in self.state.tasks
        )
        if (
            self.state.working_tree_digest
            and self.inspector.worktree_digest() != self.state.working_tree_digest
            and not active_integration
        ):
            raise StateError("working tree differs from the last controller checkpoint")

    def run(self) -> int:
        try:
            with self.store.ownership_lock():
                return asyncio.run(self._run())
        except KeyboardInterrupt:
            self._pause("Interrupted by user")
            return 130
        except OrchestratorError as exc:
            self._block(str(exc))
            raise

    def _pause(self, reason: str) -> None:
        try:
            self._update(lambda state: self._set_run_terminal(state, RunStatus.PAUSED, reason))
        except OrchestratorError:
            return

    def _block(self, reason: str) -> None:
        try:
            self._update(lambda state: self._set_run_terminal(state, RunStatus.BLOCKED, reason))
        except OrchestratorError:
            return

    @staticmethod
    def _set_run_terminal(state: RunState, status: RunStatus, reason: str) -> None:
        if state.status != status:
            state.transition(status, activity=reason)
        state.blocker = reason

    async def _run(self) -> int:
        await self._recover_if_needed()
        while True:
            self.state = self.store.load()
            if self.state.status in {RunStatus.PREPARING, RunStatus.PLANNING}:
                await self._initial_plan()
                continue
            if self.state.status in {RunStatus.REPLAN_PENDING, RunStatus.REPLANNING}:
                await self._replan()
                continue
            if self.state.status in {RunStatus.PAUSED, RunStatus.BLOCKED}:
                queued = any(
                    revision.status == RevisionStatus.QUEUED for revision in self.state.revisions
                )
                if queued:
                    self._update(
                        lambda state: state.transition(
                            RunStatus.REPLANNING,
                            activity="Applying queued requirement revisions.",
                        )
                    )
                    continue
                self._prepare_resumed_tasks()
                continue
            active_tasks = [
                task for task in self.state.tasks if task.status != TaskStatus.SUPERSEDED
            ]
            if active_tasks and all(task.status == TaskStatus.COMPLETED for task in active_tasks):
                return await self._finalize()
            scheduler = Scheduler(active_tasks, max_workers=self.state.config.max_workers)
            wave = scheduler.next_wave()
            if not wave:
                blockers = [
                    task.blocker
                    for task in active_tasks
                    if task.status in {TaskStatus.BLOCKED, TaskStatus.INTEGRATION_BLOCKED}
                    and task.blocker
                ]
                reason = "; ".join(blockers) or "No runnable task remains"
                self._block(reason)
                return 2
            await self._execute_wave(wave)

    async def _initial_plan(self) -> None:
        if self.state.status == RunStatus.PREPARING:
            self._update(
                lambda state: state.transition(
                    RunStatus.PLANNING, activity="Codex is creating the task DAG."
                )
            )
        response = await self.codex.plan(
            repository=self.repository,
            artifact_dir=self.artifact_dir,
            prompt=build_planning_prompt(
                goal=self.state.goal,
                requirements=self._requirements_text(),
                repository_rules=self._rules_text(),
            ),
        )
        if not isinstance(response.value, Plan):
            raise StateError("Codex returned a review where a plan was required")
        plan = response.value
        Scheduler(
            [TaskRecord(spec=task) for task in plan.tasks],
            max_workers=self.state.config.max_workers,
        )

        def apply_plan(state: RunState) -> None:
            state.codex_thread_id = response.thread_id
            state.tasks = [TaskRecord(spec=task) for task in plan.tasks]
            state.final_verification_commands = [
                list(command) for command in plan.final_verification_commands
            ]
            for revision in state.revisions:
                revision.status = RevisionStatus.APPLIED
            state.transition(
                RunStatus.SCHEDULING,
                activity=f"Plan ready with {len(state.tasks)} task(s).",
            )

        self._update(apply_plan)

    async def _replan(self) -> None:
        if self.state.codex_thread_id is None:
            raise StateError("cannot replan without a Codex thread ID")
        if self.state.status == RunStatus.REPLAN_PENDING:
            self._update(
                lambda state: state.transition(
                    RunStatus.REPLANNING, activity="Codex is revising the task DAG."
                )
            )
        accepted = [
            {
                "id": task.spec.id,
                "title": task.spec.title,
                "changedPaths": task.patch.changed_paths if task.patch else [],
            }
            for task in self.state.tasks
            if task.status == TaskStatus.COMPLETED
        ]
        pending = [
            task.spec.to_contract()
            for task in self.state.tasks
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED}
        ]
        context = json.dumps(
            {
                "acceptedHistory": accepted,
                "pendingTasks": pending,
                "currentChangedPaths": self.inspector.changed_paths(),
            },
            ensure_ascii=False,
        )
        response = await self.codex.replan(
            repository=self.repository,
            artifact_dir=self.artifact_dir,
            thread_id=self.state.codex_thread_id,
            prompt=build_planning_prompt(
                goal=self.state.goal,
                requirements=self._requirements_text(),
                repository_rules=self._rules_text(),
                replan_context=context,
            ),
        )
        if not isinstance(response.value, Plan):
            raise StateError("Codex returned a review where a revised plan was required")
        plan = response.value
        completed_ids = {
            task.spec.id for task in self.state.tasks if task.status == TaskStatus.COMPLETED
        }
        new_ids = {task.id for task in plan.tasks}
        conflict = completed_ids & new_ids
        if conflict:
            raise StateError(f"replan attempted to replace completed task IDs: {sorted(conflict)}")

        def apply_replan(state: RunState) -> None:
            for task in state.tasks:
                if task.status not in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED}:
                    task.status = TaskStatus.SUPERSEDED
            state.tasks.extend(TaskRecord(spec=task) for task in plan.tasks)
            state.final_verification_commands = [
                list(command) for command in plan.final_verification_commands
            ]
            for revision in state.revisions:
                if revision.status in {RevisionStatus.QUEUED, RevisionStatus.PLANNING}:
                    revision.status = RevisionStatus.APPLIED
            state.transition(
                RunStatus.SCHEDULING,
                activity=f"Replan ready with {len(plan.tasks)} new task(s).",
            )

        self._update(apply_replan)

    def _prepare_resumed_tasks(self) -> None:
        def resume_state(state: RunState) -> None:
            for task in state.tasks:
                if (
                    task.status
                    in {
                        TaskStatus.RUNNING,
                        TaskStatus.VERIFYING,
                        TaskStatus.REVIEWING,
                        TaskStatus.BLOCKED,
                    }
                    and task.attempt < state.config.max_task_attempts
                ):
                    task.status = TaskStatus.RETRYING
                    task.blocker = None
            state.blocker = None
            state.transition(
                RunStatus.SCHEDULING,
                activity="Recovered state; scheduling the next safe task.",
            )

        self._update(resume_state)

    async def _recover_if_needed(self) -> None:
        pending = [
            task
            for task in self.state.tasks
            if task.status in {TaskStatus.READY_TO_INTEGRATE, TaskStatus.INTEGRATING}
            and task.patch is not None
        ]
        if not pending:
            return
        for task in sorted(pending, key=lambda item: item.spec.id):
            assert task.patch is not None
            self.patches.apply(task.patch)
            old_worktree = Path(task.worktree_path) if task.worktree_path else None

            def recovered(state: RunState, task_id: str = task.spec.id) -> None:
                record = self._task(state, task_id)
                record.status = TaskStatus.COMPLETED
                assert record.patch is not None
                record.patch.integrated = True
                if task_id not in state.accepted_patch_ids:
                    state.accepted_patch_ids.append(task_id)
                state.working_tree_digest = self.inspector.worktree_digest()
                state.last_integration = f"Recovered already-applied {task_id}.patch."
                record.worktree_path = None

            self._update(recovered)
            if old_worktree is not None and old_worktree.is_dir():
                self.worktrees.cleanup_path(old_worktree, force=True)
                parent = old_worktree.parent
                if parent.name.startswith(f"codex-claude-{self.state.run_id}-"):
                    with suppress(OSError):
                        parent.rmdir()

    async def _execute_wave(self, wave: list[TaskRecord]) -> None:
        task_ids = [task.spec.id for task in wave]

        def prepare(state: RunState) -> None:
            if state.status == RunStatus.SCHEDULING:
                state.transition(
                    RunStatus.EXECUTING,
                    activity=f"Running task wave: {', '.join(task_ids)}.",
                )
            for task_id in task_ids:
                task = self._task(state, task_id)
                if task.status == TaskStatus.PENDING:
                    validate_task_transition(task.status, TaskStatus.READY)
                    task.status = TaskStatus.READY
            state.active_task_ids = task_ids

        self._update(prepare)
        outcomes = await asyncio.gather(
            *[
                self._execute_task(task_id, f"W{index + 1:02d}")
                for index, task_id in enumerate(task_ids)
            ]
        )
        current = self.store.load()
        if current.status == RunStatus.EXECUTING:
            self._update(
                lambda state: state.transition(
                    RunStatus.INTEGRATING,
                    activity=f"Integrating accepted patches from {', '.join(task_ids)}.",
                )
            )
        for outcome in sorted(outcomes, key=lambda item: item.task_id):
            if outcome.accepted:
                assert outcome.patch is not None
                try:
                    self.patches.apply(outcome.patch)
                except IntegrationError as exc:
                    self._mark_integration_blocked(outcome.task_id, str(exc))
                    continue
                self._mark_integrated(outcome.task_id)
                self.worktrees.cleanup(outcome.worktree, force=True)
            else:
                self._mark_task_blocked(
                    outcome.task_id, outcome.blocker or "Task did not produce an accepted patch"
                )
        self.worktrees.cleanup_base()
        current = self.store.load()
        if current.status == RunStatus.INTEGRATING:
            self._update(
                lambda state: state.transition(
                    RunStatus.SCHEDULING, activity="Wave integration complete."
                )
            )
        elif current.status == RunStatus.REPLAN_PENDING:
            self._update(
                lambda state: state.transition(
                    RunStatus.REPLANNING,
                    activity="Applying requirement revision at a safe boundary.",
                )
            )

    def _accepted_patch_paths(self) -> tuple[Path, ...]:
        paths = []
        for task_id in self.state.accepted_patch_ids:
            task = self._task(self.state, task_id)
            if task.patch is None:
                raise StateError(f"accepted task is missing its patch: {task_id}")
            paths.append(self.patches.verify_artifact(task.patch))
        return tuple(paths)

    def _existing_or_new_worktree(self, task: TaskRecord, worker_id: str) -> WorkerWorktree:
        if task.worktree_path:
            path = Path(task.worktree_path)
            if path.is_dir():
                return WorkerWorktree(task.spec.id, worker_id, path)
        worktree = self.worktrees.create(
            task_id=task.spec.id,
            worker_id=worker_id,
            initial_head=self.state.initial_head,
            accepted_patches=self._accepted_patch_paths(),
        )

        def remember(state: RunState) -> None:
            record = self._task(state, task.spec.id)
            record.worker_id = worker_id
            record.worktree_path = str(worktree.path)

        self._update(remember)
        return worktree

    async def _execute_task(self, task_id: str, worker_id: str) -> TaskOutcome:
        task = self._task(self.store.load(), task_id)
        worktree = self._existing_or_new_worktree(task, worker_id)
        retry_context = ""
        continuation_count = 0
        while True:
            current = self._task(self.store.load(), task_id)
            if current.attempt >= self.state.config.max_task_attempts:
                return TaskOutcome(task_id, worktree, blocker="Task attempt budget exhausted")
            session_id = None
            if current.sessions and not current.sessions[-1].rotated:
                session_id = current.sessions[-1].session_id

            def start_attempt(state: RunState) -> None:
                record = self._task(state, task_id)
                if record.status in {TaskStatus.READY, TaskStatus.RETRYING}:
                    validate_task_transition(record.status, TaskStatus.RUNNING)
                    record.status = TaskStatus.RUNNING
                record.attempt += 1
                record.worker_id = worker_id
                record.worktree_path = str(worktree.path)

            self._update(start_attempt)
            task = self._task(self.state, task_id)
            while True:
                try:
                    result = await self._run_claude_until_boundary(
                        task,
                        worktree,
                        session_id=session_id,
                        retry_context=retry_context,
                        continuation_count=continuation_count,
                    )
                except ProcessError as exc:
                    return TaskOutcome(task_id, worktree, blocker=str(exc))
                if result.scope_change_required:
                    return TaskOutcome(
                        task_id,
                        worktree,
                        blocker="Claude requires a write-scope change: "
                        + "; ".join(result.remaining_work),
                    )
                if result.completed:
                    break
                continuation_count += 1
                if continuation_count > self.state.config.max_context_rotations + 2:
                    return TaskOutcome(
                        task_id,
                        worktree,
                        blocker="Claude did not complete at a safe boundary",
                    )
                retry_context = self._handoff_text(result)
                session_id = None if result.session.rotated else result.session.session_id
            self._set_task_status(task_id, TaskStatus.VERIFYING)
            verification = await self.verifier.run_all(
                task.spec.verification_commands, cwd=worktree.path
            )

            def store_verification(
                state: RunState,
                results: list[VerificationResult] = verification,
            ) -> None:
                record = self._task(state, task_id)
                record.verification = results
                validate_task_transition(record.status, TaskStatus.REVIEWING)
                record.status = TaskStatus.REVIEWING

            self._update(store_verification)
            worktree_inspector = GitInspector(worktree.path)
            diff = worktree_inspector.diff_binary().decode("utf-8", errors="replace")
            evidence = self._task_evidence(task_id, diff, verification, result)
            async with self._codex_lock:
                review_response = await self.codex.review(
                    repository=worktree.path,
                    artifact_dir=self.artifact_dir,
                    thread_id=self.state.codex_thread_id or "",
                    prompt=build_review_prompt(task=task_id, evidence=evidence),
                )
            if not isinstance(review_response.value, Review):
                raise StateError("Codex returned a plan where a review was required")
            review = review_response.value
            verified = all(item.exit_code == 0 and not item.timed_out for item in verification)
            if review.verdict == "PASS" and verified:
                try:
                    patch = self.patches.capture(worktree=worktree.path, task=task.spec)
                except IntegrationError as exc:
                    self._set_task_status(task_id, TaskStatus.INTEGRATION_BLOCKED, blocker=str(exc))
                    return TaskOutcome(task_id, worktree, blocker=str(exc))

                def ready(
                    state: RunState,
                    accepted_patch: PatchRecord = patch,
                ) -> None:
                    record = self._task(state, task_id)
                    validate_task_transition(record.status, TaskStatus.READY_TO_INTEGRATE)
                    record.status = TaskStatus.READY_TO_INTEGRATE
                    record.patch = accepted_patch

                self._update(ready)
                return TaskOutcome(task_id, worktree, patch=patch)
            if review.verdict == "BLOCKED":
                return TaskOutcome(task_id, worktree, blocker="; ".join(review.findings))
            if task.attempt >= self.state.config.max_task_attempts:
                return TaskOutcome(
                    task_id,
                    worktree,
                    blocker="Verification/review failed and task attempt budget was exhausted",
                )
            retry_context = review.retry_instruction or self._verification_failure(verification)
            self._set_retrying(task_id, retry_context)

    async def _run_claude_until_boundary(
        self,
        task: TaskRecord,
        worktree: WorkerWorktree,
        *,
        session_id: str | None,
        retry_context: str,
        continuation_count: int,
    ) -> ClaudeResult:
        result = await self.claude.execute(
            worktree=worktree.path,
            prompt=build_task_prompt(
                goal=self.state.goal,
                task=task.spec,
                requirements=self._requirements_text(),
                repository_rules=self._rules_text(task.spec.relevant_paths),
                retry_context=retry_context,
            ),
            task=task.spec,
            session_id=session_id,
        )

        def remember_session(state: RunState) -> None:
            self._task(state, task.spec.id).sessions.append(result.session)

        self._update(remember_session)
        rotations = sum(
            session.rotated for session in self._task(self.state, task.spec.id).sessions
        )
        if rotations > self.state.config.max_context_rotations:
            raise ProcessError("Claude context rotation budget exhausted")
        if (
            result.session.rotated
            and not result.completed
            and continuation_count >= self.state.config.max_context_rotations
        ):
            raise ProcessError("Claude context rotation budget exhausted")
        return result

    @staticmethod
    def _handoff_text(result: ClaudeResult) -> str:
        remaining = "; ".join(result.remaining_work) or "continue the current task"
        return f"Previous session summary: {result.summary}. Remaining work: {remaining}"

    @staticmethod
    def _verification_failure(results: Sequence[object]) -> str:
        return (
            "Verification failed. Inspect the recorded command results "
            f"({len(results)} command(s))."
        )

    @staticmethod
    def _task_evidence(
        task_id: str,
        diff: str,
        verification: Sequence[object],
        result: ClaudeResult,
    ) -> str:
        return json.dumps(
            {
                "taskId": task_id,
                "claudeSummary": result.summary,
                "diff": diff[:100_000],
                "verification": [str(item) for item in verification],
            },
            ensure_ascii=False,
        )

    def _set_retrying(self, task_id: str, reason: str) -> None:
        self._set_task_status(task_id, TaskStatus.RETRYING, blocker=reason)

    def _set_task_status(
        self, task_id: str, status: TaskStatus, *, blocker: str | None = None
    ) -> None:
        def update(state: RunState) -> None:
            task = self._task(state, task_id)
            validate_task_transition(task.status, status)
            task.status = status
            task.blocker = blocker

        self._update(update)

    def _mark_task_blocked(self, task_id: str, reason: str) -> None:
        current = self._task(self.store.load(), task_id)
        if current.status not in {
            TaskStatus.BLOCKED,
            TaskStatus.INTEGRATION_BLOCKED,
        }:
            try:
                self._set_task_status(task_id, TaskStatus.BLOCKED, blocker=reason)
            except StateError:
                self._set_task_status(task_id, TaskStatus.INTEGRATION_BLOCKED, blocker=reason)

        def inactive(state: RunState) -> None:
            state.active_task_ids = [
                active for active in state.active_task_ids if active != task_id
            ]

        self._update(inactive)

    def _mark_integration_blocked(self, task_id: str, reason: str) -> None:
        self._set_task_status(task_id, TaskStatus.INTEGRATION_BLOCKED, blocker=reason)
        self._update(
            lambda state: setattr(
                state,
                "active_task_ids",
                [active for active in state.active_task_ids if active != task_id],
            )
        )

    def _mark_integrated(self, task_id: str) -> None:
        def integrated(state: RunState) -> None:
            task = self._task(state, task_id)
            validate_task_transition(task.status, TaskStatus.INTEGRATING)
            task.status = TaskStatus.INTEGRATING
            validate_task_transition(task.status, TaskStatus.COMPLETED)
            task.status = TaskStatus.COMPLETED
            if task.patch is None:
                raise StateError(f"integrated task is missing patch: {task_id}")
            task.patch.integrated = True
            task.worktree_path = None
            if task_id not in state.accepted_patch_ids:
                state.accepted_patch_ids.append(task_id)
            state.active_task_ids = [
                active for active in state.active_task_ids if active != task_id
            ]
            state.working_tree_digest = self.inspector.worktree_digest()
            state.last_integration = f"{task_id}.patch applied successfully."

        self._update(integrated)

    async def _finalize(self) -> int:
        commands = tuple(tuple(command) for command in self.state.final_verification_commands)
        results = await self.verifier.run_all(commands, cwd=self.repository)
        verified = all(result.exit_code == 0 and not result.timed_out for result in results)
        diff = self.inspector.diff_binary().decode("utf-8", errors="replace")
        async with self._codex_lock:
            response = await self.codex.review(
                repository=self.repository,
                artifact_dir=self.artifact_dir,
                thread_id=self.state.codex_thread_id or "",
                prompt=build_review_prompt(
                    task="FINAL",
                    evidence=json.dumps(
                        {
                            "diff": diff[:200_000],
                            "verification": [str(result) for result in results],
                        },
                        ensure_ascii=False,
                    ),
                    final=True,
                ),
            )
        if not isinstance(response.value, Review):
            raise StateError("Codex returned a plan where a final review was required")
        if not verified or response.value.verdict != "PASS":
            reason = (
                "; ".join(response.value.findings)
                or "Final verification or Codex review did not pass"
            )
            self._block(reason)
            return 2

        def completed(state: RunState) -> None:
            if state.status == RunStatus.SCHEDULING:
                state.transition(RunStatus.INTEGRATING, activity="Final review passed.")
            state.transition(
                RunStatus.COMPLETED,
                activity="All tasks, final verification and final review passed.",
            )
            state.working_tree_digest = self.inspector.worktree_digest()
            state.blocker = None
            state.active_task_ids = []

        self._update(completed)
        self.worktrees.cleanup_base()
        return 0


def add_requirement(repository: Path, requirement: str) -> str:
    loaded: LoadedInput = load_prompt(requirement)
    inspector = discover_repository(repository)
    store = ProcessStore(inspector)
    state = store.load()
    if inspector.head != state.initial_head:
        raise GitError("repository HEAD changed since the run started")
    if state.working_tree_digest != inspector.worktree_digest():
        raise GitError("working tree differs from the last controller checkpoint")

    revision_id = ""

    def append(current: RunState) -> None:
        nonlocal revision_id
        revision = create_revision(
            loaded,
            git_common_dir=inspector.common_dir,
            run_id=current.run_id,
            revision_number=len(current.revisions) + 1,
        )
        revision_id = revision.id
        current.revisions.append(revision)
        if current.status == RunStatus.COMPLETED:
            current.transition(
                RunStatus.REPLANNING,
                activity=f"Queued {revision.id}; run reopened for replanning.",
            )
        elif current.status not in {
            RunStatus.PAUSED,
            RunStatus.BLOCKED,
            RunStatus.REPLAN_PENDING,
            RunStatus.REPLANNING,
        }:
            current.transition(
                RunStatus.REPLAN_PENDING,
                activity=f"Queued {revision.id}; waiting for a safe boundary.",
            )
        else:
            current.current_activity = f"Queued {revision.id}; it will be applied on resume/replan."

    store.update(append)
    return revision_id
