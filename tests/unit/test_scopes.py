import pytest

from codex_claude.errors import ValidationError
from codex_claude.security import normalize_repo_path, scope_owns_path, scopes_overlap


def test_directory_scope_uses_path_segments() -> None:
    assert scope_owns_path("src/auth/", "src/auth/service.py")
    assert not scope_owns_path("src/auth/", "src/authentication.py")
    assert scopes_overlap("src/auth/", "src/auth/service.py")


def test_scope_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        normalize_repo_path("../secret")
