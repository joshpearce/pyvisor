"""Deliberately non-secure in-memory executor for Stage 2 API testing."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pyvisor.models import JobStatus


@dataclass(frozen=True, slots=True)
class FakeExecution:
    """A predetermined execution result returned by ``FakeExecutor``."""

    status: JobStatus = "completed"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0

    def __post_init__(self) -> None:
        if self.status == "running" and self.exit_code is not None:
            raise ValueError("a running fake execution cannot have an exit code")


class ExecutionPlanner(Protocol):
    def __call__(self, command: str) -> FakeExecution: ...


def _complete_successfully(command: str) -> FakeExecution:
    del command
    return FakeExecution()


class FakeExecutor:
    """Returns planned states; it never invokes a shell or subprocess.

    This backend exists only until the local process manager is introduced in
    Stage 4. It must never be used as a sandbox or advertised as one.
    """

    def __init__(self, planner: ExecutionPlanner = _complete_successfully) -> None:
        self._planner = planner

    sandbox_image = "fake-executor"

    async def start(
        self,
        command: str,
        *,
        workspace: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> _FakeProcessExecution:
        del workspace
        execution = self._planner(command)
        if execution.stdout:
            with stdout_path.open("ab") as stdout:
                stdout.write(execution.stdout.encode())
        if execution.stderr:
            with stderr_path.open("ab") as stderr:
                stderr.write(execution.stderr.encode())
        return _FakeProcessExecution(execution)


class _FakeProcessExecution:
    """Asynchronous adapter that keeps the fake backend usable in API tests."""

    def __init__(self, execution: FakeExecution) -> None:
        self._execution = execution
        self._finished = asyncio.Event()
        self._exit_code = execution.exit_code
        if execution.status != "running":
            self._finished.set()

    async def wait(self) -> int:
        await self._finished.wait()
        return self._exit_code if self._exit_code is not None else -signal.SIGTERM

    async def terminate(self) -> int:
        self._exit_code = -signal.SIGTERM
        self._finished.set()
        return self._exit_code
