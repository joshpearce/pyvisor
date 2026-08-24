"""Direct ``runsc run`` backend and restrictive OCI bundle generation.

This backend deliberately starts one disposable container per job for now.  A
warm-container/``runsc exec`` lifecycle needs a separately validated init and
reconciliation design; it must not be inferred from the local-process backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pyvisor.config import AppConfig

_OCI_VERSION = "1.0.2"
class RuntimeUnavailableError(RuntimeError):
    """The configured gVisor runtime or rootfs cannot be used safely."""


@dataclass(frozen=True, slots=True)
class OciBundle:
    """A generated OCI bundle and its service-generated runtime id."""

    container_id: str
    path: Path


def _directory(path: Path, *, label: str) -> Path:
    """Resolve an existing trusted directory without accepting a symlink."""
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeUnavailableError(f"{label} does not exist") from error
    if path.is_symlink() or not path.is_dir():
        raise RuntimeUnavailableError(f"{label} must be a non-symlink directory")
    return path.resolve(strict=True)


class OciBundleBuilder:
    """Creates disposable OCI bundles below the configured runtime root."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def bundles_root(self) -> Path:
        return self._config.runtime_root / "bundles"

    @property
    def runsc_root(self) -> Path:
        return self._config.runtime_root / "runsc"

    def _prepare_runtime_root(self) -> None:
        self._config.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_root = _directory(self._config.runtime_root, label="runtime root")
        for directory in (runtime_root / "bundles", runtime_root / "runsc"):
            directory.mkdir(mode=0o700, exist_ok=True)
            _directory(directory, label="runtime directory")

    def create(self, *, session_id: str, workspace: Path, command: str) -> OciBundle:
        """Generate a bundle whose writable host mount is only ``workspace``."""
        self._prepare_runtime_root()
        workspace = _directory(workspace, label="workspace")
        rootfs = _directory(self._config.sandbox_rootfs, label="sandbox rootfs")
        container_id = f"pv-{uuid4()}"
        bundle_path = self.bundles_root / container_id
        bundle_path.mkdir(mode=0o700)
        try:
            # An OCI root is bundle-relative.  The target is a configured,
            # service-owned rootfs rather than caller-controlled state.
            os.symlink(rootfs, bundle_path / "rootfs")
            config_path = bundle_path / "config.json"
            config_path.write_text(
                json.dumps(
                    self._config_value(session_id=session_id, workspace=workspace, command=command),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(config_path, 0o600)
        except Exception:
            self.remove(OciBundle(container_id=container_id, path=bundle_path))
            raise
        return OciBundle(container_id=container_id, path=bundle_path)

    def _config_value(self, *, session_id: str, workspace: Path, command: str) -> dict[str, object]:
        scratch_size = self._config.scratch_disk_limit_bytes
        return {
            "ociVersion": _OCI_VERSION,
            "annotations": {
                "io.pyvisor.managed": "true",
                "io.pyvisor.session_id": session_id,
                "io.pyvisor.sandbox_image": self._config.sandbox_image_identity,
            },
            "process": {
                "terminal": False,
                # The service must run as a dedicated non-root account.  Using
                # its numeric ids preserves a private, owner-writable workspace
                # mount without making it world-writable.
                "user": {"uid": os.getuid(), "gid": os.getgid()},
                "args": ["/bin/bash", "-lc", command],
                "env": [
                    "HOME=/workspace",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                    "TMPDIR=/tmp",
                ],
                "cwd": "/workspace",
                "capabilities": {
                    "bounding": [],
                    "effective": [],
                    "inheritable": [],
                    "permitted": [],
                    "ambient": [],
                },
                "noNewPrivileges": True,
                "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024}],
            },
            "root": {"path": "rootfs", "readonly": True},
            "hostname": "pyvisor",
            "mounts": [
                {
                    "destination": "/proc",
                    "type": "proc",
                    "source": "proc",
                    "options": ["nosuid", "noexec", "nodev"],
                },
                {
                    "destination": "/tmp",
                    "type": "tmpfs",
                    "source": "tmpfs",
                    "options": [
                        "nosuid",
                        "nodev",
                        "noexec",
                        "mode=1777",
                        f"size={scratch_size}",
                    ],
                },
                {
                    "destination": "/workspace",
                    "type": "bind",
                    "source": str(workspace),
                    "options": ["rbind", "rw", "nosuid", "nodev", "noexec"],
                },
            ],
            "linux": {
                "namespaces": [
                    {"type": "pid"},
                    {"type": "ipc"},
                    {"type": "uts"},
                    {"type": "mount"},
                    {"type": "network"},
                ],
            },
        }

    def remove(self, bundle: OciBundle) -> None:
        """Remove only a bundle whose generated path is under ``bundles``."""
        try:
            parent = bundle.path.parent.resolve(strict=True)
        except FileNotFoundError:
            return
        if parent != self.bundles_root.resolve(strict=True):
            raise RuntimeUnavailableError("refusing to remove a bundle outside runtime root")
        shutil.rmtree(bundle.path)


class RunscExecution:
    """A direct ``runsc run`` invocation for one OCI bundle."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        runsc_binary: Path,
        runsc_root: Path,
        bundle: OciBundle,
        builder: OciBundleBuilder,
    ) -> None:
        self._process = process
        self._runsc_binary = runsc_binary
        self._runsc_root = runsc_root
        self._bundle = bundle
        self._builder = builder
        self._cleaned = False

    async def wait(self) -> int:
        try:
            return await self._process.wait()
        finally:
            self._cleanup()

    async def terminate(self) -> int:
        """Force-delete the container, then kill the launcher process group if needed."""
        if self._process.returncode is None:
            delete = await asyncio.create_subprocess_exec(
                str(self._runsc_binary),
                "--root",
                str(self._runsc_root),
                "delete",
                "--force",
                self._bundle.container_id,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await delete.wait()
        if self._process.returncode is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            return await asyncio.wait_for(self._process.wait(), timeout=5)
        except TimeoutError:
            if self._process.returncode is None:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return await self._process.wait()
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        if not self._cleaned:
            self._builder.remove(self._bundle)
            self._cleaned = True


class RunscExecutor:
    """Execute a command with direct ``runsc run`` and generated OCI bundles."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._builder = OciBundleBuilder(config)
        self.sandbox_image = config.sandbox_image_identity

    async def start(
        self,
        command: str,
        *,
        session_id: str,
        workspace: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> RunscExecution:
        if os.geteuid() == 0:
            raise RuntimeUnavailableError("runsc backend must run as a non-root service account")
        if self.sandbox_image == "pyvisor-sandbox:unconfigured":
            raise RuntimeUnavailableError(
                "runsc backend requires an explicit sandbox image identity"
            )
        runsc_binary = self._config.runsc_binary
        if not runsc_binary.is_file() or not os.access(runsc_binary, os.X_OK):
            raise RuntimeUnavailableError("configured runsc binary is not executable")
        bundle = self._builder.create(session_id=session_id, workspace=workspace, command=command)
        try:
            with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                process = await asyncio.create_subprocess_exec(
                    str(runsc_binary),
                    "--root",
                    str(self._builder.runsc_root),
                    "--platform=systrap",
                    "--network=none",
                    "run",
                    "--bundle",
                    str(bundle.path),
                    bundle.container_id,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
        except Exception:
            self._builder.remove(bundle)
            raise
        return RunscExecution(
            process,
            runsc_binary=runsc_binary,
            runsc_root=self._builder.runsc_root,
            bundle=bundle,
            builder=self._builder,
        )
