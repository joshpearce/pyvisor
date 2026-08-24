# Pyvisor Roadmap

This roadmap orders the work so the project can move from design notes to a
tested NixOS deployment without hiding security or runtime uncertainty.

Stages 0-10 below are the long arc. If you are picking up a discrete task right
now, go to **Deployment Review Follow-Ups** near the end of this file: it holds
the open `FU-` items from the first NAS deployment, each scoped small enough to
finish in one sitting, and it explains how to claim one and record the result.

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
- Pending: FU-4 in Deployment Review Follow-Ups — `JobResponse` omits `reason`,
  and the documented `503`/`500` for a failed runtime setup is really a `200`.

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
- Pending: FU-3 in Deployment Review Follow-Ups — the executor start-failure
  path has no test coverage.
- Pending: FU-5 in Deployment Review Follow-Ups — the runsc launch diagnostic
  is captured and then deleted with the bundle.

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
The artifact ran real jobs on the NAS on 2026-08-24: a submitted command
reported `Python 3.13.14`, the rootfs derivation's own interpreter, and durable
job records carry the rootfs store path as `sandbox_image`.  See
`docs/deployment-notes.md`.

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
- Completed: derive the deployed identity from the `sandbox-rootfs` derivation
  instead of the `REPLACE_WITH_NAR_HASH` placeholder.
- Completed: FU-2 — runsc startup rejects empty and placeholder sandbox image
  identities.
- Pending: FU-7 in Deployment Review Follow-Ups — the identity is derived in
  the `nixconfigs` repo rather than in this flake.

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
- Completed: FU-1 — structured JSON logs include bounded exception type and
  message details.

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

Status: in progress. Deployed to the NAS on 2026-08-24 and serving jobs over the
Tailscale sidecar. The health check passes, the sessions dataset is durable and
replicated off-site, and a submitted command ran to completion inside a gVisor
sandbox. `docs/deployment-notes.md` is the as-deployed reference and outranks the
`docs/design/nixos-deployment.md` draft wherever the two disagree. Remaining:
exercise `poll` and `cancel` from a real agent harness, and close the follow-ups
below.

Goal: deploy Pyvisor using the same NixOS/Quadlet/Tailscale sidecar pattern as
existing services.

Tasks:

- Completed operator action: create the persistent ZFS dataset at
  `lildeet/nasdata/workloads/pyvisor` under `lildeet/nasdata/workloads`. It is
  snapshotted and replicated off-site nightly; nothing outside
  `PYVISOR_SESSIONS_ROOT` is backed up.
- Completed: add a reproducible Nix-built service OCI archive.
- Completed: document a NixOS service/Quadlet deployment draft for review.
- Completed: document the private Quadlet network, app, and `tsnsrv-pyvisor`
  sidecar definitions.
- Completed: document the `/workloads/pyvisor` layout, health check, restart
  policy, required session mount, and intentionally absent `/dev/kvm` mount.
- Completed: record the as-deployed contracts, storage durability split, nested
  `runsc` requirements, and debugging commands in `docs/deployment-notes.md`.
- Pending: FU-6 in Deployment Review Follow-Ups — the deployment draft still
  contains values known to break a deploy.

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

## Deployment Review Follow-Ups

Status: in progress.

Goal: close the operability and contract gaps found by reviewing
`docs/deployment-notes.md` against the code after the first NAS deployment on
2026-08-24.

Each item is self-contained: the evidence, the change, the traps that will bite
you, and the check that proves it done.  None of them change the sandbox
security model.  They make failures diagnosable and make the documented
contracts match the code.

### Working This Queue

1. Pick the highest-priority item with `Status: pending` whose `Blocked by` line
   is empty or names a completed item.
2. Set it to `Status: in progress` in this file *before* starting, so a parallel
   agent does not pick up the same one.
3. Make the change.  Follow `agents.md`.  Keep it scoped — if you find adjacent
   work, add a new `FU-` item rather than widening the one you hold.
4. Run `uv run pytest` and `uv run ruff check .`.  Both must pass.  The runsc
   integration suite stays skipped off NixOS; say so rather than implying it ran.
5. Set `Status: complete (<short-sha>)`.  If what you did differs from what the
   item describes, rewrite the item to say what you actually did.
6. Add a matching `Completed:` line to the stage named in the item's `Stage:`
   field, so the stage records stay true.

### FU-1: Emit Exception Detail In Structured Logs

- Status: complete (94420c0)
- Priority: high
- Stage: 7
- Blocked by: none
- Files: `src/pyvisor/logging.py`, `tests/test_logging.py` (new)

`JsonFormatter.format` (`src/pyvisor/logging.py:14-25`) builds its event dict
from a fixed key list and never calls `formatException`, so `record.exc_info` is
discarded.  Every `LOGGER.exception` call in the service logs an event name and
nothing else — ten sites: `app.py:96,114,132` and
`jobs.py:99,122,208,220,258,320,359`.

This is why the `--rootless` bug took an afternoon instead of a minute.
`RuntimeUnavailableError` already carries runsc's stderr
(`runsc_executor.py:558-565`), and `jobs.py:258` already logs it with
`LOGGER.exception`.  The detail reached the formatter and died there.

Change: include the exception type and message in the JSON event when
`record.exc_info` is set.  A starting point:

```python
if record.exc_info:
    exc_type, exc_value, _ = record.exc_info
    event["error_type"] = exc_type.__name__
    event["error"] = str(exc_value)[:_ERROR_DETAIL_LIMIT]
```

Watch out for:

- The `extra` allow-list on line 21 is hardcoded to `session_id`, `job_id`,
  `status`, `reason`, `count`.  Any other field passed via `extra=` is dropped
  silently.  Extend the tuple if you add one.
- The detail is unbounded — runsc's stderr goes into the message verbatim.  Cap
  it, or one bad launch writes a multi-megabyte log line.
- Keep the `event` value a stable snake_case token.  It is the log's join key.
- A full traceback is noisier than the message and rarely the useful part here.
  Type plus message is enough; add `formatException` only if you decide you want
  it.

Done when: a unit test asserts a logged exception's message appears in the JSON
output, and the suite passes.

### FU-2: Reject Placeholder Sandbox Image Identities At Startup

- Status: complete (a2c933a)
- Priority: high
- Stage: 6
- Blocked by: none
- Files: `src/pyvisor/config.py`, `tests/test_config.py`

`config.py:97-98` rejects only an empty identity, so a placeholder or stale
value is accepted silently and the `io.pyvisor.sandbox_image` annotation and
every durable job record become meaningless provenance.

A guard does exist at `runsc_executor.py:467`, but it fires at *job start*, not
startup, and matches only the literal default sentinel.  A misconfigured
deployment therefore boots, passes `/healthz`, reports green, and fails every
submission.  Removing that failure shape is the point of this item.

Change: validate in `AppConfig.from_environment()`, alongside the existing
`execution_backend` check at `config.py:95`.  Reject values that are empty,
equal to the `pyvisor-sandbox:unconfigured` sentinel, or contain `REPLACE_`.

Watch out for:

- **Gate it on the backend.**  `execution_backend` defaults to `local` and the
  identity defaults to the sentinel, so an unconditional check breaks every
  local development run.  Require a real identity only when
  `execution_backend == "runsc"`.
- **Do not assert a digest shape.**  The deployed value is
  `pyvisor-sandbox-python-3.13@<store-hash>-pyvisor-sandbox-rootfs-3.13.14` —
  no `sha256:` prefix.  `docs/design/sandbox-image.md:51`, `README.md:61`, and
  all six unit-test configs use `@sha256:...`.  A rule requiring `sha256:`
  rejects production on the next restart.
- `AppConfig` is a frozen slots dataclass and tests construct it directly, so
  validation in `__post_init__` would break `tests/test_runsc_executor.py`.
  Keep it in `from_environment`.
- `test_config_uses_documented_defaults` clears only two environment variables
  and inherits the rest of the ambient environment.
- Leave the `runsc_executor.py:467` guard in place as defence in depth.

Done when: a test in `tests/test_config.py` follows the existing
`pytest.raises(ConfigurationError, match=...)` style for a placeholder value, a
local-backend default still loads, and the suite passes.

### FU-3: Cover The Executor Start-Failure Path With Tests

- Status: in progress
- Priority: high
- Stage: 5
- Blocked by: none
- Files: `tests/test_runsc_executor.py`, `tests/test_api.py`,
  `src/pyvisor/fake_executor.py`

Nothing tests the `_runtime()` launch-failure path, the `job_start_failed` log,
or `reason == "executor_start_failed"`.  The bug that broke the first deployment
lived in untested code, and FU-1, FU-4, and FU-5 all change behaviour here.

Change: add a unit test where the faked `runsc run` exits non-zero, and an
API-level test that a start failure produces a durable `runtime_error` record.

How the existing fakes work (`tests/test_runsc_executor.py:254-292`):
`AppConfig.runsc_binary` points at a stub file checked only for `is_file()` and
the executable bit — it is never executed — and
`pyvisor.runsc_executor.asyncio.create_subprocess_exec` is monkeypatched at the
module attribute.

Watch out for:

- No existing fake writes to `kwargs["stderr"]`; every other call passes
  `DEVNULL`.  To exercise the diagnostic capture, the fake must write into the
  handle it is given before returning a non-zero `wait()`.
- `FakeExecutor.start()` (`src/pyvisor/fake_executor.py:37-71`) has no failure
  mode, so the API-level test needs a raising stub executor or a new hook.

Done when: both tests pass, and fail if the production code they cover is
reverted.

### FU-4: Tell The Caller Why A Job Failed To Start

- Status: pending
- Priority: medium
- Stage: 2
- Blocked by: none
- Files: `src/pyvisor/models.py`, `src/pyvisor/jobs.py`, `docs/design/api.md`,
  `tests/test_api.py`

`JobResponse` (`models.py:29-38`) has no `reason` field, and `_response`
(`jobs.py:131-143`) drops `record.reason` when projecting.  An agent harness
sees `status: "runtime_error"`, a null exit code, and two empty output streams,
with no way to distinguish a failed sandbox launch from anything else.

`docs/design/api.md:77-79` also says failed runtime setup returns `503`/`500`.
The code returns `200` (`app.py:82-99`, `jobs.py:249-271`).  One of the two is
wrong.

Change: expose `reason` on `JobResponse`, populate it in `_response`, and
correct `api.md` to describe the `200`.

Keep the `200`.  A failed start still creates a durable job record with a real
`job_id`, which `api.md` already promises.  A bodiless `503` throws away the
identifier the caller needs to inspect that record.  The status code is the
wrong channel for this; `reason` is the right one.

Watch out for:

- `reason` is a closed vocabulary, not prose: `timeout`, `cancel_requested`,
  `service_restart`, `executor_start_failed`, `executor_wait_failed`,
  `timeout_termination_failed`, `cancel_termination_failed`,
  `missing_live_executor`.  Keep it that way — raw runsc output does not belong
  in an API field, and `docs/design/session-layout.md:53-55` forbids unredacted
  implementation exceptions in durable state.
- `models.py` sets `extra="forbid"` on the request model only, so adding an
  optional response field is safe.

Done when: an API test asserts `reason` on a failed start, `api.md` matches the
code, and the suite passes.

### FU-5: Preserve Or Log The runsc Launch Diagnostic

- Status: pending
- Priority: medium
- Stage: 5
- Blocked by: FU-1
- Files: `src/pyvisor/runsc_executor.py`, `docs/design/session-layout.md`

`_runtime()` writes runsc's stderr to `runsc-launch.stderr` in the bundle and
folds it into `RuntimeUnavailableError` (`runsc_executor.py:529-565`).  The
`except` branch at `:566-567` then removes the bundle, destroying that file.

**Do FU-1 first, then re-judge this item.**  Once the formatter emits exception
detail the message already reaches the operator, and bundle retention may not be
worth its cost.  If you still want the artefact on disk, these are the traps:

- `reconcile_orphans()` (`runsc_executor.py:336-359`) walks `runsc list`, not the
  bundles directory.  A retained bundle with no live container is never visited
  by anything and leaks until the container restarts.
- `tests/test_runsc_runtime_integration.py:133` asserts the bundles directory is
  empty after `aclose()`.
- `docs/design/session-layout.md:79-81` declares runtime bundles disposable.
  Retention needs that line changed, not ignored.
- `PYVISOR_RUNTIME_ROOT` is the container's writable layer, so a retained bundle
  dies with the container anyway.  It buys a live shell session, not history.

Separately, and whichever way you go: a failed `runsc run` can leave state in the
`runsc --root` directory, and the `except` branch removes only the bundle.  No
`runsc delete` is attempted.

Done when: a start failure leaves the operator a readable diagnostic without an
unbounded on-disk leak, and the decision is recorded in this item.

### FU-6: Correct The NixOS Deployment Draft

- Status: pending
- Priority: medium
- Stage: 9
- Blocked by: none
- Files: `docs/design/nixos-deployment.md`

`docs/deployment-notes.md` says the draft loses where the two disagree.  That is
fine for someone who reads both, but the draft is the copy-paste source, and it
still contains a bug the notes already diagnosed:

- `:129` `HostName=pyvisor` with `:156` `-name pyvisor` is exactly the tsnsrv
  DNS collision that produced the `502`.  The deployed backend is `pyvisor-app`.
- `:132` uses the host store path `${pkgs.curl}/bin/curl`.  The deployment uses
  the image's own `/bin/curl`, because a host path resolves inside the container
  only by coincidence of a shared nixpkgs.
- `:131` still carries `REPLACE_WITH_NAR_HASH`.
- The draft sets no `MemoryMax`, `CPUQuota`, or `TasksMax`.  The deployment
  applies `MemoryMax=16G`, `CPUQuota=400%`, `TasksMax=2048` on the outer unit,
  and those are the only kernel-enforced limits in the system.

Change: fix those four, and mark the file at the top as a draft superseded by
`docs/deployment-notes.md`.

Watch out for: the authoritative copy is `nas/pyvisor.nix` in the `nixconfigs`
repo.  Read it before editing rather than copying the notes second-hand.

Done when: the draft no longer contains a value known to break a deployment.

### FU-7: Derive The Sandbox Image Identity In The Flake

- Status: pending
- Priority: low
- Stage: 6
- Blocked by: FU-2
- Files: `flake.nix`, `docs/deployment-notes.md`, and `nas/pyvisor.nix` in the
  `nixconfigs` repo

`flake.nix:146-152` bakes `PYVISOR_SANDBOX_ROOTFS=${sandboxRootfs}` into the
container image but sets no `PYVISOR_SANDBOX_IMAGE_IDENTITY`, so the derivation
lives in the `nixconfigs` repo.  Nothing structurally stops the two from
disagreeing; only the discipline of changing both.

Change: set `PYVISOR_SANDBOX_IMAGE_IDENTITY` in the image `Env`, derived from
the same expression the image ships —
`pyvisor-sandbox-python-3.13@${baseNameOf sandboxRootfs}`.  The identity then
cannot describe a rootfs other than the one baked in, and the NixOS module can
stop computing it.

Watch out for:

- This is a cross-repo change.  The `nixconfigs` `Environment=` line must be
  removed in the same deploy, or it silently overrides the image default.
- Add the identity to the contract table in `docs/deployment-notes.md`.  It is a
  contract between the two repos today and is written down nowhere.
- Verifying needs `nix build .#pyvisor-container` on a NixOS host.  Say so if you
  cannot run it.

Done when: inspecting the built image shows the derived identity, and the
`nixconfigs` override is gone.

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
