# Visible CLI terminals design

## Goal

When a real orchestration starts on Windows, each Codex or Claude invocation
runs in its own visible PowerShell console. Codex consoles remain open after
their command finishes; Claude consoles close when their command finishes. The
controller must retain its existing JSONL contracts, timeout/cancellation,
redaction, retry, context rotation, and crash recovery behavior.

Linux and headless CI remain supported. In `auto` mode they use the existing
pipe-based runner instead of assuming a desktop terminal emulator. A caller can
request `hidden` mode everywhere, while `visible` mode is supported on Windows
and fails clearly when the platform cannot provide a console host.

## Architecture

`SubprocessRunner` gains a terminal mode and an invocation policy. In hidden
mode it keeps the current `asyncio.create_subprocess_exec` path. In visible
Windows mode it writes a private invocation manifest and UTF-8 stdin artifact
under the run's internal artifact directory, then starts a standard-library
terminal host in a new console with `CREATE_NEW_CONSOLE`.

The terminal host:

1. Reads the manifest and starts the requested CLI with `shell=False`.
2. Streams stdout and stderr to bounded, redacted UTF-8 log artifacts and to
   the visible console.
3. Writes a completion record containing the exit code, timeout-independent
   process result, and an unambiguous completion marker.
4. Exits after Claude invocations, or waits for a user keypress after Codex
   invocations so the Codex window remains open.

The controller waits for the completion record rather than parsing a terminal
pipe. It then parses the bounded log artifact with the same JSONL callback and
contract validation used by the hidden path. The host PID is retained so
timeouts, Ctrl+C, and cancellation can terminate the host and its CLI child
tree. Manifest and log paths never contain prompt text or secrets.

Adapters pass the policy explicitly: Codex uses `keep_open`, Claude uses
`close_on_exit`. Each adapter invocation therefore gets its own window,
including Codex plan/review calls and Claude context rotations.

## Configuration and compatibility

`RunConfig` adds `terminal_mode` with values `auto`, `visible`, and `hidden`.
The CLI exposes `--terminal-mode`; the default is `auto`. `auto` selects
visible mode only for real Codex/Claude commands on Windows and hidden mode for
Linux, CI, and custom fake command prefixes. Existing tests remain headless.

Visible mode is an execution presentation feature only; it does not widen the
command policy, environment allowlist, write scopes, or agent permissions.

## Failure handling

- Failure to create a console host is reported as a typed process error before
  the invocation is considered successful.
- Malformed JSONL, missing completion records, non-zero exit, timeout, and
  cancellation map to the same existing typed errors as hidden execution.
- Partial logs and manifests remain under the run artifact directory for
  recovery; sensitive values are redacted before persistence and display.
- A Codex window left open after completion is not treated as a running task;
  the controller records completion and continues. Cleanup of an abandoned
  Codex host is documented for recovery.

## Verification

Add unit tests for mode selection and manifest validation, integration tests for
the hidden path and Windows host path using a fake CLI, and checks that Claude
hosts exit while Codex hosts leave a controlled completion state. Existing
full-system, timeout, cancellation, adapter, and live-smoke tests must remain
green. Documentation will describe Windows behavior, Linux fallback, artifact
locations, and how to select `--terminal-mode hidden` in CI.
