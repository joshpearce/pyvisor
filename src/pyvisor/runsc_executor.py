"""Direct ``runsc`` backend with per-session warm containers.

Each session gets a restrictive OCI bundle whose init only waits for signals.
Commands then run through ``runsc exec``.  This keeps the durable workspace
mounted in one sandbox between jobs without treating a local process as an
equivalent lifecycle model.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from pyvisor.config import AppConfig
from pyvisor.ids import valid_session_id

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
        path.lstat()
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

    def create(self, *, session_id: str, workspace: Path) -> OciBundle:
        """Generate a warm bundle whose writable host mount is only ``workspace``."""
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
                    self._config_value(session_id=session_id, workspace=workspace),
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

    def _config_value(self, *, session_id: str, workspace: Path) -> dict[str, object]:
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
                # Root inside the sandbox maps to the dedicated non-root
                # service account through the single-ID user namespace below.
                # This keeps the host workspace owner-writable without giving
                # the sandbox any additional host UID or GID mappings.
                "user": {"uid": 0, "gid": 0},
                # The init deliberately does no user work.  ``runsc exec``
                # supplies a separately scoped process for every job.
                "args": [
                    "/bin/sh",
                    "-c",
                    "trap 'exit 0' TERM INT; while :; do sleep 2147483647 & wait $!; done",
                ],
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
                    {"type": "user"},
                    {"type": "pid"},
                    {"type": "ipc"},
                    {"type": "uts"},
                    {"type": "mount"},
                ],
                "uidMappings": [{"containerID": 0, "hostID": os.geteuid(), "size": 1}],
                "gidMappings": [{"containerID": 0, "hostID": os.getegid(), "size": 1}],
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


@dataclass(frozen=True, slots=True)
class _WarmRuntime:
    """The disposable runtime state retained for one session."""

    session_id: str
    bundle: OciBundle


class RunscExecution:
    """One ``runsc exec`` command in a warm session runtime."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        runsc_binary: Path,
        runsc_root: Path,
        runtime: _WarmRuntime,
        pid_path: Path,
        destroy_runtime: Callable[[_WarmRuntime], Awaitable[None]],
    ) -> None:
        self._process = process
        self._runsc_binary = runsc_binary
        self._runsc_root = runsc_root
        self._runtime = runtime
        self._pid_path = pid_path
        self._destroy_runtime = destroy_runtime

    async def wait(self) -> int:
        try:
            return await self._process.wait()
        finally:
            self._pid_path.unlink(missing_ok=True)

    async def terminate(self) -> int:
        """Stop this exec process, destroying the runtime only if it is unsafe to retain."""
        if self._process.returncode is None:
            sandbox_pid = await self._sandbox_pid()
            if sandbox_pid is None:
                # Without a sandbox pid, a host-side launcher kill could leave
                # an unknown command running.  Destroy the whole runtime.
                await self._destroy_runtime(self._runtime)
                self._stop_launcher(signal.SIGTERM)
            else:
                await self._signal(sandbox_pid, "SIGTERM")
        try:
            return await asyncio.wait_for(self._process.wait(), timeout=5)
        except TimeoutError:
            if self._process.returncode is None:
                sandbox_pid = await self._sandbox_pid()
                if sandbox_pid is None:
                    await self._destroy_runtime(self._runtime)
                    self._stop_launcher(signal.SIGKILL)
                else:
                    await self._signal(sandbox_pid, "SIGKILL")
            return await self._process.wait()
        finally:
            self._pid_path.unlink(missing_ok=True)

    async def _sandbox_pid(self) -> int | None:
        """Wait briefly for ``runsc exec --pid-file`` to publish its PID."""
        for _ in range(100):
            try:
                pid = int(self._pid_path.read_text(encoding="ascii").strip())
            except FileNotFoundError:
                if self._process.returncode is not None:
                    return None
            except ValueError as error:
                raise RuntimeUnavailableError("runsc wrote an invalid exec pid") from error
            else:
                if pid > 0:
                    return pid
                raise RuntimeUnavailableError("runsc wrote an invalid exec pid")
            await asyncio.sleep(0.01)
        return None

    async def _signal(self, sandbox_pid: int, signal_name: str) -> None:
        command = await asyncio.create_subprocess_exec(
            str(self._runsc_binary),
            "--root",
            str(self._runsc_root),
            "kill",
            "--pid",
            str(sandbox_pid),
            self._runtime.bundle.container_id,
            signal_name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await command.wait() != 0:
            await self._destroy_runtime(self._runtime)
            raise RuntimeUnavailableError("runsc could not signal the exec process")

    def _stop_launcher(self, signal_value: signal.Signals) -> None:
        try:
            os.killpg(self._process.pid, signal_value)
        except ProcessLookupError:
            pass


class RunscExecutor:
    """Execute a command with direct ``runsc run`` and generated OCI bundles."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._builder = OciBundleBuilder(config)
        self.sandbox_image = config.sandbox_image_identity
        self._runtimes: dict[str, _WarmRuntime] = {}
        self._runtime_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _managed_container_id(container_id: str) -> bool:
        """Return whether an id has the exact service-generated form."""
        if not container_id.startswith("pv-"):
            return False
        try:
            parsed = UUID(container_id.removeprefix("pv-"))
        except ValueError:
            return False
        return parsed.version == 4 and container_id == f"pv-{parsed}"

    def _runtime_lock(self, session_id: str) -> asyncio.Lock:
        return self._runtime_locks.setdefault(session_id, asyncio.Lock())

    async def reconcile_orphans(self) -> int:
        """Kill surviving, verified Pyvisor containers before accepting work.

        The ``runsc --root`` directory is private to this service, but the
        runtime's container listing alone is not enough authorization to
        delete a container.  A candidate must have the service-generated id,
        point to its expected generated bundle, and carry the managed OCI
        annotations in both runtime state and ``config.json``.
        """
        runsc_binary = self._config.runsc_binary
        if not runsc_binary.is_file() or not os.access(runsc_binary, os.X_OK):
            raise RuntimeUnavailableError("configured runsc binary is not executable")
        self._builder._prepare_runtime_root()
        listed = await self._listed_container_ids()
        removed = 0
        for container_id in listed:
            if not self._managed_container_id(container_id):
                continue
            bundle = await self._verified_orphan_bundle(container_id)
            if bundle is None:
                continue
            await self._delete_orphan(bundle)
            removed += 1
        return removed

    async def _listed_container_ids(self) -> list[str]:
        command = await asyncio.create_subprocess_exec(
            str(self._config.runsc_binary),
            "--root",
            str(self._builder.runsc_root),
            "list",
            "--quiet",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await command.communicate()
        if command.returncode != 0:
            raise RuntimeUnavailableError("runsc could not enumerate existing runtimes")
        try:
            value = stdout.decode("ascii")
        except UnicodeDecodeError as error:
            raise RuntimeUnavailableError("runsc returned invalid container ids") from error
        return list(dict.fromkeys(line for line in value.splitlines() if line))

    async def _verified_orphan_bundle(self, container_id: str) -> OciBundle | None:
        """Return the generated bundle only when runtime and disk agree on it."""
        command = await asyncio.create_subprocess_exec(
            str(self._config.runsc_binary),
            "--root",
            str(self._builder.runsc_root),
            "state",
            container_id,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await command.communicate()
        if command.returncode != 0:
            raise RuntimeUnavailableError("runsc could not inspect an existing runtime")
        try:
            state = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeUnavailableError("runsc returned invalid container state") from error
        if not isinstance(state, dict) or state.get("id") != container_id:
            return None
        annotations = state.get("annotations")
        if not isinstance(annotations, dict):
            return None
        session_id = annotations.get("io.pyvisor.session_id")
        image_identity = annotations.get("io.pyvisor.sandbox_image")
        if (
            annotations.get("io.pyvisor.managed") != "true"
            or not isinstance(session_id, str)
            or not valid_session_id(session_id)
            or not isinstance(image_identity, str)
            or not image_identity
        ):
            return None

        bundle_path = self._builder.bundles_root / container_id
        try:
            bundle_path = _directory(bundle_path, label="orphan runtime bundle")
            state_bundle = Path(state["bundle"]).resolve(strict=True)
            config_path = bundle_path / "config.json"
            if config_path.is_symlink():
                return None
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, KeyError, OSError, TypeError, json.JSONDecodeError):
            return None
        if state_bundle != bundle_path or not isinstance(config, dict):
            return None
        config_annotations = config.get("annotations")
        if not isinstance(config_annotations, dict):
            return None
        expected_annotations = {
            "io.pyvisor.managed": "true",
            "io.pyvisor.session_id": session_id,
            "io.pyvisor.sandbox_image": image_identity,
        }
        if config_annotations != expected_annotations:
            return None
        return OciBundle(container_id=container_id, path=bundle_path)

    async def _delete_orphan(self, bundle: OciBundle) -> None:
        delete = await asyncio.create_subprocess_exec(
            str(self._config.runsc_binary),
            "--root",
            str(self._builder.runsc_root),
            "delete",
            "--force",
            bundle.container_id,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await delete.wait() != 0:
            raise RuntimeUnavailableError("runsc could not delete an orphaned runtime")
        self._builder.remove(bundle)

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
        runtime = await self._runtime(session_id=session_id, workspace=workspace)
        pid_path = runtime.bundle.path / f"exec-{uuid4()}.pid"
        try:
            with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                process = await asyncio.create_subprocess_exec(
                    str(runsc_binary),
                    "--root",
                    str(self._builder.runsc_root),
                    "--platform=systrap",
                    "--network=none",
                    "exec",
                    "--cwd",
                    "/workspace",
                    "--env",
                    "HOME=/workspace",
                    "--env",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                    "--env",
                    "TMPDIR=/tmp",
                    "--user",
                    "0:0",
                    "--pid-file",
                    str(pid_path),
                    runtime.bundle.container_id,
                    "/bin/bash",
                    "-lc",
                    command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
        except Exception:
            pid_path.unlink(missing_ok=True)
            raise
        return RunscExecution(
            process,
            runsc_binary=runsc_binary,
            runsc_root=self._builder.runsc_root,
            runtime=runtime,
            pid_path=pid_path,
            destroy_runtime=self._destroy_runtime,
        )

    async def _runtime(self, *, session_id: str, workspace: Path) -> _WarmRuntime:
        """Return a live warm runtime, booting it once for this session."""
        async with self._runtime_lock(session_id):
            existing = self._runtimes.get(session_id)
            if existing is not None:
                return existing
            bundle = self._builder.create(session_id=session_id, workspace=workspace)
            try:
                launcher_stderr = bundle.path / "runsc-launch.stderr"
                with launcher_stderr.open("wb") as stderr:
                    launcher = await asyncio.create_subprocess_exec(
                        str(self._config.runsc_binary),
                        "--root",
                        str(self._builder.runsc_root),
                        "--ignore-cgroups",
                        "--platform=systrap",
                        "--network=none",
                        "run",
                        "--detach",
                        "--bundle",
                        str(bundle.path),
                        bundle.container_id,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=stderr,
                        start_new_session=True,
                    )
                    if await launcher.wait() != 0:
                        detail = launcher_stderr.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                        message = "runsc could not start the warm session runtime"
                        if detail:
                            message = f"{message}: {detail}"
                        raise RuntimeUnavailableError(message)
            except Exception:
                self._builder.remove(bundle)
                raise
            runtime = _WarmRuntime(session_id=session_id, bundle=bundle)
            self._runtimes[session_id] = runtime
            return runtime

    async def _destroy_runtime(self, runtime: _WarmRuntime) -> None:
        """Delete a known warm runtime and its disposable bundle exactly once."""
        async with self._runtime_lock(runtime.session_id):
            await self._destroy_runtime_locked(runtime)

    async def _destroy_runtime_locked(self, runtime: _WarmRuntime) -> bool:
        """Delete a runtime while its session-specific runtime lock is held."""
        if self._runtimes.get(runtime.session_id) != runtime:
            return False
        delete = await asyncio.create_subprocess_exec(
            str(self._config.runsc_binary),
            "--root",
            str(self._builder.runsc_root),
            "delete",
            "--force",
            runtime.bundle.container_id,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await delete.wait() != 0:
            raise RuntimeUnavailableError("runsc could not delete the warm session runtime")
        self._runtimes.pop(runtime.session_id, None)
        self._builder.remove(runtime.bundle)
        return True

    async def reap(self, session_id: str) -> bool:
        """Destroy the warm runtime for an idle session, if it still exists."""
        async with self._runtime_lock(session_id):
            runtime = self._runtimes.get(session_id)
            if runtime is None:
                return False
            return await self._destroy_runtime_locked(runtime)

    async def aclose(self) -> None:
        """Destroy all warm runtimes during an orderly service shutdown."""
        for runtime in list(self._runtimes.values()):
            await self._destroy_runtime(runtime)
