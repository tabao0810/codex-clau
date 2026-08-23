from __future__ import annotations

from pathlib import Path

from codex_claude.git_inspector import GitInspector
from codex_claude.process_store import ProcessStore
from codex_claude.state import RunState


def test_process_store_round_trip_and_exclude(git_repo: Path) -> None:
    inspector = GitInspector(git_repo)
    store = ProcessStore(inspector)
    state = RunState("run-1", str(git_repo), inspector.head, "Implement")
    store.create(state)
    restored = store.load()
    assert restored.run_id == "run-1"
    assert "/process.md" in (inspector.common_dir / "info" / "exclude").read_text(encoding="utf-8")
    assert "process.md" not in inspector.status_paths()
