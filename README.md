# Pyvisor

Pyvisor is a durable, containerized sandbox service for AI-agent-authored
Python and bash commands. Its production execution path will use direct
`runsc run`; the local executor planned for the initial service stages is only
for lifecycle testing and is not a security boundary.

## Development

The project targets Python 3.13. Install the pinned development environment
and run the checks with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --all-groups
uv run pytest
uv run ruff check .
```

The dependency lockfile is committed so development and CI resolve the same
versions.

Pyvisor defaults to the direct gVisor backend. It requires a pinned prepared
rootfs, an explicit sandbox image identity, and the NixOS service permissions
described in `docs/design/nixos-runtime.md`; it is not runnable from an ordinary
development shell.

For lifecycle-only development, local host execution requires both
`PYVISOR_EXECUTION_BACKEND=local` and `PYVISOR_UNSAFE_LOCAL_EXECUTION=1`. It
accepts only an exact loopback listener (`127.0.0.1` or `::1`), reports an
unhealthy `/healthz`, and must never be exposed as a sandbox service.

The pinned offline Python rootfs is defined by the repository Nix flake.  See
`docs/design/sandbox-image.md` to build it, inspect its manifest, and derive
the image identity used in traces.

## Deployment image

The Nix flake also builds a loadable OCI archive for the Pyvisor service:

```sh
nix build .#pyvisor-container
```

It contains the service, direct `runsc` binary, and pinned sandbox rootfs. See
`docs/design/nixos-deployment.md` for the reviewed NixOS/Quadlet deployment
draft and the separate NAS changes that must be applied before deployment, and
`docs/deployment-notes.md` for what actually runs on the NAS today — the
contracts that break a deploy silently, the storage durability split, the
nested-`runsc` requirements, and the debugging commands that differ from the
obvious ones.

## NixOS `runsc` integration test

The direct-runtime integration test is deliberately disabled unless explicitly
enabled.  Run it only from the delegated non-root NixOS service or VM-test
context described in `docs/design/nixos-runtime.md`:

```sh
PYVISOR_RUNSC_INTEGRATION=1 \
PYVISOR_RUNSC_BINARY=/path/to/runsc \
PYVISOR_SANDBOX_ROOTFS=/path/to/pinned-python-rootfs \
PYVISOR_SANDBOX_IMAGE_IDENTITY='python-3.13.x@sha256:...' \
uv run pytest -m runsc_integration
```

The test boots a warm sandbox, runs Python twice against one workspace,
verifies the first command's file persists for the second, and verifies orderly
runtime cleanup leaves the workspace intact.  It is skipped elsewhere rather
than attempting an unsafe or rootful fallback.
