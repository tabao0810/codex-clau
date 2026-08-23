# codex-claude

`codex-claude` is a Python 3.11+ terminal orchestrator. Codex plans and reviews
in a read-only sandbox; Claude Code is the only agent that edits source, and it
does so in isolated Git worktrees. The controller verifies and applies accepted
patches to the target working tree without committing, pushing, publishing, or
deploying them.

The implementation follows the approved
[design](docs/superpowers/specs/2026-08-23-codex-claude-orchestrator-design.md).

> This tool executes repository verification commands locally. Repository code
> can therefore run with your user account's operating-system permissions. Use
> the tool only with repositories and requirements you trust, then review all
> uncommitted changes before committing them yourself.

## Requirements and installation

Install Python 3.11 or newer, Git, the Codex CLI, and the Claude Code CLI. Both
CLIs must already be authenticated. The startup preflight checks the CLI flags
required by the orchestrator and runs `codex login status` and
`claude auth status --json` before creating workers.

Install from this source checkout:

```powershell
python -m pip install .
codex-claude --help
```

For development, install the quality-gate dependencies too:

```powershell
python -m pip install -e ".[dev]"
```

## Running an orchestration

A new run requires a clean Git working tree and no unfinished
`codex-claude` run in the repository. Start from either one direct prompt or one
UTF-8 `.md`/`.txt` specification of at most 1 MiB:

```powershell
codex-claude run "Add request validation and tests" --cwd D:\source\my-project
codex-claude run --spec D:\requirements\feature.md --cwd D:\source\my-project
```

The important operating defaults are two workers, a 55% Claude context limit,
a 600-second Codex timeout, an 1,800-second Claude timeout, and a 900-second
verification timeout. They can be changed on `run`:

```powershell
codex-claude run "Implement the approved change" `
  --cwd D:\source\my-project `
  --max-workers 2 `
  --claude-context-limit 55 `
  --codex-timeout 600 `
  --claude-timeout 1800 `
  --verification-timeout 900
```

Append a requirement without replacing prior requirements:

```powershell
codex-claude add "Also cover malformed UTF-8 input" --cwd D:\source\my-project
```

When a run is active, `add` waits for the next safe boundary before replanning.
For a paused or blocked run, append the requirement first and then resume:

```powershell
codex-claude resume --cwd D:\source\my-project
```

## Monitoring `process.md`

The controller writes `process.md` at the target repository root after every
important state transition. The readable section shows the run phase, active
tasks, worker IDs, attempts, worktree paths, context usage, current activity,
and last integration. A machine-readable JSON block at the end is the durable
checkpoint used by `resume`.

The file is owned by the controller and added to `.git/info/exclude`; Claude
workers do not receive it, and it must not be committed. Raw prompt/SRS copies,
patches, and agent response artifacts live under
`.git/codex-claude/<run-id>/`. Do not put credentials in prompts or SRS files:
these recovery artifacts intentionally preserve the original input.

Claude usage is evaluated at response boundaries. When the latest response uses
55% or more of its model context window, the controller records a handoff and
opens a fresh Claude session in the same worktree. Rotation does not consume a
task retry; a task still has at most three attempts total.

## Permissions and blocked runs

Codex is launched read-only. Claude gets a bounded tool set in a detached
worktree, while verification commands are validated and receive an environment
allowlist. Prompt or SRS text cannot grant broader terminal permissions, bypass
repository rules, or authorize publish/deploy operations. See the complete
[security model](docs/security-model.md).

Expected policy, checkpoint, permission, scope, or integration failures leave
the run `BLOCKED` with a reason in `process.md` and exit code 2. Do not edit the
partially integrated main working tree just to make `resume` proceed; checkpoint
validation will refuse unexpected changes. Resolve the stated external issue,
append a requirement if the plan must change, then run `resume`. Detailed crash,
artifact, and worktree procedures are in the [recovery guide](docs/recovery.md).

The normal exit codes are 0 for `COMPLETED`, 2 for a rejected/blocked run, and
130 when interrupted with Ctrl+C and recorded as `PAUSED`.

## Development and tests

The default quality gate never calls real AI services:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pytest -m "not live" --cov=codex_claude --cov-report=term-missing
python -m build
```

An authenticated live smoke test is available only by explicit opt-in. It asks
both CLIs to inspect a temporary fixture without editing it:

```powershell
$env:CODEX_CLAUDE_RUN_LIVE_TESTS = "1"
python -m pytest -m live tests/smoke/test_live_clis.py
Remove-Item Env:CODEX_CLAUDE_RUN_LIVE_TESTS
```

CI always uses `-m "not live"` on Windows and Linux.
