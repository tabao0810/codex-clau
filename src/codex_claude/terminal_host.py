from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, TextIO, cast

from .security import redact


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"terminal manifest {name} must be a non-empty string")
    return value


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read terminal manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("terminal manifest must be an object")
    required = {
        "argv",
        "cwd",
        "stdinPath",
        "stdoutPath",
        "stderrPath",
        "completionPath",
        "environment",
        "maxOutputBytes",
        "keepOpen",
    }
    if set(value) != required:
        raise ValueError("terminal manifest has unexpected fields")
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ValueError("terminal manifest argv must be a non-empty string array")
    for name in ("cwd", "stdinPath", "stdoutPath", "stderrPath", "completionPath"):
        _require_string(value[name], name)
    environment = value["environment"]
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in environment.items()
    ):
        raise ValueError("terminal manifest environment must be a string mapping")
    if not isinstance(value["maxOutputBytes"], int) or value["maxOutputBytes"] <= 0:
        raise ValueError("terminal manifest maxOutputBytes must be positive")
    if not isinstance(value["keepOpen"], bool):
        raise ValueError("terminal manifest keepOpen must be boolean")
    return cast(dict[str, object], value)


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


def _pump(
    source: BinaryIO,
    artifact: BinaryIO,
    console: TextIO,
    *,
    max_output_bytes: int,
    truncated: list[bool],
) -> None:
    written = 0
    while True:
        chunk = source.readline()
        if not chunk:
            return
        if written >= max_output_bytes:
            truncated[0] = True
            continue
        accepted = chunk[: max_output_bytes - written]
        if len(accepted) != len(chunk):
            truncated[0] = True
        written += len(accepted)
        text = redact(accepted.decode("utf-8", errors="replace"))
        artifact.write(text.encode("utf-8"))
        artifact.flush()
        console.write(text)
        console.flush()


def run_manifest(manifest_path: Path) -> int:
    started = time.monotonic()
    manifest: dict[str, object] | None = None
    completion_path: Path | None = None
    try:
        manifest = _load_manifest(manifest_path)
        completion_path = Path(_require_string(manifest["completionPath"], "completionPath"))
        argv = cast(list[str], manifest["argv"])
        cwd = Path(_require_string(manifest["cwd"], "cwd"))
        stdin_path = Path(_require_string(manifest["stdinPath"], "stdinPath"))
        stdout_path = Path(_require_string(manifest["stdoutPath"], "stdoutPath"))
        stderr_path = Path(_require_string(manifest["stderrPath"], "stderrPath"))
        environment = cast(dict[str, str], manifest["environment"])
        max_output_bytes = cast(int, manifest["maxOutputBytes"])
        keep_open = cast(bool, manifest["keepOpen"])
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            stdout_path.open("wb") as stdout_artifact,
            stderr_path.open("wb") as stderr_artifact,
            subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process,
        ):
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise RuntimeError("terminal host could not create CLI pipes")
            process.stdin.write(stdin_path.read_bytes())
            process.stdin.close()
            stdout_truncated = [False]
            stderr_truncated = [False]
            stdout_thread = threading.Thread(
                target=_pump,
                args=(process.stdout, stdout_artifact, sys.stdout),
                kwargs={"max_output_bytes": max_output_bytes, "truncated": stdout_truncated},
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_pump,
                args=(process.stderr, stderr_artifact, sys.stderr),
                kwargs={"max_output_bytes": max_output_bytes, "truncated": stderr_truncated},
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            returncode = process.wait()
            stdout_thread.join()
            stderr_thread.join()
        _atomic_json(
            completion_path,
            {
                "durationSeconds": time.monotonic() - started,
                "error": None,
                "returncode": returncode,
                "stderrTruncated": stderr_truncated[0],
                "stdoutTruncated": stdout_truncated[0],
            },
        )
        if keep_open:
            print("\nCodex invocation finished. Press Enter to close this window.")
            with suppress(EOFError):
                input()
        return returncode
    except BaseException as exc:
        if completion_path is not None:
            _atomic_json(
                completion_path,
                {
                    "durationSeconds": time.monotonic() - started,
                    "error": redact(str(exc)),
                    "returncode": 1,
                    "stderrTruncated": False,
                    "stdoutTruncated": False,
                },
            )
        print(f"terminal host failed: {redact(str(exc))}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-claude-terminal-host")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_manifest(args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
