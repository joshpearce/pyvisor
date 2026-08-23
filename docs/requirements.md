# Pyvisor Requirements

Pyvisor is a network service for AI agents that need a persistent, sandboxed
Python workspace during a chat session. Agents submit bash commands over HTTP.
The service executes those commands inside a strongly isolated Python sandbox,
captures output, and preserves session files across commands, container reaps,
and service restarts.

## Core Lifetimes

The design separates three lifetimes:

- Session: caller-supplied logical id.
- Session directory: durable per-session filesystem state, created on first use
  and never automatically deleted.
- Container: disposable sandbox process, lazily created, kept warm, idle-reaped,
  and recreated against the same session directory.

Only the container is disposable. The session directory is the source of truth
for user work and RL capture.

## Runtime Assumptions

- The service runs as a container on the deployment host.
- HTTPS ingress is provided by a Tailscale `tsnsrv` sidecar.
- The app can listen on plain HTTP inside the shared container network.
- The sandbox runtime path should use direct `runsc run` over generated OCI
  bundles, not Docker/Podman `--runtime=runsc`.
- Use the official static `runsc` release binary.
- Prefer the default modern gVisor platform. Do not use `ptrace`.
- KVM may be supported when `/dev/kvm` is mounted, but it is optional.

## Sandbox Image

The sandbox has no runtime network access, so the image must contain all expected
tools and libraries at build time.

Required:

- Pinned Python 3.14.x if feasible; Python 3.13.x is the fallback.
- Pinned versions of significant Python libraries.
- At minimum: `numpy`, `pandas`.
- Likely scientific/data stack: `scipy`, `scikit-learn`, `matplotlib`, `sympy`,
  `networkx`, `statsmodels`, plus deliberate domain libraries.
- Common bash/coreutils environment.

The image is the reproducibility boundary. The service must record the image
identity/version in each session trace.

## Command Execution

Every submitted command is a job.

- `run` starts a job and waits up to a configurable foreground soft cap.
- If the job finishes within the cap, return `completed` with exit code,
  stdout, and stderr.
- If it is still running, return `running` with a `job_id` and partial output.
- Output is streamed to files in the session directory from process start.
- Full output remains on disk; API output is bounded with truncation metadata.
- Stdin is empty/null.
- Non-UTF-8 output is decoded with replacement.
- Each job has a wall-clock timeout. On timeout, kill the process group and keep
  partial output.
- `poll` reads current output and completion state.
- `cancel` kills the job process group and returns a cancellation status.

## API

Initial API surface:

- `run(session_id, command, timeout?)`
- `poll(session_id, job_id)`
- `cancel(session_id, job_id)`

Responses must distinguish:

- `completed`
- `running`
- `timed_out`
- `cancelled`
- `busy`
- relevant error states

Responses must keep stdout and stderr separate and include exit code when known.

## Concurrency

Initial rule: one live job per session.

- A per-session foreground lock guards launch and short synchronous execution.
- While a job is live, `run` returns `busy`.
- `poll` and `cancel` remain available.
- The idle reaper must treat live jobs as activity.
- Idle time starts after the last job completes, not when it starts.

## Container Lifecycle

- Lazy boot a container on first command for a session.
- Mount the durable session directory at the fixed in-container work path.
- Keep a warm idle process alive between jobs.
- Reap containers after 30 minutes of no live jobs and no completed activity.
- Recreating a container for an old session is the normal cold path.
- On service restart, either adopt labeled surviving containers or kill orphans.
  The simpler correct default is kill-orphans because session directories are
  durable.

## Isolation And Limits

Required hardening:

- No network inside the sandbox.
- Read-only base filesystem.
- Writable session directory.
- Writable bounded scratch/temp mount.
- CPU limit.
- Memory limit.
- Disk quota for session directories.
- Dropped capabilities.
- No privileged mode.

## RL Capture

Each session directory must contain a durable trace log. For every command,
append:

- command input
- job id
- stdout path and/or captured stdout
- stderr path and/or captured stderr
- exit code or terminal status
- timing
- timeout/cancel metadata
- image identity/version

The final session filesystem plus the trace is the training record. Local
session directories are the working set; long-term retention should be handled
by archival to object storage.

## Harness Contract

The caller must:

- Use an HTTP client timeout longer than the service foreground soft cap.
- Choose a polling policy deliberately.
- Wire user stop/abandonment to `cancel`.

