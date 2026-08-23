import subprocess
import sys

from codex_claude import RunConfig


def test_package_exports_run_config() -> None:
    assert RunConfig().max_workers == 2


def test_module_help_lists_public_commands() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "codex_claude", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "{run,add,resume}" in completed.stdout


def test_run_help_exposes_terminal_mode() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "codex_claude", "run", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--terminal-mode" in completed.stdout
