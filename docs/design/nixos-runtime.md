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

The ordinary agent launcher remains deliberately confined with
`NoNewPrivileges=1` and no effective capabilities; it cannot be used for this
integration suite.  The earlier rootless attempt from that context reached
gofer creation but was denied `EPERM`.  The NixOS test/service context must
instead provide the permissions exercised by the successful privileged probe.

The privileged context has a writable cgroup-v2 hierarchy and ran the smoke
test without `--ignore-cgroups`.  This proves cgroup setup is reachable, but
does not yet prove that Pyvisor's configured CPU, memory, disk, and pids limits
are applied correctly; those remain required integration tests.

## Required deployment conditions

1. Install and pin the `runsc` package/binary in the NixOS configuration.
   Record its version or Nix store derivation in deployment metadata and session
   traces.  The currently installed package reports `VERSION_MISSING`, so a
   derivation identity is especially important.
2. Run Pyvisor under an account and service policy that permit the selected
   `runsc` mode.  If rootless mode is used, it must permit user namespaces,
   mappings for the service UID/GID, and the gofer's required namespace/chroot
   operations.  Do not enable `TESTONLY-unsafe-nonroot`.
3. Delegate a writable cgroup-v2 subtree with CPU, memory, and pids controllers
   to the service (for example through an appropriate systemd service policy).
   The runtime backend must create each sandbox/job cgroup inside that subtree
   and must fail closed when it cannot apply configured limits.
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

The future backend begins each invocation with an explicit private runtime root,
`--platform=systrap`, and `--network=none`.  OCI configuration supplies a
read-only rootfs, drops capabilities, disables privilege escalation, and mounts
only the session workspace plus bounded scratch as writable paths.  It must not
pass `--ignore-cgroups`, choose `ptrace`, use `--TESTONLY-unsafe-nonroot`, or
silently fall back to host networking.

## Next validation sequence

1. Provision the service account and delegated cgroup subtree in NixOS.
2. Build a minimal pinned Python rootfs and OCI bundle generator.
3. Repeat the passed `runsc run` isolation smoke test using that rootfs.
4. Keep a trivial init alive; prove `runsc exec` can run separate commands
   against the same workspace.
5. Add resource, cancellation, reaper, and restart-reconciliation tests before
   accepting untrusted commands.
