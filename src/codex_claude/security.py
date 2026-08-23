from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from .errors import PolicyError, ValidationError

_GLOB_CHARS = frozenset("*?[]{}")
_SECRET_NAME = re.compile(
    r"(?:token|password|passwd|secret|api[_-]?key|authorization|cookie)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key|authorization|cookie)"
    r"\b\s*[:=]\s*([^\s,;]+)"
)
_FORBIDDEN_EXECUTABLES = {
    "deploy",
    "publish",
    "kubectl",
    "helm",
    "terraform",
    "pulumi",
    "ansible",
    "ssh",
    "scp",
    "ftp",
    "bash",
    "cmd",
    "fish",
    "powershell",
    "pwsh",
    "sh",
    "wsl",
    "zsh",
}
_DESTRUCTIVE = {
    "rm",
    "rmdir",
    "del",
    "erase",
    "format",
    "mkfs",
    "diskpart",
    "shutdown",
    "reboot",
    "dropdb",
}
_GIT_MUTATIONS = {
    "add",
    "am",
    "apply",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "fetch",
    "merge",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "stash",
    "switch",
    "tag",
    "worktree",
}
_ENV_ALLOW = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TERM",
    "COLORTERM",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "CI",
}


def normalize_repo_path(value: str, *, directory_hint: bool | None = None) -> str:
    raw = value.replace("\\", "/").strip()
    if not raw or "\x00" in raw:
        raise ValidationError("repository path must be non-empty and contain no NUL")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValidationError(f"absolute paths are forbidden: {value}")
    if any(char in raw for char in _GLOB_CHARS):
        raise ValidationError(f"glob paths are forbidden: {value}")
    is_directory = raw.endswith("/") if directory_hint is None else directory_hint
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"path traversal is forbidden: {value}")
    normalized = "/".join(parts)
    return f"{normalized}/" if is_directory else normalized


def scope_owns_path(scope: str, path: str) -> bool:
    normalized_scope = normalize_repo_path(scope)
    normalized_path = normalize_repo_path(path, directory_hint=False)
    if normalized_scope.endswith("/"):
        return normalized_path.startswith(normalized_scope)
    return normalized_scope == normalized_path


def scopes_overlap(left: str, right: str) -> bool:
    lhs = normalize_repo_path(left)
    rhs = normalize_repo_path(right)
    if lhs == rhs:
        return True
    if lhs.endswith("/") and rhs.startswith(lhs):
        return True
    return rhs.endswith("/") and lhs.startswith(rhs)


def changed_paths_within_scopes(paths: Iterable[str], scopes: Iterable[str]) -> bool:
    normalized_scopes = tuple(normalize_repo_path(scope) for scope in scopes)
    return all(any(scope_owns_path(scope, path) for scope in normalized_scopes) for path in paths)


def sanitize_environment(
    source: Mapping[str, str] | None = None,
    *,
    extra_allow: Iterable[str] = (),
) -> dict[str, str]:
    environment = os.environ if source is None else source
    allowed = _ENV_ALLOW | {name.upper() for name in extra_allow}
    return {
        name: value
        for name, value in environment.items()
        if name.upper() in allowed and not _SECRET_NAME.search(name)
    }


def redact(text: str) -> str:
    return _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def validate_command(argv: Iterable[str]) -> tuple[str, ...]:
    command = tuple(argv)
    if not command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command):
        raise ValidationError("command must be a non-empty argv without empty or NUL arguments")
    executable = PurePosixPath(command[0].replace("\\", "/")).name.lower()
    executable = executable.removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
    if executable in _FORBIDDEN_EXECUTABLES or executable in _DESTRUCTIVE:
        raise PolicyError(f"command is forbidden by policy: {executable}")
    if executable == "git" and len(command) > 1 and command[1].lower() in _GIT_MUTATIONS:
        raise PolicyError(f"Git mutation is forbidden for agent/verification: {command[1]}")
    lowered = {arg.lower() for arg in command[1:]}
    if {"deploy", "publish"} & lowered:
        raise PolicyError("publish/deploy commands are forbidden")
    return command
