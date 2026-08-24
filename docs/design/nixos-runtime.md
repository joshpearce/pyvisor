# NixOS Runtime Prerequisites

This document records host-level conditions required before Pyvisor can claim
that its `runsc` backend and resource limits are tested.  It is intentionally
separate from the application design: these conditions are deployment policy,
not properties that an HTTP handler can safely manufacture at runtime.

## Host probe: 2026-08-23

The target is NixOS 26.11 on x86_64.  `runsc` is installed and advertises
`systrap`, `kvm`, and `ptrace`; Pyvisor will use `systrap`.  `/dev/kvm` is not
present, so KVM is not part of the first deployment.  `runsc` supports both
direct `run` and `exec`, confirming that a warm container plus per-job `exec`
is an available runtime model.

An isolated OCI bundle using a static BusyBox rootfs was exercised from
`/tmp` in the privileged host shell.  The bundle had a read-only rootfs, an
empty capability set, `noNewPrivileges: true`, a writable `/workspace` bind
mount owned by the unprivileged sandbox user, and `--network=none` with
`--platform=systrap`.  It successfully booted and verified all of the following:

- a workspace write persisted on the host;
- a rootfs write failed with `Read-only file system`;
- a statically linked TCP probe to `1.1.1.1:80` failed with `network is
  unreachable`; and
- `runsc list` was empty after the one-shot process exited.

On 2026-08-24, the NixOS `pyvisor-test` service passed the opt-in integration
suite using true rootless gVisor. Its OCI bundle maps only in-sandbox UID/GID 0
to the dedicated non-root service account, uses `--ignore-cgroups`, and omits a
network namespace because true-rootless gVisor does not support one. `runsc`
still receives `--network=none`, so the sandbox has no network. The test booted
a warm sandbox, executed Python twice, persisted the workspace, and reaped the
runtime.

This deployment deliberately has aggregate rather than per-sandbox resource
limits: the systemd service caps all of its sandboxes together. Per-session CPU,
memory, PID, and disk limits remain future work.

The pinned Stage 6 rootfs was built successfully from this repository's flake
on the host.  Its manifest, Python entry point, and shell compatibility links
were inspected successfully.  A subsequent direct launch from the ordinary
non-root login session failed as expected before container startup: its
non-delegated cgroup-v2 subtree denied `runsc` access to
`cgroup.subtree_control`.  This is not a rootfs failure and does not validate
the integration suite; run it from the delegated service or VM-test context.

## Required deployment conditions

1. Install and pin the `runsc` package/binary in the NixOS configuration.
   Record its version or Nix store derivation in deployment metadata and session
   traces.  The currently installed package reports `VERSION_MISSING`, so a
   derivation identity is especially important.
2. Run Pyvisor under a dedicated non-root account whose service policy permits
   user namespaces. The rootless OCI bundle maps only in-sandbox UID/GID 0 to
   that account's effective UID/GID; it must not grant additional host-ID
   mappings or enable `TESTONLY-unsafe-nonroot`.
3. Apply CPU, memory, and task limits to the enclosing systemd service. In the
   rootless design these are aggregate Pyvisor limits, not per-sandbox cgroups.
4. Give the service a private, owner-writable `runsc --root` directory on the
   host filesystem.  The directory holds control sockets and runtime state and
   is disposable; it must not be a session directory.
5. Build the sandbox rootfs separately and reproducibly.  The smoke probe used
   a static BusyBox binary only to exercise OCI mechanics; it is not the Python
   sandbox image.  The real rootfs must meet Stage 6's pinned offline-Python
   requirements.
6. Run the integration suite from a NixOS VM test or a suitably delegated
   service context, not from the constrained agent launcher used for ordinary
   development commands.

## Baseline runtime flags

The rootless backend begins each invocation with an explicit private runtime
root, `--ignore-cgroups`, `--platform=systrap`, and `--network=none`. OCI
configuration supplies a single-UID/GID user namespace, a read-only rootfs,
empty capabilities, no privilege escalation, and only the session workspace
plus bounded scratch as writable paths. It must not choose `ptrace`, use
`TESTONLY-unsafe-nonroot`, grant additional host-ID mappings, or silently fall
back to host networking.

## Next validation sequence

1. Keep the service-level CPU, memory, and task limits applied in NixOS.
2. Extend the rootless integration suite with cancellation and restart
   reconciliation coverage.
3. Decide whether per-session resource fairness is needed before adding a
   privileged cgroup broker.

## Running the Stage 5 integration test

`tests/test_runsc_runtime_integration.py` covers the direct-runtime lifecycle
that is safe to validate before the Stage 8 limit/security suite: it boots a
warm container, executes Python twice, confirms a workspace write survives
between execs, confirms `pip install` reports that its reserved external index
is unreachable, and confirms cleanup removes the runtime while preserving the
session directory. It is opt-in so ordinary development shells never attempt
to launch gVisor.

From the rootless non-root NixOS service or VM-test context, provide the
pinned rootfs and its recorded image identity, then run:

```sh
PYVISOR_RUNSC_INTEGRATION=1 \
PYVISOR_RUNSC_BINARY=/path/to/runsc \
PYVISOR_SANDBOX_ROOTFS=/path/to/pinned-python-rootfs \
PYVISOR_SANDBOX_IMAGE_IDENTITY='python-3.13.x@sha256:...' \
uv run pytest -m runsc_integration
```

The test refuses to run as root and skips unless all four settings are present.
It verifies offline `pip` behavior, a read-only rootfs, the writable workspace
and scratch mounts, and an empty effective capability set. CPU, memory, pids,
session-disk limits, and cancellation coverage remain later work.
