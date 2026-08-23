from __future__ import annotations

import pytest

from codex_claude.config import RunConfig
from codex_claude.errors import StateError, ValidationError
from codex_claude.state import RunState, RunStatus, TaskStatus, validate_task_transition


def test_state_round_trip() -> None:
    state = RunState("run-1", "C:/repo", "abc", "Implement")
    restored = RunState.from_dict(state.to_dict())
    assert restored.run_id == "run-1"
    assert restored.config.claude_context_limit == 55


def test_invalid_run_transition_is_rejected() -> None:
    state = RunState("run-1", "C:/repo", "abc", "Implement")
    with pytest.raises(StateError):
        state.transition(RunStatus.COMPLETED)


def test_invalid_task_transition_is_rejected() -> None:
    with pytest.raises(StateError):
        validate_task_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_workers", 0),
        ("codex_timeout", -1),
        ("claude_context_limit", 101),
    ],
)
def test_run_config_rejects_invalid_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        RunConfig(**{field: value})
