from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .input_loader import LoadedInput
from .state import Revision, RevisionStatus, utc_now


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_revision(
    loaded: LoadedInput,
    *,
    git_common_dir: Path,
    run_id: str,
    revision_number: int,
) -> Revision:
    revision_id = f"R{revision_number:02d}"
    relative = Path("codex-claude") / run_id / "inputs" / f"{revision_id}{loaded.extension}"
    artifact = git_common_dir / relative
    if artifact.exists():
        raise FileExistsError(f"revision artifact already exists: {artifact}")
    _atomic_write(artifact, loaded.text.encode("utf-8"))
    return Revision(
        id=revision_id,
        type=loaded.type,
        created_at=utc_now(),
        sha256=loaded.sha256,
        artifact_path=relative.as_posix(),
        summary=loaded.summary,
        status=RevisionStatus.QUEUED,
        source_path=loaded.source_path,
        size_bytes=loaded.size_bytes,
    )


def read_revision_content(revision: Revision, git_common_dir: Path) -> str:
    path = git_common_dir / Path(revision.artifact_path)
    data = path.read_bytes()
    import hashlib

    if hashlib.sha256(data).hexdigest() != revision.sha256:
        raise ValueError(f"revision artifact digest mismatch: {revision.id}")
    return data.decode("utf-8")
