from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ContractError, ProcessError, ProcessTimeoutError
from .security import redact, sanitize_environment

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


class SubprocessRunner:
    def __init__(self, *, max_output_bytes: int = 1_000_000) -> None:
        self.max_output_bytes = max_output_bytes

    async def _stop(self, process: asyncio.subprocess.Process, grace: float = 3.0) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                getattr(os, "killpg")(process.pid, signal.SIGINT)  # noqa: B009
            await asyncio.wait_for(process.wait(), grace)
            return
        except (ProcessLookupError, TimeoutError):
            pass
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                getattr(os, "killpg")(  # noqa: B009
                    process.pid,
                    getattr(signal, "SIGKILL"),  # noqa: B009
                )
            except ProcessLookupError:
                return
        await process.wait()

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str = "",
        timeout: float,
        env: Mapping[str, str] | None = None,
        parse_jsonl: bool = False,
        on_event: EventCallback | None = None,
    ) -> ProcessResult:
        if not argv:
            raise ProcessError("cannot run an empty command")
        started = time.monotonic()
        try:
            sanitized_env = sanitize_environment(env)
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=sanitized_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=sanitized_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except OSError as exc:
            raise ProcessError(f"cannot start {argv[0]}: {exc}") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            await self._stop(process)
            raise ProcessError("subprocess pipes were not created")

        stdout_buffer = bytearray()
        stderr_buffer = bytearray()

        async def consume(
            stream: asyncio.StreamReader,
            buffer: bytearray,
            *,
            events: bool,
        ) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                if len(buffer) < self.max_output_bytes:
                    remaining = self.max_output_bytes - len(buffer)
                    buffer.extend(line[:remaining])
                if events:
                    decoded = line.decode("utf-8", errors="replace").strip()
                    if not decoded:
                        continue
                    try:
                        event = json.loads(decoded)
                    except json.JSONDecodeError as exc:
                        raise ContractError(f"invalid JSONL event: {decoded[:200]}") from exc
                    if not isinstance(event, dict):
                        raise ContractError("JSONL event must be an object")
                    if on_event is not None:
                        callback_result = on_event(event)
                        if callback_result is not None:
                            await callback_result

        stdout_task = asyncio.create_task(
            consume(process.stdout, stdout_buffer, events=parse_jsonl)
        )
        stderr_task = asyncio.create_task(consume(process.stderr, stderr_buffer, events=False))
        process.stdin.write(stdin.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(stdout_task, stderr_task, process.wait())
        except TimeoutError as exc:
            timed_out = True
            stdout_task.cancel()
            stderr_task.cancel()
            await self._stop(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise ProcessTimeoutError(
                f"{argv[0]} exceeded timeout after {timeout:g} seconds"
            ) from exc
        except BaseException:
            await self._stop(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        stdout = redact(stdout_buffer.decode("utf-8", errors="replace"))
        stderr = redact(stderr_buffer.decode("utf-8", errors="replace"))
        return ProcessResult(
            argv=tuple(argv),
            returncode=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )


def output_digest(stdout: str, stderr: str) -> str:
    digest = hashlib.sha256()
    digest.update(stdout.encode("utf-8"))
    digest.update(b"\0")
    digest.update(stderr.encode("utf-8"))
    return digest.hexdigest()


def run_sync(
    runner: SubprocessRunner,
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: str = "",
    timeout: float,
    env: Mapping[str, str] | None = None,
    parse_jsonl: bool = False,
    on_event: EventCallback | None = None,
) -> ProcessResult:
    return asyncio.run(
        runner.run(
            argv,
            cwd=cwd,
            stdin=stdin,
            timeout=timeout,
            env=env,
            parse_jsonl=parse_jsonl,
            on_event=on_event,
        )
    )
