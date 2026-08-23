from __future__ import annotations

import os
import time
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .errors import LockError


class FileLock:
    """Small cross-platform advisory lock over the first byte of a file."""

    def __init__(self, path: Path, *, timeout: float = 0.0) -> None:
        self.path = path
        self.timeout = timeout
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock(stream)
                self._stream = stream
                return
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise LockError(f"lock is already held: {self.path}") from exc
                time.sleep(0.05)

    @staticmethod
    def _lock(stream: BinaryIO) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = import_module("fcntl")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(stream: BinaryIO) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = import_module("fcntl")
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            self._unlock(self._stream)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
