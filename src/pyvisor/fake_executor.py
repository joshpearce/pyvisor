"""Deliberately non-secure in-memory executor for Stage 2 API testing."""

from __future__ import annotations

from dataclasses import dataclass
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

    def start(self, command: str) -> FakeExecution:
        return self._planner(command)

    def cancel(self, job_id: str) -> FakeExecution:
        del job_id
        return FakeExecution(status="cancelled", exit_code=None)
