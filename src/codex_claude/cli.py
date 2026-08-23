from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import RunConfig
from .errors import OrchestratorError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-claude",
        description="Let Codex plan/review and Claude Code edit isolated Git worktrees.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="start a new orchestration run")
    run.add_argument("goal", nargs="?", help="goal or implementation prompt")
    run.add_argument("--spec", type=Path, help="UTF-8 .md or .txt requirements file")
    _add_common_options(run)

    add = subcommands.add_parser("add", help="append a requirement revision")
    add.add_argument("requirement", help="additional requirement prompt")
    add.add_argument("--cwd", type=Path, default=Path.cwd())

    resume = subcommands.add_parser("resume", help="resume the durable run in process.md")
    resume.add_argument("--cwd", type=Path, default=Path.cwd())
    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--claude-context-limit", type=float, default=55)
    parser.add_argument("--codex-timeout", type=int, default=600)
    parser.add_argument("--claude-timeout", type=int, default=1800)
    parser.add_argument("--verification-timeout", type=int, default=900)


def _config_from_args(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        max_workers=args.max_workers,
        claude_context_limit=args.claude_context_limit,
        codex_timeout=args.codex_timeout,
        claude_timeout=args.claude_timeout,
        verification_timeout=args.verification_timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            from .controller import Controller

            controller = Controller.start(
                repository=args.cwd,
                prompt=args.goal,
                spec=args.spec,
                config=_config_from_args(args),
            )
            exit_code = controller.run()
            state = controller.store.load()
            print(f"Run {state.run_id}: {state.status.value}. State: {controller.store.path}")
            return exit_code
        if args.command == "add":
            from .controller import add_requirement

            revision_id = add_requirement(args.cwd, args.requirement)
            print(f"Queued requirement revision {revision_id} in process.md")
            return 0
        if args.command == "resume":
            from .controller import Controller

            controller = Controller.resume(args.cwd)
            exit_code = controller.run()
            state = controller.store.load()
            print(f"Run {state.run_id}: {state.status.value}. State: {controller.store.path}")
            return exit_code
        parser.error(f"unknown command: {args.command}")
    except OrchestratorError as exc:
        print(f"codex-claude: {exc}", file=sys.stderr)
        return 2
    return 2
