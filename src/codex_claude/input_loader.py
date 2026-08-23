from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError

MAX_SPEC_BYTES = 1024 * 1024
SUPPORTED_SPEC_SUFFIXES = {".md", ".txt"}


@dataclass(frozen=True, slots=True)
class LoadedInput:
    type: str
    text: str
    sha256: str
    size_bytes: int
    source_path: str | None
    extension: str

    @property
    def summary(self) -> str:
        compact = " ".join(self.text.split())
        return compact[:240] + ("…" if len(compact) > 240 else "")


def _canonical_text(data: bytes, source: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{source} must be UTF-8") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _loaded(kind: str, text: str, source_path: str | None, extension: str) -> LoadedInput:
    encoded = text.encode("utf-8")
    return LoadedInput(
        type=kind,
        text=text,
        sha256=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
        source_path=source_path,
        extension=extension,
    )


def load_prompt(prompt: str) -> LoadedInput:
    if not prompt.strip():
        raise ValidationError("prompt must not be empty")
    return _loaded("prompt", prompt.replace("\r\n", "\n").replace("\r", "\n"), None, ".txt")


def load_spec(path: Path) -> LoadedInput:
    if path.is_symlink():
        raise ValidationError(f"SRS symlinks are not accepted: {path}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValidationError(f"SRS must be a regular file: {path}")
    suffix = resolved.suffix.lower()
    if suffix not in SUPPORTED_SPEC_SUFFIXES:
        raise ValidationError("SRS extension must be .md or .txt")
    size = resolved.stat().st_size
    if size > MAX_SPEC_BYTES:
        raise ValidationError("SRS exceeds the 1 MiB limit")
    text = _canonical_text(resolved.read_bytes(), str(resolved))
    return _loaded("srs", text, str(resolved), suffix)


def load_input(prompt: str | None, spec: Path | None) -> LoadedInput:
    if (prompt is None) == (spec is None):
        raise ValidationError("provide exactly one input source: prompt or --spec")
    if prompt is not None:
        return load_prompt(prompt)
    if spec is None:
        raise AssertionError("validated input source unexpectedly missing")
    return load_spec(spec)


def verify_source_digest(loaded: LoadedInput) -> None:
    if loaded.type != "srs" or loaded.source_path is None:
        return
    current = load_spec(Path(loaded.source_path))
    if current.sha256 != loaded.sha256:
        raise ValidationError("SRS content changed since the run was created")
