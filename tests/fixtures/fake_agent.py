from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value), flush=True)


def task_contract(
    task_id: str,
    target: str,
    requirement: str = "R01",
    *,
    write_scope: str | None = None,
) -> dict[str, object]:
    target_literal = repr(target)
    return {
        "id": task_id,
        "title": f"Create {target}",
        "objective": f"Create {target}",
        "acceptanceCriteria": [f"{target} exists"],
        "requirementRefs": [requirement],
        "dependsOn": [],
        "writeScopes": [write_scope or target],
        "resourceLocks": [],
        "parallelSafe": True,
        "verificationCommands": [
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; assert Path({target_literal}).is_file()",
            ]
        ],
        "relevantPaths": ["README.md"],
    }


def full_plan(*, replan: bool) -> tuple[list[dict[str, object]], list[str]]:
    if replan:
        return [task_contract("T07", "added.txt", "R02")], [
            "parallel-a.txt",
            "parallel-b.txt",
            "shared/first.txt",
            "shared/second.txt",
            "retry.txt",
            "rotate.txt",
            "added.txt",
        ]
    tasks = [
        task_contract("T01", "parallel-a.txt"),
        task_contract("T02", "parallel-b.txt"),
        task_contract("T03", "shared/first.txt", write_scope="shared/"),
        task_contract("T04", "shared/second.txt"),
        task_contract("T05", "retry.txt"),
        task_contract("T06", "rotate.txt"),
    ]
    tasks[4]["verificationCommands"] = [
        [
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('retry.txt').read_text() == 'implemented\\n'",
        ]
    ]
    return tasks, [
        "parallel-a.txt",
        "parallel-b.txt",
        "shared/first.txt",
        "shared/second.txt",
        "retry.txt",
        "rotate.txt",
    ]


def codex(args: list[str], scenario: str) -> int:
    prompt = sys.stdin.buffer.read().decode("utf-8")
    output_flag = args.index("--output-last-message")
    output = Path(args[output_flag + 1])
    is_review = "review" in output.name
    if is_review:
        final: dict[str, object] = {
            "verdict": "PASS",
            "findings": [],
            "retryInstruction": None,
            "scopeChange": None,
        }
    else:
        if scenario == "full":
            tasks, targets = full_plan(replan="resume" in args)
        elif scenario == "parallel":
            tasks = [
                task_contract("T01", "parallel-a.txt"),
                task_contract("T02", "parallel-b.txt"),
            ]
            targets = ["parallel-a.txt", "parallel-b.txt"]
        elif scenario == "unicode":
            target = "tính năng/đã xong.txt"
            tasks = [task_contract("T01", target)]
            targets = [target]
        else:
            task_id = "T02" if "resume" in args else "T01"
            target = "feature2.txt" if task_id == "T02" else "feature.txt"
            tasks = [
                task_contract(
                    task_id,
                    target,
                    "R02" if task_id == "T02" else "R01",
                )
            ]
            targets = [target]
        if scenario in {"retry", "alwaysfail"}:
            tasks[0]["verificationCommands"] = [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('feature.txt').read_text() == 'implemented\\n'",
                ]
            ]
            final_commands = tasks[0]["verificationCommands"]
        else:
            assertions = "; ".join(f"assert Path({target!r}).is_file()" for target in targets)
            final_commands = [
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; {assertions}",
                ]
            ]
        final = {
            "summary": "Fake plan",
            "tasks": tasks,
            "finalVerificationCommands": final_commands,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(final), encoding="utf-8")
    emit({"type": "thread.started", "thread_id": "thread-1"})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": prompt[:20]}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    return 0


def claude(args: list[str], scenario: str) -> int:
    prompt = sys.stdin.buffer.read().decode("utf-8")
    objective_prefix = "Objective: Create "
    target = next(
        (
            line.removeprefix(objective_prefix)
            for line in prompt.splitlines()
            if line.startswith(objective_prefix)
        ),
        "feature.txt",
    )
    target_path = Path(target)
    context_scenario = scenario == "context" or (scenario == "full" and target == "rotate.txt")
    first_context_turn = context_scenario and not target_path.exists()
    if context_scenario and target_path.exists() and "--resume" in args:
        return 9
    if scenario in {"parallel", "full"}:
        time.sleep(0.25)
    retry_scenario = scenario == "retry" or (scenario == "full" and target == "retry.txt")
    if scenario == "alwaysfail" or (retry_scenario and not target_path.exists()):
        content = "incorrect\n"
    else:
        content = "partial\n" if first_context_turn else "implemented\n"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    session = str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": session})
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session,
            "result": "implemented",
            "structured_output": {
                "summary": "partial" if first_context_turn else "implemented",
                "completed": not first_context_turn,
                "scopeChangeRequired": False,
                "remainingWork": ["finish"] if first_context_turn else [],
            },
            "usage": {
                "input_tokens": 600 if first_context_turn else 50,
                "cache_creation_input_tokens": 0 if first_context_turn else 10,
                "cache_read_input_tokens": 0 if first_context_turn else 20,
            },
            "modelUsage": {"fake-model": {"contextWindow": 1000}},
        }
    )
    return 0


if __name__ == "__main__":
    mode, *arguments = sys.argv[1:]
    kind, _, scenario = mode.partition("-")
    raise SystemExit(codex(arguments, scenario) if kind == "codex" else claude(arguments, scenario))
