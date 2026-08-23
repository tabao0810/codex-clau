from __future__ import annotations

import json
import sys
from pathlib import Path

from codex_claude.subprocess_runner import SubprocessRunner
from codex_claude.terminal_host import run_manifest


def test_auto_terminal_mode_keeps_custom_commands_hidden() -> None:
    runner = SubprocessRunner(terminal_mode="auto")
    assert not runner._use_visible_terminal((sys.executable, "fake-agent.py"), "codex")


def test_terminal_host_captures_redacted_output(tmp_path: Path) -> None:
    stdin_path = tmp_path / "stdin.txt"
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    completion_path = tmp_path / "completion.json"
    manifest_path = tmp_path / "manifest.json"
    stdin_path.write_text("unused prompt", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print('stdout'); print('token=secret-value', file=sys.stderr)",
                ],
                "completionPath": str(completion_path),
                "cwd": str(tmp_path),
                "environment": {"PATH": str(Path(sys.executable).parent)},
                "keepOpen": False,
                "maxOutputBytes": 10_000,
                "stderrPath": str(stderr_path),
                "stdinPath": str(stdin_path),
                "stdoutPath": str(stdout_path),
            }
        ),
        encoding="utf-8",
    )

    assert run_manifest(manifest_path) == 0
    assert stdout_path.read_text(encoding="utf-8") == "stdout\n"
    assert stderr_path.read_text(encoding="utf-8") == "token=<redacted>\n"
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert completion["returncode"] == 0
    assert completion["error"] is None
