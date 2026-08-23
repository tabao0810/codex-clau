from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .errors import ValidationError
from .security import scopes_overlap
from .state import TaskRecord, TaskStatus

_GLOBAL_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "package.json",
    "turbo.json",
}
_GLOBAL_PREFIXES = (
    "migrations/",
    "prisma/",
    "database/",
)


def is_global_scope(scope: str) -> bool:
    stripped = scope.rstrip("/")
    return stripped in _GLOBAL_NAMES or any(
        stripped == prefix.rstrip("/") or scope.startswith(prefix) for prefix in _GLOBAL_PREFIXES
    )


class Scheduler:
    def __init__(self, tasks: Iterable[TaskRecord], *, max_workers: int = 2) -> None:
        self.tasks = list(tasks)
        self.max_workers = max_workers
        self._validate()

    def _validate(self) -> None:
        ids = [task.spec.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValidationError("task IDs must be unique")
        known = set(ids)
        graph: dict[str, tuple[str, ...]] = {}
        for task in self.tasks:
            unknown = set(task.spec.depends_on) - known
            if unknown:
                raise ValidationError(f"{task.spec.id} has unknown dependencies: {sorted(unknown)}")
            graph[task.spec.id] = task.spec.depends_on
        indegree = {task_id: 0 for task_id in ids}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in ids}
        for task_id, dependencies in graph.items():
            indegree[task_id] = len(dependencies)
            for dependency in dependencies:
                dependents[dependency].append(task_id)
        queue = deque(task_id for task_id in ids if indegree[task_id] == 0)
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)
        if visited != len(ids):
            raise ValidationError("task dependency graph contains a cycle")

    @staticmethod
    def compatible(left: TaskRecord, right: TaskRecord) -> bool:
        if not left.spec.parallel_safe or not right.spec.parallel_safe:
            return False
        if set(left.spec.resource_locks) & set(right.spec.resource_locks):
            return False
        all_scopes = (*left.spec.write_scopes, *right.spec.write_scopes)
        if any(is_global_scope(scope) for scope in all_scopes):
            return False
        return not any(
            scopes_overlap(lhs, rhs)
            for lhs in left.spec.write_scopes
            for rhs in right.spec.write_scopes
        )

    def ready(self) -> list[TaskRecord]:
        completed = {task.spec.id for task in self.tasks if task.status == TaskStatus.COMPLETED}
        return [
            task
            for task in self.tasks
            if task.status
            in {
                TaskStatus.PENDING,
                TaskStatus.READY,
                TaskStatus.RETRYING,
            }
            and set(task.spec.depends_on) <= completed
        ]

    def next_wave(self) -> list[TaskRecord]:
        selected: list[TaskRecord] = []
        for task in self.ready():
            if len(selected) >= self.max_workers:
                break
            if all(self.compatible(task, existing) for existing in selected):
                selected.append(task)
        return selected
