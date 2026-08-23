from __future__ import annotations

import pytest

from codex_claude.errors import ValidationError
from codex_claude.scheduler import Scheduler
from codex_claude.state import TaskRecord, TaskSpec


def record(task_id: str, scope: str, *, depends_on: tuple[str, ...] = ()) -> TaskRecord:
    return TaskRecord(
        TaskSpec(
            id=task_id,
            title=task_id,
            objective=task_id,
            acceptance_criteria=("done",),
            requirement_refs=("R01",),
            depends_on=depends_on,
            write_scopes=(scope,),
            resource_locks=(),
            parallel_safe=True,
            verification_commands=(),
            relevant_paths=(),
        )
    )


def test_independent_tasks_share_wave() -> None:
    scheduler = Scheduler([record("T01", "a/"), record("T02", "b/")], max_workers=2)
    assert [task.spec.id for task in scheduler.next_wave()] == ["T01", "T02"]


def test_overlapping_scopes_are_serialized() -> None:
    scheduler = Scheduler([record("T01", "src/"), record("T02", "src/module.py")], max_workers=2)
    assert [task.spec.id for task in scheduler.next_wave()] == ["T01"]


def test_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Scheduler(
            [
                record("T01", "a/", depends_on=("T02",)),
                record("T02", "b/", depends_on=("T01",)),
            ]
        )
