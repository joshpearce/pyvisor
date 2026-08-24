"""Non-sandboxed local process backend used to validate job lifecycle semantics."""

from __future__ import annotations

import asyncio
import os
import signal
import shutil
from pathlib import Path


class LocalProcessExecution:
    """A command running in its own process group."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def wait(self) -> int:
        return await self._process.wait()

    async def terminate(self) -> int:
        """Terminate the complete process group, escalating if it does not exit."""
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


class LocalProcessExecutor:
    """Launch ordinary local commands. This is explicitly not a sandbox."""

    sandbox_image = "local-process"

    def __init__(self) -> None:
        # Deployment hosts use /bin/bash. Nix development shells often expose
        # bash only through PATH, while retaining the same shell semantics.
        self._bash = "/bin/bash" if Path("/bin/bash").is_file() else shutil.which("bash")
        if self._bash is None:
            raise RuntimeError("bash is required for local command execution")

    async def start(
        self,
        command: str,
        *,
        session_id: str,
        workspace: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> LocalProcessExecution:
        del session_id
        # ``start_new_session`` gives the shell and all descendants an isolated
        # process group, so timeout and cancellation do not leave children behind.
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = await asyncio.create_subprocess_exec(
                self._bash,
                "-lc",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=workspace,
                start_new_session=True,
            )
        return LocalProcessExecution(process)
