from __future__ import annotations

import json

from codex_claude.controller import Controller


def test_preflight_checks_required_flags_and_auth(monkeypatch: object) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(argv: tuple[str, ...], required: tuple[str, ...] = ()) -> str:
        calls.append(argv)
        if argv[0] == "claude" and "status" in argv:
            return json.dumps({"loggedIn": True})
        return " ".join(required)

    monkeypatch.setattr(Controller, "_preflight_command", staticmethod(fake))  # type: ignore[attr-defined]
    Controller._preflight_cli(("codex",), ("claude",))
    assert ("codex", "login", "status") in calls
    assert ("claude", "auth", "status", "--json") in calls
