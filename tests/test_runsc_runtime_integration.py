"""Integration coverage for a delegated NixOS direct-``runsc`` environment.

This suite is intentionally opt-in.  It must run under the non-root service
account that owns a delegated cgroup-v2 subtree, never under a development
shell where a fallback could weaken the runtime model being tested.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pyvisor.config import AppConfig
from pyvisor.runsc_executor import RunscExecutor

pytestmark = pytest.mark.runsc_integration


def _integration_config(tmp_path: Path) -> AppConfig:
    """Build test-only paths after confirming the real runtime is explicitly enabled."""
    if os.getenv("PYVISOR_RUNSC_INTEGRATION") != "1":
        pytest.skip(
            "requires PYVISOR_RUNSC_INTEGRATION=1 in a delegated non-root NixOS runsc context"
        )
    if os.geteuid() == 0:
        pytest.skip("the runsc backend deliberately refuses to run as root")

    required = (
        "PYVISOR_RUNSC_BINARY",
        "PYVISOR_SANDBOX_ROOTFS",
        "PYVISOR_SANDBOX_IMAGE_IDENTITY",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing required runsc integration settings: {', '.join(missing)}")

    runsc_binary = Path(os.environ["PYVISOR_RUNSC_BINARY"])
    rootfs = Path(os.environ["PYVISOR_SANDBOX_ROOTFS"])
    if not runsc_binary.is_file() or not os.access(runsc_binary, os.X_OK):
        pytest.skip("PYVISOR_RUNSC_BINARY must name an executable runsc binary")
    if rootfs.is_symlink() or not rootfs.is_dir():
        pytest.skip("PYVISOR_SANDBOX_ROOTFS must name a non-symlink prepared rootfs directory")
    if not any(
        (rootfs / candidate).is_file()
        for candidate in ("bin/python3", "usr/bin/python3", "usr/local/bin/python3")
    ):
        pytest.skip("the prepared sandbox rootfs must provide python3")
    if not (rootfs / "bin/bash").is_file():
        pytest.skip("the prepared sandbox rootfs must provide /bin/bash")

    return AppConfig(
        sessions_root=tmp_path / "sessions",
        runtime_root=tmp_path / "runtime",
        sandbox_rootfs=rootfs,
        sandbox_image_identity=os.environ["PYVISOR_SANDBOX_IMAGE_IDENTITY"],
        runsc_binary=runsc_binary,
    )


async def _listed_container_ids(config: AppConfig) -> list[str]:
    """List only runtimes under this test's fresh, private runtime root."""
    process = await asyncio.create_subprocess_exec(
        str(config.runsc_binary),
        "--root",
        str(config.runtime_root / "runsc"),
        "list",
        "--quiet",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout.decode("ascii").splitlines()


@pytest.mark.asyncio
async def test_warm_runsc_runtime_persists_workspace_then_reaps(tmp_path: Path) -> None:
    """A disposable runtime preserves the durable workspace through multiple execs."""
    config = _integration_config(tmp_path)
    session_id = "runsc_integration_01"
    workspace = config.sessions_root / session_id / "workspace"
    jobs = config.sessions_root / session_id / "jobs"
    workspace.mkdir(parents=True)
    jobs.mkdir()
    executor = RunscExecutor(config)

    first_stdout = jobs / "first.stdout"
    first_stderr = jobs / "first.stderr"
    second_stdout = jobs / "second.stdout"
    second_stderr = jobs / "second.stderr"

    try:
        first = await executor.start(
            (
                "python3 -c 'from pathlib import Path; "
                "Path(\"value.txt\").write_text(\"persisted\\n\")'"
            ),
            session_id=session_id,
            workspace=workspace,
            stdout_path=first_stdout,
            stderr_path=first_stderr,
        )
        assert await first.wait() == 0
        runtime_ids_after_first = await _listed_container_ids(config)
        assert len(runtime_ids_after_first) == 1

        second = await executor.start(
            (
                "python3 -c 'from pathlib import Path; "
                "print(Path(\"value.txt\").read_text(), end=\"\")'"
            ),
            session_id=session_id,
            workspace=workspace,
            stdout_path=second_stdout,
            stderr_path=second_stderr,
        )
        assert await second.wait() == 0
        assert await _listed_container_ids(config) == runtime_ids_after_first
        assert workspace.joinpath("value.txt").read_text() == "persisted\n"
        assert second_stdout.read_text() == "persisted\n"
        assert first_stderr.read_bytes() == b""
        assert second_stderr.read_bytes() == b""
    finally:
        await executor.aclose()

    assert await _listed_container_ids(config) == []
    assert workspace.joinpath("value.txt").read_text() == "persisted\n"
    assert list((config.runtime_root / "bundles").iterdir()) == []


@pytest.mark.asyncio
async def test_runsc_runtime_pip_install_fails_with_an_offline_diagnostic(tmp_path: Path) -> None:
    """The pinned image cannot use pip to reach an external package index."""
    config = _integration_config(tmp_path)
    session_id = "runsc_pip_offline_01"
    workspace = config.sessions_root / session_id / "workspace"
    jobs = config.sessions_root / session_id / "jobs"
    workspace.mkdir(parents=True)
    jobs.mkdir()
    executor = RunscExecutor(config)
    stdout_path = jobs / "pip.stdout"
    stderr_path = jobs / "pip.stderr"

    try:
        execution = await executor.start(
            (
                "python3 -m pip install --disable-pip-version-check --no-input "
                "--no-cache-dir --retries 0 --timeout 5 "
                "--index-url http://198.51.100.1/simple "
                "--trusted-host 198.51.100.1 "
                "--target /workspace/pip-network-probe "
                "pyvisor-network-probe==0.0.1"
            ),
            session_id=session_id,
            workspace=workspace,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        assert await execution.wait() != 0
    finally:
        await executor.aclose()

    output = (stdout_path.read_bytes() + stderr_path.read_bytes()).decode(
        "utf-8", errors="replace"
    ).lower()
    assert any(
        diagnostic in output
        for diagnostic in (
            "network is unreachable",
            "failed to establish a new connection",
            "connection error",
            "could not fetch url",
        )
    ), output
