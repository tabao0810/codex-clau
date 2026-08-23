# Recovery guide

`process.md` is the source of truth for an unfinished run. The readable section
supports operators; the versioned JSON block at the end is the checkpoint that
the controller parses. Updates use a temporary file and atomic replacement so a
process or machine failure does not normally leave a partially written state.

## First response to interruption

1. Stop launching new `run` commands for the repository.
2. Open `process.md` and record the run ID, status, active tasks, worktree paths,
   current activity, blocker, and last integration.
3. Inspect the main repository with `git status --short` and
   `git worktree list --porcelain`. Do not commit, reset, clean, or delete a
   worker directory while deciding whether to resume.
4. If Ctrl+C was handled, expect status `PAUSED`. If the process or machine died,
   the last status may instead describe the side effect that was in progress.
5. Run `codex-claude resume --cwd <repository>` only after the checkpoint and
   filesystem still represent the same run.

The repository ownership lock prevents two controllers from advancing the same
run. The separate short-lived state lock allows `add` to append a revision while
the owner reaches a safe boundary.

## Checkpoint validation

`resume` refuses to continue when it cannot prove that replay is safe. It checks:

- `process.md` exists, has exactly one recognized state block, uses the supported
  schema version, and names the same canonical repository;
- the repository `HEAD` still equals the run's initial commit;
- each source SRS still exists and has the recorded SHA-256 digest;
- every recorded patch artifact exists and matches its digest;
- the main working-tree digest matches the last controller checkpoint, except
  while recovering a recorded integration boundary; and
- no other controller holds the repository ownership lock.

Do not bypass a refusal by editing the JSON block. Restore the exact missing or
changed input/artifact when appropriate, or preserve the evidence and abandon
the run deliberately. If the working tree differs, first identify whether the
change is a controller-integrated patch or an unrelated manual edit. Blindly
overwriting either can lose work.

## Recovery artifacts

All internal paths are below the repository's Git common directory:

```text
.git/codex-claude/<run-id>/
├── inputs/             normalized prompt/SRS revisions
├── patches/            accepted binary patches and their recorded digests
├── agent/              structured Codex responses
└── process.final.md    archived final state when a later run starts
```

Detached worker worktrees are created below an OS temporary directory whose name
starts with `codex-claude-<run-id>-`. Their exact paths are recorded per task in
`process.md`; never guess a temporary path from a broad glob.

When visible terminal mode is active on Windows, each invocation also has a
manifest, stdin artifact, redacted stdout/stderr logs and completion record under
`.git/codex-claude/<run-id>/terminal/`. A completed Codex console may remain
open for inspection even after its completion record lets the controller proceed.
Do not delete these terminal artifacts while resuming; the controller uses them
as diagnostic evidence when an invocation ends unexpectedly.

The root `process.md` is excluded through `.git/info/exclude`, not the project's
`.gitignore`. It and the `.git/codex-claude` tree are local operational data and
do not appear in the source patch.

## What `resume` does at common crash points

- Before planning: it re-enters planning from the durable input artifact.
- During Claude execution, verification, or review: it keeps the worker
  worktree and schedules a safe retry if the task attempt budget remains.
- After patch capture but before integration: it validates the artifact and
  applies it in deterministic task order.
- After patch application but before the state write: it detects that the patch
  is already present by a reverse apply check, records the task `COMPLETED`, and
  avoids applying it twice.
- After all tasks: it reruns final verification on the main working tree and
  asks Codex for the final read-only review before recording `COMPLETED`.

An SRS digest mismatch, missing/tampered patch, exhausted retry or context budget,
out-of-scope change, patch conflict, or ambiguous final review remains `BLOCKED`
and requires operator action rather than speculative replay.

## Requirement changes during recovery

Requirements are append-only. For a `PAUSED` or `BLOCKED` run, queue a correction
without replacing history, then resume:

```powershell
codex-claude add "Clarify the recovery requirement" --cwd D:\source\project
codex-claude resume --cwd D:\source\project
```

`add` itself checks the initial `HEAD` and working-tree checkpoint. It will not
use a new requirement to legitimize unrecorded source changes.

## Safe worktree cleanup

Normal completion and successful recovery remove detached worktrees
automatically. For a blocked run, retain them until you have inspected and, if
needed, copied any unintegrated source changes. A blocked worker may not yet have
a patch artifact.

For each exact worktree path shown in `process.md`:

```powershell
git -C "<worker-path>" status --short
git -C "<worker-path>" diff --binary
```

Only after preserving needed changes and deciding not to resume, remove that
specific registered worktree through Git, then prune stale registration:

```powershell
git -C "<repository>" worktree remove --force "<worker-path>"
git -C "<repository>" worktree prune
```

Verify that `<worker-path>` is the recorded path for the same run ID and that it
is outside the main repository. Never recursively delete a guessed temp root,
the repository root, or `.git`. Do not delete
`.git/codex-claude/<run-id>/` while `process.md` may still be resumed; doing so
invalidates its input and patch checkpoints.

After a `COMPLETED` run, source changes intentionally remain uncommitted in the
main working tree. Review them with `git status` and `git diff`, then decide
whether to commit or discard them using your normal repository workflow.
