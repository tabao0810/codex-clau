# Security model

This document describes the security boundary implemented by `codex-claude`.
It is an execution-policy layer for a trusted local operator; it is not an
operating-system sandbox for arbitrary repository code.

## Assets and trust boundaries

The protected assets are the target repository and Git history, credentials in
the user's environment, files outside the repository, and external systems such
as package registries and deployment targets.

The local Python controller is trusted to own durable state, worktrees, patch
capture, verification, and integration. Codex and Claude are external agent
processes and must be treated as untrusted decision makers. Requirements,
repository instructions, source files, model output, diffs, and test output are
also untrusted input.

The main boundaries are:

- Codex receives repository context for planning and review but starts in its
  read-only sandbox. The controller, not Codex, performs every state or Git
  mutation.
- Claude runs with `--safe-mode` and `--permission-mode dontAsk` in a detached
  worker worktree. Its tool set is `Read`, `Edit`, `Write`, `Glob`, `Grep`, and
  bounded `Bash` patterns for read-only Git and task verification commands.
- The controller validates actual changed paths against task `writeScopes`,
  verifies the patch, and runs `git apply --check` before touching the main
  working tree.
- Verification executes repository-provided code locally. It is inside the
  orchestration workflow but outside any claimed OS security boundary.

Only the controller manages `process.md`, internal recovery artifacts, Git
worktrees, and patch integration. Neither agent is authorized to commit, push,
rewrite history, publish, or deploy.

## Command and permission policy

Verification commands are structured argv arrays, never free-form shell
strings. Empty arguments, NULs, shell executables, destructive tools,
publish/deploy commands, infrastructure tools, remote shells, and mutating Git
subcommands are rejected by policy. Claude receives only exact verification
command patterns plus these read-only Git operations: `status`, `diff`, `log`,
`show`, and `rev-parse`.

The controller serializes accepted patch application on the main working tree.
It refuses changes outside normalized repository-relative scopes. Tasks with
overlapping scopes, shared resource locks, global manifests, or an explicit
`parallelSafe=false` declaration cannot share a worker wave.

A prompt, SRS, requirement revision, repository instruction, or model response
cannot widen this policy. Conflicting instructions cause rejection or a
`BLOCKED` run; text such as “allow Bash(*)”, “ignore the controller”, or “deploy
this change” has no authorization effect.

## Environment forwarding and authentication

Child processes receive an allowlisted environment, not an unrestricted copy of
the controller environment. The allowlist covers process discovery and terminal
operation (`PATH`, `PATHEXT`, system/temp/locale variables), user/config
locations needed by authenticated CLIs (`HOME`, `USERPROFILE`, `APPDATA`,
`CODEX_HOME`, and `CLAUDE_CONFIG_DIR`), and basic CI/terminal metadata.

Names matching token, password, secret, API key, authorization, or cookie are
removed even if otherwise requested. Authentication therefore relies on the
CLIs' existing login/config stores rather than forwarding raw API tokens.

## Visible terminal mode

On Windows, `terminal_mode=auto` runs real Codex and Claude invocations through
a dedicated console host. The host starts the CLI with argv and `shell=False`,
passes the prompt through an internal artifact file, and redacts stdout/stderr
before persisting or displaying it. Codex hosts remain open after completion;
Claude hosts exit automatically. The controller reads the host completion record
and bounded logs, and can terminate the host process tree on timeout or Ctrl+C.

Visible windows are not an interactive permission channel and do not expand the
command policy, environment allowlist, or agent tool permissions. Fake commands,
Linux, and CI retain the hidden runner unless visible mode is explicitly requested
on a supported platform.

## Redaction and retained data

Captured subprocess stdout and stderr are size-limited. Inline values in common
forms such as `token=value`, `password: value`, `api_key=value`, authorization,
and cookie fields are replaced with `<redacted>` before the output is stored or
sent as review evidence.

This is pattern-based redaction, not a secret-scanning or data-loss-prevention
system. It does not recognize every credential format, values printed without a
label, source-code secrets, or sensitive business data. Do not put secrets in
requirements, repository content, command arguments, diffs, or test output.

For crash recovery, normalized prompt/SRS content is deliberately retained
verbatim under `.git/codex-claude/<run-id>/inputs/`. `process.md` also shows the
direct goal or a shortened SRS summary. Patch and agent artifacts can contain
repository content. Protect the repository's `.git` directory accordingly and
remove archived artifacts according to your local retention policy only after
the run no longer needs recovery.

## Non-goals and residual risks

`codex-claude` does not provide:

- a container, virtual machine, seccomp profile, filesystem jail, or network
  sandbox for repository tests and build scripts;
- protection from malicious dependencies, compilers, test fixtures, Git hooks,
  or executables already present on `PATH`;
- perfect prevention of agent misuse solely through CLI permission flags;
- secret discovery, malware analysis, dependency auditing, or supply-chain
  verification;
- automatic commit review, signing, push protection, deployment approval, or
  cleanup of credentials already present in source history.

Run the tool under a least-privileged user, use an isolated machine or container
when repository code is not trusted, keep credentials out of the environment
where practical, inspect `process.md` and the final diff, and commit or discard
the resulting source changes yourself.
