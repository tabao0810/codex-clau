from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from codex_claude.subprocess_runner import SubprocessRunner
from codex_claude.verifier import Verifier


def test_verifier_records_exit_code(tmp_path: Path) -> None:
    result = asyncio.run(
        Verifier(SubprocessRunner(), timeout=5).run(
            (sys.executable, "-c", "print('ok')"),
            cwd=tmp_path,
        )
    )
    assert result.exit_code == 0
    assert result.output_sha256
