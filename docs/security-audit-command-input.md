# Security Audit: Consumer-Supplied Command Strings

Assessment date: 2026-08-24

Repository revision: `54687712c141beadcbd9f645ecc169dabdc61b0b`

## Executive summary

Pyvisor's production `runsc` launch path does not expose a conventional host
shell-injection vulnerability. The command is passed as one argv element to
`runsc exec`, after the fixed sandbox executable and `-lc` arguments. Session,
job, and container identifiers are separately constrained, and the command is
serialized with JSON APIs rather than interpolated into OCI JSON, paths, or
host-side shell text.

The service is not yet safe against a malicious or compromised consumer,
however, even under the assumption that gVisor correctly isolates code from the
host kernel. The largest risks are operational paths around the sandbox:

- a missing backend environment variable silently selects direct host execution;
- command output and workspace writes can exhaust durable host storage;
- processes created by a command can outlive the process Pyvisor tracks;
- cancellation confuses a launcher-side `runsc` PID with a sandbox PID and can
  abandon an incompletely deleted runtime; and
- request size and per-sandbox compute resources are not bounded at the point
  where the host pays their cost.

The overall risk is **high** for the documented deployment and **critical** for
any deployment that can accidentally start with the default local backend.
Untrusted command traffic should not be broadened beyond the current restricted
tailnet audience until the critical and high findings are resolved and exercised
against the pinned NixOS/runsc environment.

## Scope and assumptions

This review follows `command` from the HTTP request through validation,
persistence, process launch, output capture, timeout, cancellation, reaping, and
deployment controls. The assets in scope are the host, the outer Pyvisor service
container, durable host storage, and availability of other Pyvisor sessions.

The review assumes:

- code executing inside a correctly configured gVisor sandbox cannot directly
  escape it; gVisor implementation vulnerabilities are out of scope;
- the attacker can submit arbitrary JSON command strings and choose valid
  session IDs through an authorized consumer path;
- the as-deployed notes accurately describe the outer rootless Podman and systemd
  limits; and
- application authentication, tailnet policy correctness, and attacks using
  fields other than the command string are adjacent concerns, not the primary
  subject of this assessment.

This was a source and configuration review. No destructive proof-of-concept was
run against the NAS, and runtime-dependent findings were not dynamically tested
against its pinned `runsc` binary.

## Severity scale

| Severity | Meaning in this assessment |
| --- | --- |
| Critical | Direct host command execution, or a similarly complete loss of the host boundary |
| High | A practical command-origin attack can defeat a core containment control or exhaust host-backed resources |
| Medium | A practical denial of service is contained by an outer boundary, requires amplification, or has a narrower blast radius |
| Low | Hardening weakness with limited standalone impact |

## Findings

### SEC-001: The service fails open to direct host command execution

**Severity: Critical (configuration-dependent)**

`AppConfig.execution_backend` defaults to `local`, and the environment loader
uses the same default (`src/pyvisor/config.py:52,78`). `JobService` selects
`LocalProcessExecutor` for every value other than `runsc`
(`src/pyvisor/jobs.py:77-81`). That executor intentionally invokes
`/bin/bash -lc <consumer-command>` as an ordinary process with the service's host
credentials (`src/pyvisor/local_executor.py:51-73`).

The checked-in container image sets `PYVISOR_EXECUTION_BACKEND=runsc`, so the
current documented image does not take this path. The failure mode remains
dangerous: an alternate package launch, an incomplete deployment manifest, an
environment override, or reuse of the application factory outside the image
silently converts the sandbox API into host remote-code execution. Documentation
that calls the backend development-only is not a security control.

**Recommendation**

- Make `runsc` the only implicit/default backend. Require an explicit, conspicuous
  development opt-in for local execution, such as
  `PYVISOR_UNSAFE_LOCAL_EXECUTION=1` in addition to selecting `local`.
- Refuse local mode when listening anywhere other than an exact loopback address.
- Emit the selected backend in the boot log and health/readiness state, and make
  deployment health checks fail unless the active backend is `runsc`.
- Add a packaging/deployment test that starts the built artifact with its real
  environment and asserts that a submitted command reaches `RunscExecutor`.

### SEC-002: Command-controlled writes can exhaust host durable storage

**Severity: High**

The `runsc` launcher opens durable `stdout.bin` and `stderr.bin` files on the
host and passes those file descriptors to the sandbox process
(`src/pyvisor/runsc_executor.py:543-575`). The files preserve full output by
design. `output_limit_bytes` limits only how much an API read returns; it reads a
head/tail window after consulting the full file size
(`src/pyvisor/storage.py:287-317`). It does not limit bytes written.

The sandbox also receives the durable session workspace as a writable host bind
mount (`src/pyvisor/runsc_executor.py:188-193`). Although
`session_disk_limit_bytes` is configured, it is never consumed by storage or the
runtime. The repository's deployed ZFS creation example does not set a quota,
and the deployment notes explicitly state that per-session disk limits remain
future work.

A small command can therefore generate stdout/stderr continuously or fill
`/workspace` until the shared dataset or pool is exhausted. The maximum one-hour
job timeout is not a meaningful storage bound at typical output rates. Full
storage can prevent state transitions, traces, logs, unrelated host workloads,
snapshots, or other sessions from making progress. API truncation can make this
growth less visible to the consumer while it continues on disk.

**Recommendation**

- Enforce a hard host-side quota for every session directory, covering both
  workspace and job output. A dataset/project quota is preferable to an
  in-process size check because the sandbox writes through a bind mount.
- Put the overall sessions root on a host filesystem with a reserved-capacity
  policy so one service cannot fill the host or storage pool.
- Add per-stream durable output ceilings. When reached, stop capture in a defined
  way and terminate the job with an explicit limit status; do not silently let
  the producer block forever on a full pipe.
- Account for command/state/trace bytes as well as workspace and output, and make
  quota exhaustion produce a durable, bounded error when space remains reserved
  for metadata.

### SEC-003: A command can leave untracked processes running after job completion

**Severity: High**

Pyvisor treats the exit of the single `runsc exec` operation as completion
(`src/pyvisor/runsc_executor.py:251-255`, `src/pyvisor/jobs.py:200-226`). Upstream
[`runsc exec`](https://github.com/google/gvisor/blob/master/runsc/cmd/exec.go)
waits for the one sandbox PID returned by `Execute`; it does not wait for or
terminate that process's descendants. The warm sandbox is deliberately kept
alive for later jobs.

A command can fork or daemonize a child and let its tracked parent exit. Pyvisor
then records `completed`, releases the one-live-job slot, and starts the idle
timer while the child continues in the sandbox. That child can consume CPU,
memory, task slots, workspace space, or inherited stdout/stderr file descriptors.
The wall-clock timeout no longer applies because its watcher has already
completed. Later jobs in the same session can overlap the hidden workload.

This does not require a gVisor escape. It defeats Pyvisor's host-resource and
one-live-job assumptions from inside the expected sandbox API.

**Recommendation**

- Define a job as a sandbox process tree or process group, not a single PID.
- Launch each job through a small sandbox-local supervisor that creates a fresh
  process group, reaps descendants, kills the complete group on normal parent
  exit/timeout/cancel, and reports completion only after the group is empty.
  Destroying the per-session runtime after every job is a simpler safe fallback
  if a reliable per-job group cannot be implemented while retaining warm
  containers.
- Before releasing the live-job slot, positively verify that no process belonging
  to the job remains.
- Add a real-runsc test where a parent exits after spawning a CPU loop and an
  output-writing descendant; neither process nor output growth may survive the
  terminal job transition.

### SEC-004: Cancellation uses the wrong PID namespace and can abandon runtimes

**Severity: High**

Pyvisor starts `runsc exec` with `--pid-file`, reads that integer, and passes it
to `runsc kill --pid` (`src/pyvisor/runsc_executor.py:301-336,565-570`). These
flags do not describe the same PID namespace:

- upstream `runsc exec --pid-file` writes `os.Getpid()`, the launcher-side
  `runsc` process PID outside the gVisor PID namespace; `--internal-pid-file` is
  the separate flag for the sandbox PID;
- upstream `runsc kill --pid` documents its value as relative to the container's
  root PID namespace. It also provides `--pgid` for process-group signaling.

See the upstream gVisor implementations of
[`exec`](https://github.com/google/gvisor/blob/master/runsc/cmd/exec.go) and
[`kill`](https://github.com/google/gvisor/blob/master/runsc/cmd/kill.go).

The usual outcome is that targeted signaling fails because the launcher PID
does not exist in the sandbox. The fallback force-deletes the entire warm runtime,
which may still stop the command, but it is not the targeted cancellation the
code and tests claim. A coincidental numeric PID match could signal the wrong
process inside that session.

The failure path is more serious. If `runsc delete --force` fails or exceeds the
three-second control timeout, `terminate()` kills only the launcher-side process
and calls `_discard_runtime()` (`src/pyvisor/runsc_executor.py:257-299`). Discarding
removes the runtime from the in-memory map and deletes its bundle even though
runtime destruction was not confirmed (`src/pyvisor/runsc_executor.py:672-678`).
Startup reconciliation requires that bundle and its annotations to verify an
orphan (`src/pyvisor/runsc_executor.py:445-503`), so the remaining runtime can no
longer be reclaimed by the application's normal reconciliation logic. The job is
nevertheless reported as `cancelled` or `timed_out` with no exit code.

**Recommendation**

- Use `--internal-pid-file` for sandbox PID targeting, and establish and signal a
  verified per-job process group with `runsc kill --pgid`. Validate the behavior
  against the exact pinned `runsc` derivation rather than only mocked subprocesses.
- Never discard runtime identity or its bundle until deletion is confirmed.
  Quarantine an uncertain runtime, reject new jobs for that session, retain the
  evidence needed for reconciliation, and retry bounded cleanup observably.
- If process-group termination cannot be proven, destroy the whole runtime and
  report success only after `runsc state/list` confirms it is gone.
- Add NixOS integration tests for TERM, TERM-resistant descendants, KILL
  escalation, wrong/nonexistent PIDs, slow deletion, restart after failed
  deletion, and absence of the old container before a replacement is launched.

### SEC-005: CPU, memory, and process limits are aggregate rather than per command

**Severity: Medium**

`cpu_limit` and `memory_limit_bytes` are parsed but never applied to the OCI
bundle or execution path. There is no per-sandbox PID limit. The `runsc` launch
uses `--ignore-cgroups` (`src/pyvisor/runsc_executor.py:600-627`), and the
deployment documentation confirms that `MemoryMax=16G`, `CPUQuota=400%`, and
`TasksMax=2048` apply only to the whole outer service.

Those outer limits are valuable host protection, but a single command, hidden
descendants from SEC-003, or commands spread across caller-selected session IDs
can consume the complete Pyvisor allocation and take down all sessions. The
configured per-command values create a misleading appearance of enforcement.

**Recommendation**

- Keep and verify the outer systemd limits as the last host-safety boundary.
- Add a global concurrency/admission limit so arbitrary session IDs cannot create
  unbounded simultaneous warm runtimes and jobs.
- Enforce per-runtime memory and PID limits through a host mechanism compatible
  with the rootless deployment. If per-sandbox CPU quotas remain infeasible,
  document that explicitly in API/operator policy and use admission control to
  preserve service availability.
- Fail readiness or expose an explicit degraded security state when configured
  limits are not actually enforced; do not treat parsed values as active limits.

### SEC-006: The command limit is applied after the full HTTP body is buffered

**Severity: Medium**

`RunJobRequest` checks only that `command` is non-empty
(`src/pyvisor/models.py:24-30`). FastAPI/Starlette must receive and parse the full
JSON body before `JobService.run()` encodes the string and enforces the 65,536
byte limit (`src/pyvisor/jobs.py:228-233`). No application or documented ingress
request-body limit is present.

A consumer can submit a very large JSON command value and force the service to
buffer, decode, and allocate it before receiving a rejection. Concurrent large
requests can exhaust the outer service memory limit and restart or stall the
service. The limit therefore protects durable state and argv size, but not host
memory at ingress.

**Recommendation**

- Enforce a small request-body byte limit before JSON parsing, at the ASGI server
  or a streaming middleware, and mirror it in the trusted ingress where possible.
- Reject oversized `Content-Length` immediately and enforce the same limit while
  streaming for absent or dishonest lengths.
- Keep the post-parse UTF-8 byte check as defense in depth and reject invalid
  Unicode scalar values with a stable 400 response.
- Add tests for oversized declared bodies, chunked bodies, multibyte UTF-8 at the
  boundary, and escaped lone surrogates.

## Controls that are working

The following controls materially reduce command-string attack surface and
should be preserved:

- The production executor uses `asyncio.create_subprocess_exec`, not a host shell,
  and supplies the consumer command as one final argv element after
  `/bin/bash -lc` (`src/pyvisor/runsc_executor.py:545-575`). Shell metacharacters,
  newlines, leading hyphens, and substitutions are interpreted only by Bash
  inside the sandbox.
- The command is not interpolated into the OCI bundle. `json.dumps` serializes
  fixed configuration and validated session metadata.
- Session IDs use a strict 64-character allowlist, job IDs are canonical UUIDs,
  and container IDs are service-generated UUIDs. Storage also revalidates IDs
  before path construction and rejects symlinked managed directories.
- The only caller-associated host mount is the validated session workspace at a
  fixed `/workspace` destination. The rootfs is read-only, scratch is bounded,
  capabilities are empty, `noNewPrivileges` is set, and `runsc` is invoked with
  `--network=none` and `systrap`.
- The `runsc` backend rejects real host root. The deployed service is additionally
  contained by rootless Podman under a dedicated host account.
- Commands are persisted and traced through JSON serialization rather than raw
  log-line concatenation. Lifecycle logs do not include the command string, and
  exception text is bounded.
- API output reads are byte-bounded, stdout and stderr remain separate, and
  invalid UTF-8 output is decoded with replacement.

## Remediation order

1. Remove the fail-open local backend default.
2. Correct sandbox PID/process-group tracking and make failed runtime deletion
   fail closed without losing reconciliation state.
3. Prevent post-job descendants and prove timeout/cancel process-tree behavior on
   the pinned NixOS runtime.
4. Enforce host-backed session/output quotas with reserved metadata capacity.
5. Add early HTTP body limits, then global admission and enforceable runtime
   resource limits.

Until items 1-4 are complete, the current tailnet restriction and outer
systemd limits should be treated as essential compensating controls, not optional
deployment conveniences.
