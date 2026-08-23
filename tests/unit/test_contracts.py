from __future__ import annotations

import pytest

from codex_claude.errors import ValidationError
from codex_claude.state import Plan, Review


def valid_plan() -> dict[str, object]:
    return {
        "summary": "Implement",
        "tasks": [
            {
                "id": "T01",
                "title": "Task",
                "objective": "Make it work",
                "acceptanceCriteria": ["tests pass"],
                "requirementRefs": ["R01"],
                "dependsOn": [],
                "writeScopes": ["src/"],
                "resourceLocks": [],
                "parallelSafe": True,
                "verificationCommands": [["pytest"]],
                "relevantPaths": ["src/"],
            }
        ],
        "finalVerificationCommands": [["pytest"]],
    }


def test_plan_rejects_shell_command_string() -> None:
    value = valid_plan()
    task = value["tasks"][0]  # type: ignore[index]
    task["verificationCommands"] = ["pytest"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        Plan.from_dict(value)


def test_retry_review_requires_instruction() -> None:
    with pytest.raises(ValidationError):
        Review.from_dict(
            {
                "verdict": "RETRY",
                "findings": ["failure"],
                "retryInstruction": None,
                "scopeChange": None,
            }
        )
