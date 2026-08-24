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
versions. Runtime-dependent gVisor tests will be added separately and run on a
NixOS host.

Run the local lifecycle service with `uv run pyvisor`. It executes submitted
commands as ordinary local processes, so it is useful for development only and
must never be exposed as a sandbox service.

The direct gVisor backend is opt-in with `PYVISOR_EXECUTION_BACKEND=runsc`.
It requires a pinned prepared rootfs, an explicit sandbox image identity, and
the NixOS service permissions described in `docs/design/nixos-runtime.md`; it
is not runnable from an ordinary development shell.
