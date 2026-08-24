from __future__ import annotations

import asyncio
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


def test_runsc_root_check_accepts_rootless_container_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid_map = tmp_path / "uid_map"
    uid_map.write_text("0 1219 1\n1 363144 65536\n")
    monkeypatch.setattr("pyvisor.runsc_executor.os.geteuid", lambda: 0)

    assert not RunscExecutor.runs_as_host_root(uid_map)


def test_runsc_root_check_rejects_host_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uid_map = tmp_path / "uid_map"
    uid_map.write_text("0 0 4294967295\n")
    monkeypatch.setattr("pyvisor.runsc_executor.os.geteuid", lambda: 0)

    assert RunscExecutor.runs_as_host_root(uid_map)


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

    bundle = OciBundleBuilder(config).create(session_id="chat_01", workspace=workspace)

    value = json.loads((bundle.path / "config.json").read_text())
    assert bundle.container_id.startswith("pv-")
    assert (bundle.path / "rootfs").resolve() == rootfs.resolve()
    assert value["annotations"] == {
        "io.pyvisor.managed": "true",
        "io.pyvisor.sandbox_image": "python-3.13.5@sha256:example",
        "io.pyvisor.session_id": "chat_01",
    }
    assert value["root"] == {"path": "rootfs", "readonly": True}
    assert value["process"]["user"] == {"uid": 0, "gid": 0}
    assert value["process"]["args"] == [
        "/bin/sh",
        "-c",
        "trap 'exit 0' TERM INT; while :; do sleep 2147483647 & wait $!; done",
    ]
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
        "pid",
        "user",
        "uts",
    }
    assert value["linux"]["uidMappings"] == [
        {"containerID": 0, "hostID": os.geteuid(), "size": 1}
    ]
    assert value["linux"]["gidMappings"] == [
        {"containerID": 0, "hostID": os.getegid(), "size": 1}
    ]
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
    bundle = builder.create(session_id="chat_01", workspace=workspace)

    builder.remove(bundle)

    assert not bundle.path.exists()
    outside_bundle = tmp_path / "outside-bundle"
    outside_bundle.mkdir()
    with pytest.raises(RuntimeUnavailableError, match="outside runtime root"):
        builder.remove(OciBundle(container_id="pv-test", path=outside_bundle))
    assert outside_bundle.exists()


@pytest.mark.asyncio
async def test_runsc_startup_reaps_only_a_verified_managed_orphan(
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
    builder = OciBundleBuilder(config)
    bundle = builder.create(session_id="chat_01", workspace=workspace)
    foreign_id = "foreign-runtime"
    calls: list[tuple[object, ...]] = []

    class Process:
        def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode

        async def wait(self) -> int:
            return self.returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.stdout, b""

    async def create_subprocess_exec(*args: object, **_: object) -> Process:
        calls.append(args)
        if "list" in args:
            return Process(f"{bundle.container_id}\n{foreign_id}\n".encode())
        if "state" in args:
            return Process(
                json.dumps(
                    {
                        "id": bundle.container_id,
                        "bundle": str(bundle.path.resolve()),
                        "annotations": {
                            "io.pyvisor.managed": "true",
                            "io.pyvisor.session_id": "chat_01",
                            "io.pyvisor.sandbox_image": "python-3.13.5@sha256:example",
                        },
                    }
                ).encode()
            )
        return Process()

    monkeypatch.setattr(
        "pyvisor.runsc_executor.asyncio.create_subprocess_exec", create_subprocess_exec
    )

    assert await RunscExecutor(config).reconcile_orphans() == 1
    assert not bundle.path.exists()
    assert [call[3] for call in calls] == ["list", "state", "delete"]
    delete_call = next(call for call in calls if "delete" in call)
    assert delete_call[delete_call.index("delete") :] == (
        "delete",
        "--force",
        bundle.container_id,
    )


@pytest.mark.asyncio
async def test_runsc_startup_leaves_an_unverified_container_untouched(
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
    bundle = OciBundleBuilder(config).create(session_id="chat_01", workspace=workspace)
    calls: list[tuple[object, ...]] = []

    class Process:
        returncode = 0

        def __init__(self, stdout: bytes = b"") -> None:
            self.stdout = stdout

        async def wait(self) -> int:
            return self.returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.stdout, b""

    async def create_subprocess_exec(*args: object, **_: object) -> Process:
        calls.append(args)
        if "list" in args:
            return Process(f"{bundle.container_id}\n".encode())
        return Process(
            json.dumps(
                {
                    "id": bundle.container_id,
                    "bundle": str(bundle.path.resolve()),
                    "annotations": {
                        "io.pyvisor.managed": "false",
                        "io.pyvisor.session_id": "chat_01",
                        "io.pyvisor.sandbox_image": "python-3.13.5@sha256:example",
                    },
                }
            ).encode()
        )

    monkeypatch.setattr(
        "pyvisor.runsc_executor.asyncio.create_subprocess_exec", create_subprocess_exec
    )

    assert await RunscExecutor(config).reconcile_orphans() == 0
    assert bundle.path.exists()
    assert not any("delete" in call for call in calls)


@pytest.mark.asyncio
async def test_runsc_executor_boots_a_warm_runtime_then_executes_a_command(
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

    assert calls[0][0:8] == (
        str(runsc),
        "--root",
        str(tmp_path / "runtime" / "runsc"),
        "--ignore-cgroups",
        "--platform=systrap",
        "--network=none",
        "run",
        "--detach",
    )
    assert calls[1][0:6] == (
        str(runsc),
        "--root",
        str(tmp_path / "runtime" / "runsc"),
        "--platform=systrap",
        "--network=none",
        "exec",
    )
    assert calls[1][-3:] == ("/bin/bash", "-lc", "true")
    await execution.wait()


@pytest.mark.asyncio
async def test_runsc_executor_reuses_the_session_runtime_for_later_jobs(
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

    async def create_subprocess_exec(*args: object, **_: object) -> Process:
        calls.append(args)
        return Process()

    monkeypatch.setattr(
        "pyvisor.runsc_executor.asyncio.create_subprocess_exec", create_subprocess_exec
    )
    executor = RunscExecutor(config)
    first = await executor.start(
        "printf first",
        session_id="chat_01",
        workspace=workspace,
        stdout_path=tmp_path / "first.stdout",
        stderr_path=tmp_path / "first.stderr",
    )
    second = await executor.start(
        "printf second",
        session_id="chat_01",
        workspace=workspace,
        stdout_path=tmp_path / "second.stdout",
        stderr_path=tmp_path / "second.stderr",
    )

    assert calls[0][3:7] == (
        "--ignore-cgroups",
        "--platform=systrap",
        "--network=none",
        "run",
    )
    assert [call[5] for call in calls[1:]] == ["exec", "exec"]
    container_id = calls[0][-1]
    assert container_id in calls[1]
    assert container_id in calls[2]
    await first.wait()
    await second.wait()
    assert await executor.reap("chat_01") is True
    assert calls[-1][3:] == ("delete", "--force", container_id)
    assert await executor.reap("chat_01") is False


@pytest.mark.asyncio
async def test_runsc_exec_cancellation_signals_only_the_exec_pid(
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

        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            if self.returncode is None:
                await self.finished.wait()
            assert self.returncode is not None
            return self.returncode

    exec_process = Process(None)

    async def create_subprocess_exec(*args: object, **_: object) -> Process:
        calls.append(args)
        if "exec" in args:
            pid_path = Path(str(args[args.index("--pid-file") + 1]))
            pid_path.write_text("42\n")
            return exec_process
        if "kill" in args:
            exec_process.returncode = 143
            exec_process.finished.set()
        return Process(0)

    monkeypatch.setattr(
        "pyvisor.runsc_executor.asyncio.create_subprocess_exec", create_subprocess_exec
    )
    executor = RunscExecutor(config)
    execution = await executor.start(
        "sleep 10",
        session_id="chat_01",
        workspace=workspace,
        stdout_path=tmp_path / "stdout.bin",
        stderr_path=tmp_path / "stderr.bin",
    )

    assert await execution.terminate() == 143
    kill_call = next(call for call in calls if "kill" in call)
    assert kill_call[kill_call.index("kill") :] == (
        "kill",
        "--pid",
        "42",
        calls[0][-1],
        "SIGTERM",
    )
    assert not any("delete" in call for call in calls)
    await executor.aclose()
