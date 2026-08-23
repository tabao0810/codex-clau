from __future__ import annotations

from pathlib import Path

import pytest

from codex_claude.errors import ValidationError
from codex_claude.input_loader import MAX_SPEC_BYTES, load_input, load_spec


def test_load_spec_normalizes_newlines(tmp_path: Path) -> None:
    spec = tmp_path / "srs.md"
    spec.write_bytes(b"# SRS\r\nRequirement\r\n")
    loaded = load_spec(spec)
    assert loaded.text == "# SRS\nRequirement\n"
    assert loaded.type == "srs"


def test_input_requires_exactly_one_source(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_input("prompt", tmp_path / "srs.md")


def test_spec_limit(tmp_path: Path) -> None:
    spec = tmp_path / "large.txt"
    spec.write_bytes(b"x" * (MAX_SPEC_BYTES + 1))
    with pytest.raises(ValidationError):
        load_spec(spec)
