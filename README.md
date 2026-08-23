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

Run the Stage 2 local service with `uv run pyvisor`. Its in-memory executor is
only an API/lifecycle test double: it does **not** run submitted commands and
must never be exposed as a sandbox service.
