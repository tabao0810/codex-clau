from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from codex_claude.subprocess_runner import SubprocessRunner


def test_runner_streams_jsonl(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        events.append(event)

    result = asyncio.run(
        SubprocessRunner().run(
            [
                sys.executable,
                "-c",
                "import json; print(json.dumps({'type': 'ok'}), flush=True)",
            ],
            cwd=tmp_path,
            timeout=5,
            parse_jsonl=True,
            on_event=capture,
        )
    )
    assert result.returncode == 0
    assert events == [{"type": "ok"}]


def test_runner_redacts_output(tmp_path: Path) -> None:
    code = f"print({json.dumps('token=very-secret')})"
    result = asyncio.run(
        SubprocessRunner().run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            timeout=5,
        )
    )
    assert "very-secret" not in result.stdout
    assert "token=<redacted>" in result.stdout
