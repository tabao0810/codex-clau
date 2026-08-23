from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
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
    def __init__(
        self,
        *,
        max_output_bytes: int = 1_000_000,
        terminal_mode: str = "auto",
        terminal_artifact_dir: Path | None = None,
    ) -> None:
        self.max_output_bytes = max_output_bytes
        self.terminal_mode = terminal_mode
        self.terminal_artifact_dir = terminal_artifact_dir

    def _use_visible_terminal(self, argv: Sequence[str], terminal_kind: str | None) -> bool:
        if terminal_kind is None or self.terminal_mode == "hidden":
            return False
        if self.terminal_mode == "visible":
            if os.name != "nt":
                raise ProcessError("visible terminal mode is currently supported only on Windows")
            return True
        if self.terminal_mode != "auto":
            raise ProcessError(f"unknown terminal mode: {self.terminal_mode}")
        executable = Path(argv[0]).name.lower().removesuffix(".exe").removesuffix(".cmd")
        return os.name == "nt" and executable == terminal_kind

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent, text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _dispatch_jsonl(self, output: str, on_event: EventCallback | None) -> None:
        for line in output.splitlines():
            decoded = line.strip()
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

    async def _run_visible_terminal(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout: float,
        env: Mapping[str, str] | None,
        parse_jsonl: bool,
        on_event: EventCallback | None,
        keep_terminal_open: bool,
    ) -> ProcessResult:
        if self.terminal_artifact_dir is None:
            raise ProcessError("visible terminal mode requires a run artifact directory")
        invocation_dir = self.terminal_artifact_dir / uuid.uuid4().hex
        stdin_path = invocation_dir / "stdin.txt"
        stdout_path = invocation_dir / "stdout.log"
        stderr_path = invocation_dir / "stderr.log"
        completion_path = invocation_dir / "completion.json"
        manifest_path = invocation_dir / "manifest.json"
        invocation_dir.mkdir(parents=True, exist_ok=True)
        stdin_path.write_text(stdin, encoding="utf-8", newline="\n")
        self._atomic_json(
            manifest_path,
            {
                "argv": list(argv),
                "completionPath": str(completion_path),
                "cwd": str(cwd),
                "environment": sanitize_environment(env),
                "keepOpen": keep_terminal_open,
                "maxOutputBytes": self.max_output_bytes,
                "stderrPath": str(stderr_path),
                "stdinPath": str(stdin_path),
                "stdoutPath": str(stdout_path),
            },
        )
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "codex_claude.terminal_host",
                "--manifest",
                str(manifest_path),
                cwd=cwd,
                env=sanitize_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            raise ProcessError(f"cannot start visible terminal for {argv[0]}: {exc}") from exc
        try:
            async with asyncio.timeout(timeout):
                while not completion_path.is_file():
                    if process.returncode is not None:
                        raise ProcessError(
                            f"terminal host exited before reporting completion for {argv[0]}"
                        )
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            await self._stop(process)
            raise ProcessTimeoutError(
                f"{argv[0]} exceeded timeout after {timeout:g} seconds"
            ) from exc
        except BaseException:
            if not keep_terminal_open:
                await self._stop(process)
            raise
        try:
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessError(f"cannot read terminal completion for {argv[0]}: {exc}") from exc
        if not isinstance(completion, dict):
            raise ProcessError(f"terminal completion is malformed for {argv[0]}")
        returncode = completion.get("returncode")
        error = completion.get("error")
        duration = completion.get("durationSeconds")
        if not isinstance(returncode, int) or not isinstance(duration, (int, float)):
            raise ProcessError(f"terminal completion is malformed for {argv[0]}")
        if error is not None:
            if not isinstance(error, str):
                raise ProcessError(f"terminal completion is malformed for {argv[0]}")
            raise ProcessError(f"terminal host failed for {argv[0]}: {error}")
        try:
            stdout = redact(stdout_path.read_text(encoding="utf-8", errors="replace"))
            stderr = redact(stderr_path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise ProcessError(f"cannot read terminal output for {argv[0]}: {exc}") from exc
        if parse_jsonl:
            await self._dispatch_jsonl(stdout, on_event)
        if not keep_terminal_open:
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                await self._stop(process)
        return ProcessResult(
            argv=tuple(argv),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=float(duration),
        )

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
        terminal_kind: str | None = None,
        keep_terminal_open: bool = False,
    ) -> ProcessResult:
        if not argv:
            raise ProcessError("cannot run an empty command")
        if self._use_visible_terminal(argv, terminal_kind):
            return await self._run_visible_terminal(
                argv,
                cwd=cwd,
                stdin=stdin,
                timeout=timeout,
                env=env,
                parse_jsonl=parse_jsonl,
                on_event=on_event,
                keep_terminal_open=keep_terminal_open,
            )
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
