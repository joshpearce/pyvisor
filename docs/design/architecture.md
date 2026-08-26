# Pyvisor Architecture

## Scope and MVP decisions

The first deliverable is a single-process Python 3.13 service using FastAPI and
Pydantic for a JSON-over-HTTP API.  It owns durable session state and delegates execution to an
interchangeable backend.  The first backend is a local-process implementation
used only to validate lifecycle semantics; it is **not** a security boundary.
The production backend launches gVisor directly with `runsc run` and is the
only supported backend for untrusted commands.

MVP deployment is behind the Tailscale `tsnsrv` sidecar described in the
requirements.  Pyvisor listens only on its private container network and does
not implement application-level authentication in MVP.  Direct/public exposure
of the listener is unsupported.  A later authentication design must bind a
caller identity to the session namespace before a listener is exposed beyond
that trusted deployment boundary.

These are the initial, configurable defaults.  They are deliberately modest
and must be validated by the NixOS runtime test suite before being described as
enforced sandbox limits.

| Setting | Default |
| --- | --- |
| sessions root | `/var/lib/pyvisor/sessions` |
| runtime root (disposable) | `/var/lib/pyvisor/runtime` |
| foreground soft cap | 5 seconds |
| job timeout | 300 seconds (maximum 3,600 seconds) |
| returned output per stream | 65,536 bytes |
| requested command size | 65,536 UTF-8 bytes |
| idle container period | 30 minutes |
| target CPU limit | 1 CPU |
| target memory limit | 1 GiB |
| target session disk limit | 1 GiB |
| target scratch disk limit | 512 MiB |
| execution backend | `runsc` |
| `runsc` binary | `/usr/local/bin/runsc` |
| sandbox rootfs | `/var/lib/pyvisor/rootfs` |
| sandbox image identity | `pyvisor-sandbox:unconfigured` |

The CPU, memory, and disk settings above are configuration intent until the
runtime backend proves the selected host mechanism applies them.  The job
wall-clock timeout and API-output bound are enforced by the service from the
local-process stage onward.

The direct gVisor backend is the default and requires `PYVISOR_RUNSC_BINARY`,
`PYVISOR_SANDBOX_ROOTFS`, and a non-placeholder
`PYVISOR_SANDBOX_IMAGE_IDENTITY` appropriate to the deployment. Local host
execution is available only for lifecycle development with both
`PYVISOR_EXECUTION_BACKEND=local` and `PYVISOR_UNSAFE_LOCAL_EXECUTION=1`; it
refuses non-loopback listeners and is unhealthy by design. The service runs as
a dedicated non-root account. Its rootless OCI bundle maps only in-sandbox
UID/GID 0 to that account so the private session workspace remains writable
without being made world-writable.

## Components and ownership

`app` wires configuration, logging, lifecycle tasks, and the HTTP server.  It
does not hold job state itself.

`api` validates HTTP requests, translates domain failures to the stable error
format, and invokes the job service.  It contains no filesystem or subprocess
logic.

`registry` owns in-memory coordination only: one async lock and at most one
live-job record per session.  Its contents are rebuilt from durable state at
startup and must never be treated as the source of truth.

`storage` maps validated identifiers to paths, creates session/job directories,
performs atomic state writes, appends trace records, and reads bounded output.
It is the sole owner of the on-disk format.

`jobs` implements launch, foreground waiting, polling, timeout, cancellation,
and terminalization.  It writes output from process start, updates durable
state, appends a trace entry exactly once at terminalization, and releases the
registry slot only after durable terminal state is written.

`executor` is a small backend interface: create or obtain a session runtime,
start a job, terminate a job process group, inspect a live job, destroy a
session runtime, and enumerate runtime instances.  It returns runtime facts,
not HTTP models.  `local_executor` launches an ordinary process group for
Stage 4.  `runsc_executor` generates an OCI bundle containing a signal-aware
idle init, then invokes `runsc exec` for each job without Docker or Podman.

`reconciler` runs before accepting traffic.  It kills any discovered managed
runtime instance, then marks any durable non-terminal job state as
`runtime_error` with reason `service_restart`.  This intentionally chooses the
safe kill-orphans policy over adopting a possibly unknown running sandbox.

`reaper` periodically destroys idle *runtime instances* only.  It never
deletes a session directory.  It takes the same per-session coordination lock
as launch and cancellation, and refuses to reap a session with a live job.

## Lifecycle

1. `run` validates the session id and command, obtains the session lock, and
   rejects the request as `busy` if a live job exists.
2. Storage creates the durable session and job directories and atomically
   records a `running` job state before launch.
3. The executor obtains or starts the disposable runtime.  It receives empty
   stdin and opens the durable stdout/stderr paths before the command begins.
4. `run` waits only the foreground soft cap.  It returns `completed` if the
   job terminalizes during that interval; otherwise it returns `running`.
5. A job watcher handles natural exit, deadline expiry, cancellation, and
   executor failure.  Timeout and cancellation terminate the executor's whole
   job process group, wait for it to exit, preserve partial output, then write
   terminal state and trace.
6. `poll` reads state and bounded output from disk; it does not require the
   original process or an in-memory output buffer.
7. After a terminal transition, the reaper may later destroy the warm runtime.
   The next command recreates it against the same session directory.

## Runtime backend boundary

Runtime paths are disposable and live under `runtime_root`, never under a
session directory.  A gVisor runtime instance receives an internally generated
container id and OCI annotations containing `io.pyvisor.managed=true` and the
validated session id.  Its OCI bundle is generated under
`runtime_root/bundles/<container-id>`.  The durable session directory is mounted
at `/workspace`; no caller-supplied path is used as a mount target.

The `runsc` backend must run with networking disabled, a read-only rootfs,
only `/workspace` plus a bounded scratch mount writable, no privileged mode,
and a non-`ptrace` platform.  It starts one `runsc run --detach` idle init per
session and uses `runsc exec --pid-file` for each command.  Cancellation first
targets that exec PID; failure to identify or signal it destroys the whole
session runtime rather than risk retaining an unknown process.  Resource-cgroup
mapping and rootfs preparation remain Stage 5 design-and-probe work; they are
not assumptions the local executor may emulate.

## Concurrency and durability rules

- A session lock covers the check-and-create-live-job operation.  `poll` may
  read without it; `cancel`, reaping, and terminal transitions synchronize with
  it as needed.
- Every job gets a unique service-generated UUID.  Caller input never selects a
  job directory.
- State files are written to a temporary file in the job directory, fsynced,
  and atomically renamed.  Directory fsync follows where the platform permits.
- Trace records are newline-delimited JSON appended with one write, then fsynced.
  A reader ignores an incomplete final line after a crash; subsequent appends
  repair by terminating that partial line before writing a new record.
- stdout and stderr are byte streams kept in separate files.  Files are never
  rewritten or deleted by normal job, container, or reaper lifecycle code.

## Error taxonomy and observability

The API error codes are `invalid_request`, `invalid_session_id`, `not_found`,
`busy`, `runtime_unavailable`, `runtime_error`, and `internal_error`.  Errors
include a stable machine-readable `code` and a human-readable `message`.
Terminal jobs use the status vocabulary in the API design; `runtime_error` is
the explicit terminal error state.

Structured logs include event name, session id, job id where applicable,
runtime container id, terminal status, elapsed time, and failure reason.  The
initial metrics are job counts by terminal status, running-job count, runtime
boot/reap/reconciliation counts, timeout/cancel counts, and API error count.
