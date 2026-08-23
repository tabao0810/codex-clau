from __future__ import annotations

import json

from .state import RunState

STATE_START = "<!-- codex-claude:state:start -->"
STATE_END = "<!-- codex-claude:state:end -->"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_process(state: RunState) -> str:
    active = ", ".join(state.active_task_ids) or "none"
    lines = [
        "# Codex-Claude Process",
        "",
        f"Run: {state.run_id}",
        f"Goal: {_cell(state.goal)}",
        f"Status: {state.status.value}",
        f"Active tasks: {active}",
        f"Updated: {state.updated_at}",
        "",
        "| Revision | Type | Source/Summary | Status |",
        "| --- | --- | --- | --- |",
    ]
    for revision in state.revisions:
        source = revision.source_path or revision.summary
        lines.append(
            f"| {_cell(revision.id)} | {_cell(revision.type)} | "
            f"{_cell(source)} | {revision.status.value} |"
        )
    if not state.revisions:
        lines.append("| — | — | — | — |")
    lines.extend(
        [
            "",
            "| Task | Worker | Status | Attempt | Worktree | Context |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for task in state.tasks:
        context = "n/a"
        if task.sessions and task.sessions[-1].used_percent is not None:
            context = f"{task.sessions[-1].used_percent:.1f}%"
        lines.append(
            f"| {_cell(task.spec.id)} | {_cell(task.worker_id or '—')} | "
            f"{task.status.value} | {task.attempt}/{state.config.max_task_attempts} | "
            f"{_cell(task.worktree_path or '—')} | {context} |"
        )
    if not state.tasks:
        lines.append("| — | — | — | — | — | — |")
    lines.extend(
        [
            "",
            "## Current activity",
            "",
            state.current_activity or "No active operation.",
            "",
            "## Last integration",
            "",
            state.last_integration or "No patch has been integrated.",
            "",
            STATE_START,
            "```json",
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            STATE_END,
            "",
        ]
    )
    return "\n".join(lines)


def parse_process(text: str) -> RunState:
    if text.count(STATE_START) != 1 or text.count(STATE_END) != 1:
        raise ValueError("process.md does not contain exactly one tool state block")
    payload = text.split(STATE_START, 1)[1].split(STATE_END, 1)[0].strip()
    if not payload.startswith("```json") or not payload.endswith("```"):
        raise ValueError("process.md state block is malformed")
    raw_json = payload[len("```json") : -len("```")].strip()
    value = json.loads(raw_json)
    if not isinstance(value, dict):
        raise ValueError("process.md state must be an object")
    return RunState.from_dict(value)
