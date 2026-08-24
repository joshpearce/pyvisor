# Sandbox Image

The sandbox rootfs is a Nix build artifact, not a mutable container image.  It
is produced by the `sandbox-rootfs` package in the repository flake and used
directly as the `rootfs` directory in each generated OCI bundle.  This matches
the direct-`runsc` design: Docker and Podman are neither required nor invoked
when a command runs.

## Contents and reproducibility

`flake.lock` pins the exact nixpkgs revision.  That fixes CPython 3.13 and all
Python, native-library, and shell-utility versions in the rootfs.  The chosen
stack is deliberately offline-complete for the MVP:

- CPython 3.13 with `pip`;
- `numpy`, `pandas`, `scipy`, `scikit-learn`, `matplotlib`, `sympy`,
  `networkx`, and `statsmodels`;
- `bash`, GNU core utilities, `diffutils`, `file`, `findutils`, `gawk`,
  `grep`, `sed`, `less`, and `which`.

The build copies the complete Nix closure below `/nix/store` in the rootfs.
This is necessary because Nix-built executables and their dynamic loader refer
to absolute store paths.  The rootfs also exposes `/bin/bash`, `/bin/sh`, and
`/bin/python3`; `/usr/bin` and `/usr/local/bin/python3` are compatibility
links.  `/workspace` and `/tmp` are only mount targets.  At runtime, the OCI
configuration makes the rootfs read-only, replaces `/tmp` with bounded tmpfs,
and mounts the session workspace at `/workspace`.

The rootfs includes no package manager other than the pinned `pip` module.
`pip install` cannot reach an index because `runsc` is always invoked with
`--network=none`; the expected result is a clear network/connectivity error,
not an attempt to make the base filesystem writable.

## Build and identify the rootfs

Build from a Linux Nix environment with flakes enabled:

```sh
rootfs="$(nix build --no-link --print-out-paths .#sandbox-rootfs)"
manifest="$rootfs/etc/pyvisor-sandbox-image.json"
nix path-info --json --json-format 1 "$rootfs"
cat "$manifest"
```

Use the rootfs output path directly; do not place a mutable or symlinked
directory in `PYVISOR_SANDBOX_ROOTFS`.  The `narHash` in `nix path-info` is the
image identity recorded in traces.  For example, after inspecting that output:

```sh
export PYVISOR_SANDBOX_ROOTFS=/nix/store/...-pyvisor-sandbox-rootfs-3.13.x
export PYVISOR_SANDBOX_IMAGE_IDENTITY='pyvisor-sandbox-python-3.13@sha256:...'
```

The manifest reports the resolved Python and package versions.  Keep the
identity's hash and the manifest together in deployment metadata, so an
archived trace can be associated with the precise artifact that executed it.

## Validation

The host-gated runtime test must use this built rootfs.  It verifies Python,
the warm `runsc exec` lifecycle, persistent workspace contents, and runtime
cleanup.  Run it only from the delegated non-root NixOS service or VM-test
context described in `nixos-runtime.md`:

```sh
PYVISOR_RUNSC_INTEGRATION=1 \
PYVISOR_RUNSC_BINARY=/path/to/runsc \
PYVISOR_SANDBOX_ROOTFS="$rootfs" \
PYVISOR_SANDBOX_IMAGE_IDENTITY='pyvisor-sandbox-python-3.13@sha256:...' \
uv run pytest -m runsc_integration
```

The host-gated runtime suite also makes `pip install` target a reserved external
address and requires a clear offline-network diagnostic. Stage 8 must still
prove read-only rootfs, writable workspace and scratch mounts, and resource
limits on the deployment host.
