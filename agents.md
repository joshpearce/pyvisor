# Agent Instructions

This project is Pyvisor: a containerized service that runs AI-agent-authored
Python/bash commands inside durable per-session gVisor sandboxes.

Read `docs/requirements.md` before making architectural changes. The
requirements in that file are the source of truth unless the user explicitly
updates them.

## Project Priorities

- Security first: sandboxed code is untrusted.
- Correctness before convenience: do not make lifecycle shortcuts that weaken
  isolation, durability, or cancellation semantics.
- Reproducibility: images, dependencies, traces, and tests must be pinned and
  repeatable.
- Operability: the service should be diagnosable from logs, metrics, health
  checks, and durable on-disk state.
- Small, explicit APIs: keep the initial surface focused on `run`, `poll`, and
  `cancel`.

## Architecture Requirements

- Keep session, session directory, and container lifetimes independent.
- Treat the session directory as durable state. Never auto-delete it from normal
  lifecycle code.
- Treat containers as disposable runtime state.
- Prefer direct `runsc run` against generated OCI bundles for sandbox launch.
- Do not depend on Docker or Podman `--runtime=runsc` for the core execution
  path.
- Keep the app's internal listener plain HTTP unless an explicit in-process TLS
  requirement is added. HTTPS exposure is expected through the Tailscale sidecar.
- Do not add network access inside sandboxes.
- Keep one live job per session until there is a clear requirement and design
  for concurrent jobs.
- Make cancellation kill the process group, not just mark service-side state.
- Make restart reconciliation explicit. Either adopt labeled running sandboxes
  or kill orphans; do not trust only in-memory maps.

## API Requirements

- Responses must be self-describing and stable enough for an agent harness.
- Keep stdout and stderr separate.
- Include exit code when known.
- Include terminal status: `completed`, `running`, `timed_out`, `cancelled`, or
  an explicit error state.
- Bound returned output with clear truncation metadata.
- Preserve full output on disk.
- Decode output robustly with replacement for invalid UTF-8.
- Use empty/null stdin for every command.

## Persistence And Trace Requirements

- Store all per-session durable state under a configured sessions root.
- Write job stdout/stderr to durable files as the process runs.
- Append a durable per-session trace entry for every command.
- Trace entries should include command, job id, timings, terminal status, exit
  code, output paths, truncation metadata, and sandbox image identity.
- Writes to trace/state files should be crash-tolerant enough that service
  restart does not corrupt the whole session record.

## Sandbox And Resource Requirements

- Sandboxes must run without network access.
- Base filesystem should be read-only.
- Writable paths should be limited to the mounted session directory and bounded
  scratch/temp storage.
- Enforce memory, CPU, job wall-clock timeout, and session disk limits.
- Drop capabilities and avoid privileged mode.
- Do not use gVisor `ptrace` platform.
- KVM support may be optional and feature-gated when `/dev/kvm` is available.

## Implementation Standards

- Prefer simple, explicit modules over clever abstractions.
- Separate HTTP/API handling, session state, job execution, runtime integration,
  output capture, and reaping logic.
- Use structured configuration with documented defaults.
- Validate all caller-supplied identifiers before mapping them to filesystem
  paths. Session ids and job ids must not allow path traversal.
- Use atomic file operations for durable state where practical.
- Make background tasks stoppable and observable.
- Log lifecycle transitions: boot, run, poll, cancel, timeout, reap, runtime
  failure, restart reconciliation.
- Avoid global mutable state unless it is protected by clear locking.
- Prefer typed data structures for request/response/state models.

## Testing Expectations

This repo may start on macOS, but it is expected to move to a NixOS host where
real runtime tests can run. Build toward that.

Required test layers:

- Unit tests for id validation, config parsing, output truncation, trace writing,
  state reconciliation, and lifecycle decisions.
- API tests for `run`, `poll`, `cancel`, busy behavior, timeout behavior, and
  error responses.
- Runtime integration tests on NixOS for direct `runsc run` OCI bundle launch.
- Sandbox tests proving no network, read-only base filesystem, writable session
  mount, env/workdir setup, CPU/memory timeout behavior, and process-group
  cancellation.
- Persistence tests proving session files and traces survive container reap and
  service restart.
- Reaper tests proving idle containers are reaped and live jobs are not.

Until the runtime host is available, keep runtime-dependent tests clearly marked
or skipped with an explicit reason.

## Build And Deployment Expectations

- Keep the service container build reproducible.
- Pin Python and significant Python package versions in the sandbox image.
- Do not rely on runtime package installation from inside the sandbox.
- Include a health endpoint suitable for container/service checks.
- Include Nix/NixOS or Quadlet deployment artifacts when the deployment shape is
  finalized.
- Keep the Tailscale sidecar deployment separate from the app container.

## Documentation Expectations

- Update `docs/requirements.md` when requirements change.
- Document non-obvious runtime assumptions, especially gVisor limitations and
  NixOS host requirements.
- Keep API examples current with implementation.
- Document local development and NixOS integration test workflows once they
  exist.

## Change Discipline

- Keep changes scoped to the user's request.
- Do not introduce unrelated refactors while implementing behavior.
- Do not weaken sandboxing or durability to make tests easier.
- If a requirement cannot be met yet because the repo is still on macOS, leave a
  precise TODO or skipped test tied to the missing host capability.
- Before finalizing a code change, run the relevant checks that are available in
  the current environment and report anything that could not be run.

