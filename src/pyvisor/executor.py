"""Execution backend protocols shared by the job service and its backends."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class JobExecution(Protocol):
    """One started command whose process group can be awaited or terminated."""

    async def wait(self) -> int: ...

    async def terminate(self) -> int: ...


class JobExecutor(Protocol):
    """Starts commands with already-created, durable output destinations."""

    sandbox_image: str

    async def start(
        self,
        command: str,
        *,
        session_id: str,
        workspace: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> JobExecution: ...

    async def reap(self, session_id: str) -> bool:
        """Destroy an idle session runtime, returning whether one was removed."""
