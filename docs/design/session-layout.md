# Durable Session Layout

The configured sessions root defaults to `/var/lib/pyvisor/sessions`.  Every
path below is owned by Pyvisor.  A session directory is durable user state and
is never automatically deleted by job completion, container reaping, or service
restart.

```text
<sessions-root>/
  <session-id>/
    session.json
    trace.jsonl
    workspace/                 # mounted in the sandbox as /workspace
    jobs/
      <job-id>/
        state.json
        stdout.bin
        stderr.bin
```

`workspace/` is created when the session is created.  It is the only durable
caller work area exposed to a sandbox.  `session.json`, `trace.jsonl`, and
`jobs/` are service-managed and are not mounted writable into the sandbox.
The runtime may mount only `workspace/` at `/workspace` and a separate bounded,
disposable scratch directory at `/tmp`.

## `session.json`

Created atomically on first use and immutable apart from explicitly versioned
metadata additions:

```json
{
  "format_version": 1,
  "session_id": "chat_01",
  "created_at": "2026-08-23T12:00:00Z"
}
```

The file makes session existence explicit without relying on a directory's
mtime.  Future caller-identity binding, if added, belongs here only after an
authentication design is approved.

## Job state

`state.json` is the current durable record for one job.  It is atomically
replaced, never updated in place.  Required fields are `format_version`,
`session_id`, `job_id`, `command`, `status`, `started_at`, `timeout_seconds`,
`stdout_path`, `stderr_path`, and `sandbox_image`.  Terminal records also have
`finished_at`, `exit_code` when known, and an optional `reason` such as
`timeout`, `cancel_requested`, `service_restart`, or executor failure detail.

`stdout_path` and `stderr_path` are relative, fixed-format paths within the
job directory (`stdout.bin` and `stderr.bin`), not arbitrary paths.  State must
not contain credentials, environment values, or unredacted implementation
exceptions.

## Trace log

`trace.jsonl` contains one terminal record per submitted command.  It is
append-only in normal operation.  Each JSON object includes all durable job
facts needed for RL capture: command, ids, start and finish timing, terminal
status, exit code, cancellation/timeout reason, relative output paths, observed
output sizes and API truncation policy, and sandbox image identity.

A trace entry is appended only once terminal state is durable.  Startup
reconciliation writes a terminal `runtime_error` state and then its matching
trace entry for an interrupted job.  This ensures every accepted command has
exactly one terminal trace record even when the service restarts.

## Path and ownership rules

- Storage accepts only validated `session_id` and service-generated `job_id`.
- It builds paths from fixed components and verifies their resolved parent is
  within the configured sessions root before use.
- The service creates directories with restrictive permissions (owner-only by
  default) and never follows a caller-created symlink from session data into
  service-managed paths.
- Runtime bundles, runsc root/state, cgroup handles, and scratch directories
  reside beneath the separate disposable runtime root.  They are intentionally
  absent from this layout and may be destroyed during failed launch cleanup,
  reconciliation, or reaping. Failed `runsc` launch stderr is emitted through
  the bounded structured exception log, not retained in a disposable bundle.

## Crash behaviour

Output may end at any byte boundary after a crash and is still retained.  State
is either the previous valid JSON object or a fully written replacement.  A
truncated final JSONL trace line is ignored during recovery and repaired before
future append; valid preceding trace entries remain usable.  On recovery,
non-terminal state has no trusted live process and is terminalized as
`runtime_error` with `reason: "service_restart"`.
