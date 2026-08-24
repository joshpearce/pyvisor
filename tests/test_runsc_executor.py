from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pyvisor.config import AppConfig
from pyvisor.runsc_executor import (
    OciBundle,
    OciBundleBuilder,
    RunscExecutor,
    RuntimeUnavailableError,
)


def test_oci_bundle_has_fixed_restrictive_sandbox_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "sessions" / "chat_01" / "workspace"
    workspace.mkdir(parents=True)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    config = AppConfig(
        sessions_root=tmp_path / "sessions",
        runtime_root=tmp_path / "runtime",
        sandbox_rootfs=rootfs,
        sandbox_image_identity="python-3.13.5@sha256:example",
        scratch_disk_limit_bytes=123_456,
    )

    bundle = OciBundleBuilder(config).create(
        session_id="chat_01", workspace=workspace, command="python -c 'print(2 + 2)'"
    )

    value = json.loads((bundle.path / "config.json").read_text())
    assert bundle.container_id.startswith("pv-")
    assert (bundle.path / "rootfs").resolve() == rootfs.resolve()
    assert value["annotations"] == {
        "io.pyvisor.managed": "true",
        "io.pyvisor.sandbox_image": "python-3.13.5@sha256:example",
        "io.pyvisor.session_id": "chat_01",
    }
    assert value["root"] == {"path": "rootfs", "readonly": True}
    assert value["process"]["user"] == {"uid": os.getuid(), "gid": os.getgid()}
    assert value["process"]["args"] == ["/bin/bash", "-lc", "python -c 'print(2 + 2)'"]
    assert value["process"]["capabilities"] == {
        "ambient": [],
        "bounding": [],
        "effective": [],
        "inheritable": [],
        "permitted": [],
    }
    assert value["process"]["noNewPrivileges"] is True
    assert {entry["type"] for entry in value["linux"]["namespaces"]} == {
        "ipc",
        "mount",
        "network",
        "pid",
        "uts",
    }
    mounts = {entry["destination"]: entry for entry in value["mounts"]}
    assert mounts["/workspace"] == {
        "destination": "/workspace",
        "type": "bind",
        "source": str(workspace.resolve()),
        "options": ["rbind", "rw", "nosuid", "nodev", "noexec"],
    }
    assert "size=123456" in mounts["/tmp"]["options"]


def test_oci_bundle_cleanup_refuses_paths_outside_generated_root(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    config = AppConfig(
        sessions_root=tmp_path / "sessions",
        runtime_root=tmp_path / "runtime",
        sandbox_rootfs=rootfs,
    )
    builder = OciBundleBuilder(config)
    workspace = tmp_path / "sessions" / "chat_01" / "workspace"
    workspace.mkdir(parents=True)
    bundle = builder.create(session_id="chat_01", workspace=workspace, command="true")

    builder.remove(bundle)

    assert not bundle.path.exists()
    outside_bundle = tmp_path / "outside-bundle"
    outside_bundle.mkdir()
    with pytest.raises(RuntimeUnavailableError, match="outside runtime root"):
        builder.remove(OciBundle(container_id="pv-test", path=outside_bundle))
    assert outside_bundle.exists()


@pytest.mark.asyncio
async def test_runsc_executor_uses_direct_systrap_no_network_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    workspace = tmp_path / "sessions" / "chat_01" / "workspace"
    workspace.mkdir(parents=True)
    runsc = tmp_path / "runsc"
    runsc.write_text("#!/bin/sh\n")
    runsc.chmod(0o700)
    config = AppConfig(
        sessions_root=tmp_path / "sessions",
        runtime_root=tmp_path / "runtime",
        sandbox_rootfs=rootfs,
        sandbox_image_identity="python-3.13.5@sha256:example",
        runsc_binary=runsc,
    )
    calls: list[tuple[object, ...]] = []

    class Process:
        pid = 1
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def create_subprocess_exec(*args: object, **kwargs: object) -> Process:
        calls.append(args)
        assert kwargs["stdin"] is not None
        assert kwargs["start_new_session"] is True
        return Process()

    monkeypatch.setattr(
        "pyvisor.runsc_executor.asyncio.create_subprocess_exec", create_subprocess_exec
    )
    execution = await RunscExecutor(config).start(
        "true",
        session_id="chat_01",
        workspace=workspace,
        stdout_path=tmp_path / "stdout.bin",
        stderr_path=tmp_path / "stderr.bin",
    )

    assert calls[0][0:7] == (
        str(runsc),
        "--root",
        str(tmp_path / "runtime" / "runsc"),
        "--platform=systrap",
        "--network=none",
        "run",
        "--bundle",
    )
    await execution.wait()
