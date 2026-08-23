# Pyvisor Roadmap

This roadmap orders the work so the project can move from design notes to a
tested NixOS deployment without hiding security or runtime uncertainty.

## Stage 0: Requirements Baseline

Status: in progress (provisional MVP decisions recorded in Stage 1 design;
security and deployment decisions need review).

Goal: keep the current spec coherent before implementation starts.

Tasks:

- Maintain `docs/requirements.md` as the source of truth.
- Keep `agents.md` aligned with the active requirements.
- Decide which requirements are MVP versus later hardening.

More design needed: yes.

Open questions:

- Whether the first deployment will use systrap only or include optional KVM.

Exit criteria:

- MVP scope is explicit.
- Security invariants are agreed.
- Unknowns are tracked as follow-up design docs or issues.

## Stage 1: Architecture Design

Status: complete.

Goal: write enough design to prevent the first implementation from baking in
bad lifecycle boundaries.

Tasks:

- Define service module boundaries:
  - HTTP API.
  - session registry.
  - durable session storage.
  - job manager.
  - output capture/truncation.
  - trace writer.
  - runtime launcher.
  - idle reaper.
- Define on-disk session layout.
- Define request/response schemas.
- Define config schema and defaults.
- Define runtime labels, bundle paths, and cleanup/reconciliation behavior.
- Define error taxonomy.

More design needed: yes.

Exit criteria:

- `docs/design/architecture.md` exists.
- `docs/design/api.md` exists.
- `docs/design/session-layout.md` exists.
- Python 3.13 with FastAPI/Pydantic is the implementation choice for Stage 2.

## Stage 2: Local Service Skeleton

Status: complete.

Goal: create a runnable service without real gVisor execution yet.

Tasks:

- Completed: add project packaging and dependency management.
- Completed: implement config loading.
- Completed: implement health endpoint.
- Completed: implement request/response models.
- Completed: implement `run`, `poll`, and `cancel` API routes behind a fake executor.
- Completed: implement session id and job id validation.
- Completed: add structured logging.
- Completed: add initial unit and API tests.

More design needed: limited.

Notes:

- The fake executor should exercise lifecycle and API behavior without pretending
  to be secure.
- Keep the executor interface close to what the real `runsc` backend needs.

Exit criteria:

- Service starts locally.
- Unit/API tests run on macOS.
- API semantics are testable without gVisor.

## Stage 3: Durable Session Storage And Trace

Status: complete.

Goal: make session directories and job records the durable source of truth.

Tasks:

- Completed: implement configured sessions root, durable session/job layout, and output files.
- Completed: implement atomic state writes, crash-tolerant JSONL trace repair, and restart reconciliation.
- Completed: serve bounded head/tail output with robust UTF-8 replacement decoding.
- Completed: add persistence, truncation, trace, and restart-reconciliation tests.

More design needed: moderate.

Open questions:

- Exact trace format: JSONL is likely, but should be confirmed.
- Whether job state is stored as JSON files, SQLite, or derived mostly from
  process/runtime state.

Exit criteria:

- A completed command leaves durable stdout, stderr, state, and trace records.
- Restart can reconstruct enough state for completed jobs from disk.

## Stage 4: Process Job Manager

Status: complete.

Goal: implement real local process job semantics before adding gVisor.

Tasks:

- Completed: launch commands as process groups with empty/null stdin.
- Completed: capture stdout/stderr to durable files while running.
- Completed: enforce wall-clock timeout and process-group cancellation.
- Completed: implement one-live-job-per-session busy behavior and foreground soft-cap wait.
- Completed: add local-process tests for completion, running, timeout,
  cancellation, and busy responses.

More design needed: limited.

Notes:

- This stage is not a security boundary. It validates job semantics only.

Exit criteria:

- Job behavior matches the API contract using ordinary local processes.

## Stage 5: OCI Bundle And `runsc` Runtime Backend

Status: in progress.  A privileged NixOS smoke test passed with `runsc`,
`systrap`, no network, a read-only rootfs, and a writable workspace.  The
pinned Python rootfs, OCI bundle generator, service deployment, and full
resource/lifecycle integration suite remain.  See `docs/design/nixos-runtime.md`.

Goal: execute jobs inside real gVisor sandboxes using direct `runsc run`.

Tasks:

- Add runtime backend that generates OCI bundles.
- Mount session directory at the fixed in-container work path.
- Configure no-network sandbox execution.
- Configure read-only base filesystem plus writable session and scratch paths.
- Hold warm containers open between jobs.
- Exec jobs into warm containers or define the equivalent direct-`runsc`
  mechanism.
- Label/tag containers by session for reconciliation.
- Implement orphan cleanup on service startup.
- Add runtime integration tests.

More design needed: high.

Open questions:

- Exact mechanism for executing multiple jobs into a warm direct-`runsc`
  container.
- Rootfs/image preparation flow for OCI bundle generation.
- How disk quotas are enforced on the NixOS deployment host.
- How CPU/memory limits map into direct `runsc` config.

Exit criteria:

- A NixOS integration test boots a sandbox, runs Python, writes to the session
  directory, reaps the container, and resumes from the same directory.

## Stage 6: Sandbox Image

Status: not started.

Goal: build the offline-complete Python image used by the runtime backend.

Tasks:

- Choose Python version and patch release.
- Choose initial package list.
- Pin exact versions.
- Build image/rootfs reproducibly.
- Include common shell utilities.
- Confirm runtime package installation fails legibly without network.
- Record image identity/version in trace records.

More design needed: yes.

Open questions:

- Python 3.14 availability and scientific wheel maturity at implementation time.
- Whether to build from a container image, Nix closure, debootstrap rootfs, or
  another reproducible rootfs source.

Exit criteria:

- Runtime integration tests use the pinned image/rootfs.
- Trace records include image identity.

## Stage 7: Idle Reaper And Lifecycle Hardening

Status: not started.

Goal: make long-running service behavior predictable.

Tasks:

- Implement 30-minute idle reap.
- Treat live jobs as activity.
- Update activity timestamp on job completion.
- Ensure reaper cannot race with launch/cancel/poll.
- Add startup reconciliation.
- Add lifecycle metrics and logs.
- Add tests for reaping and non-reaping of active jobs.

More design needed: moderate.

Exit criteria:

- Idle containers are destroyed without touching session directories.
- Live jobs are never reaped out from under themselves.

## Stage 8: Resource Limits And Security Verification

Status: blocked until NixOS/runtime host.

Goal: prove the sandbox matches the security model.

Tasks:

- Enforce CPU limits.
- Enforce memory limits.
- Enforce job wall-clock timeout.
- Enforce session disk quota.
- Verify no network from inside sandbox.
- Verify base filesystem is read-only.
- Verify writable paths are only intended mounts.
- Verify dropped capabilities and unprivileged operation.
- Add automated security integration tests.

More design needed: high.

Exit criteria:

- Security tests fail closed when limits or isolation are missing.

## Stage 9: Deployment Artifacts

Status: not started.

Goal: deploy Pyvisor using the same NixOS/Quadlet/Tailscale sidecar pattern as
existing services.

Tasks:

- Add container build artifact.
- Add NixOS service module or deploy snippet.
- Add Quadlet network definition.
- Add app container definition.
- Add `tsnsrv-pyvisor` sidecar definition.
- Add workload directory layout under `/workloads/pyvisor`.
- Add health check and restart policy.
- Document required mounts, including optional `/dev/kvm`.

More design needed: moderate.

Exit criteria:

- Service is reachable over the Tailscale sidecar.
- Health check passes.
- A real agent harness can call `run`, `poll`, and `cancel`.

## Stage 10: Archival And RL Corpus

Status: later.

Goal: prevent local session directories from becoming the permanent corpus.

Tasks:

- Define archive format.
- Define object storage target and credentials flow.
- Implement periodic sweep of idle session directories.
- Preserve trace and final filesystem state.
- Define restore or read-only inspection workflow.

More design needed: yes.

Exit criteria:

- Idle sessions can be archived without losing RL-relevant data.
- Local disk remains a bounded working set.

## Suggested First Implementation Slice

The first useful code slice should stop before gVisor:

1. Service skeleton.
2. API schemas.
3. durable session/job layout.
4. local process executor.
5. unit/API tests.

That slice proves the agent-facing contract and job semantics on macOS. The next
slice on NixOS replaces the executor with the real `runsc` backend and adds
security integration tests.
