from __future__ import annotations

from pathlib import Path

from .errors import ProcessTimeoutError
from .security import validate_command
from .state import VerificationResult
from .subprocess_runner import SubprocessRunner, output_digest


class Verifier:
    def __init__(self, runner: SubprocessRunner, *, timeout: int = 900) -> None:
        self.runner = runner
        self.timeout = timeout

    async def run(self, argv: tuple[str, ...], *, cwd: Path) -> VerificationResult:
        command = validate_command(argv)
        try:
            result = await self.runner.run(command, cwd=cwd, timeout=self.timeout)
        except ProcessTimeoutError:
            return VerificationResult(
                argv=command,
                exit_code=None,
                duration_seconds=float(self.timeout),
                timed_out=True,
                stderr_tail=f"command exceeded timeout after {self.timeout} seconds",
            )
        return VerificationResult(
            argv=command,
            exit_code=result.returncode,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
            stdout_tail=result.stdout[-8000:],
            stderr_tail=result.stderr[-8000:],
            output_sha256=output_digest(result.stdout, result.stderr),
        )

    async def run_all(
        self, commands: tuple[tuple[str, ...], ...], *, cwd: Path
    ) -> list[VerificationResult]:
        results: list[VerificationResult] = []
        for command in commands:
            result = await self.run(command, cwd=cwd)
            results.append(result)
            if result.exit_code != 0 or result.timed_out:
                break
        return results
