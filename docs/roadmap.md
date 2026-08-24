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

Status: in progress. A NixOS rootless integration test passed with `runsc`,
`systrap`, no network, a read-only rootfs, and a writable workspace. The
rootless service uses aggregate systemd resource limits; per-sandbox resource
limits and the remaining lifecycle/security coverage are still future work.
See `docs/design/nixos-runtime.md`.

Goal: execute jobs inside real gVisor sandboxes using direct `runsc run`.

Tasks:

- Completed: add a direct `runsc` backend that generates restrictive per-job OCI bundles.
- Completed: mount the session workspace at fixed in-container `/workspace`.
- Completed: invoke `runsc` with `--network=none` and `--platform=systrap`.
- Completed: generate a read-only base filesystem with only workspace and bounded scratch writable.
- Completed: hold a per-session warm container open between jobs.
- Completed: execute jobs with `runsc exec` in the warm container, including
  targeted exec-PID cancellation so a normal job cancellation preserves the
  session runtime.
- Completed: label generated containers with managed, session, and image-identity OCI annotations.
- Completed: reconcile startup orphans by force-deleting only verified managed
  `runsc` containers and their generated bundles before accepting traffic.
- Completed: add host-gated runtime integration tests for warm boot, Python
  execution, workspace persistence, and orderly runtime cleanup.
- Completed: run that suite from a dedicated rootless NixOS service using a
  single-UID OCI user namespace and no sandbox network.

More design needed: high.

Open questions:

- Rootfs/image preparation flow for OCI bundle generation.
- How disk quotas are enforced on the NixOS deployment host.
- Whether the aggregate systemd limits need future per-session fairness.

Exit criteria:

- A NixOS integration test boots a sandbox, runs Python, writes to the session
  directory, reaps the container, and resumes from the same directory.

## Stage 6: Sandbox Image

Status: in progress.  The repository now defines a Nix-built, closure-complete
CPython 3.13 rootfs with the initial scientific stack and shell utilities.
The artifact and a delegated NixOS runtime execution remain to be validated.

Goal: build the offline-complete Python image used by the runtime backend.

Tasks:

- Completed: choose CPython 3.13 as the pinned fallback while Python 3.14
  wheel maturity remains uncertain.
- Completed: choose the initial scientific package list and common shell
  utilities.
- Completed: define a reproducible closure-complete Nix rootfs artifact.
- Completed: add the generated flake lock, which pins the exact versions.
- Completed: build the artifact from the pinned flake and verify its manifest,
  NAR identity, Python entry point, and shell compatibility links on NixOS.
- Completed: run the opt-in NixOS `runsc` integration suite through the
  dedicated rootless service, validating the built rootfs end to end.
- Completed: add a host-gated integration test that requires `pip install` to
  fail with a legible offline-network diagnostic.
- Pending: run that `pip install` check in the delegated NixOS service.
- Completed: document deriving an image identity from the rootfs NAR hash;
  trace records already persist the configured identity.

More design needed: yes.

Open questions:

- When the scientific stack is ready for Python 3.14, whether a deliberate
  image-version migration is justified.

Exit criteria:

- Runtime integration tests use the pinned image/rootfs.
- Trace records include image identity.

## Stage 7: Idle Reaper And Lifecycle Hardening

Status: in progress. The service now reaps idle warm runtimes under the same
per-session lock used for launch, terminalization, and cancellation; deployment
metrics and NixOS runtime validation remain.

Goal: make long-running service behavior predictable.

Tasks:

- Completed: implement configured idle reaping for warm runtimes.
- Completed: treat live jobs as activity.
- Completed: update activity timestamp on launch and terminal job transitions.
- Completed: synchronize reaping with launch, cancellation, and terminalization.
- Completed: retain startup reconciliation before the reaper begins.
- Completed: add structured idle-reap logs.
- Completed: expose process-level lifecycle metrics without high-cardinality
  session or job labels.
- Completed: add tests for reaping and non-reaping of active jobs.

More design needed: moderate.

Exit criteria:

- Idle containers are destroyed without touching session directories.
- Live jobs are never reaped out from under themselves.

## Stage 8: Resource Limits And Security Verification

Status: in progress. The rootless NixOS host is available for the opt-in
isolation suite, but per-sandbox CPU, memory, and disk enforcement still need a
host-level mechanism.

Goal: prove the sandbox matches the security model.

Tasks:

- Enforce CPU limits.
- Enforce memory limits.
- Enforce job wall-clock timeout.
- Enforce session disk quota.
- Pending: run the existing offline `pip` probe in the delegated NixOS service.
- Completed: add host-gated probes for a read-only rootfs, writable workspace
  and scratch mounts, and an empty effective capability set.
- Completed: add automated host-gated isolation integration tests.

More design needed: high.

Exit criteria:

- Security tests fail closed when limits or isolation are missing.

## Stage 9: Deployment Artifacts

Status: not started.

Goal: deploy Pyvisor using the same NixOS/Quadlet/Tailscale sidecar pattern as
existing services.

Tasks:

- Create the persistent ZFS dataset at `lildeet/nasdata/workloads/pyvisor`
  under `lildeet/nasdata/workloads` and mount it for the service workload.
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

## Unplanned Future Work

### OCI Registry Distribution Of The Sandbox Rootfs

Status: unplanned.

Goal: make the pinned Nix-built sandbox rootfs distributable through GitHub
Container Registry without making Docker or Podman part of Pyvisor's execution
path.

Tasks:

- Produce a reproducible OCI image or OCI artifact from the exact
  `sandbox-rootfs` Nix output.
- Publish it to `ghcr.io/joshpearce/pyvisor-sandbox`, associated with this
  repository's `origin` account.
- Define CI credentials, publishing permissions, version/tag policy, and
  retention policy.
- Record both the Nix NAR hash and OCI manifest digest, and define the one
  used as the runtime trace identity.
- Define verified pull and unpack steps that yield a non-symlink rootfs
  directory acceptable to direct `runsc` OCI bundles.
- Add an end-to-end deployment test proving that a pulled artifact produces
  the same rootfs behavior as the native Nix output.

More design needed: yes.

Notes:

- The Nix binary cache remains the native distribution mechanism.  This future
  work adds a registry-facing distribution format only.
- Docker and Podman must remain absent from the command-execution path; `runsc`
  continues to consume an unpacked rootfs directly.

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
