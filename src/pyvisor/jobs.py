"""In-memory job lifecycle service backed by the Stage 2 fake executor."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pyvisor.config import AppConfig
from pyvisor.fake_executor import FakeExecutor
from pyvisor.models import JobResponse, JobStatus, OutputResponse

LOGGER = logging.getLogger("pyvisor.jobs")


class JobServiceError(Exception):
    """Base class for errors translated by the HTTP layer."""


class BusyError(JobServiceError):
    pass


class NotFoundError(JobServiceError):
    pass


class InvalidRequestError(JobServiceError):
    pass


@dataclass(slots=True)
class _Job:
    session_id: str
    job_id: str
    command: str
    status: JobStatus
    started_at: datetime
    timeout_seconds: int
    stdout: str
    stderr: str
    finished_at: datetime | None
    exit_code: int | None


class JobService:
    """Coordinates fake jobs while preserving the future service boundary."""

    def __init__(self, config: AppConfig, executor: FakeExecutor | None = None) -> None:
        self._config = config
        self._executor = executor or FakeExecutor()
        self._jobs: dict[tuple[str, str], _Job] = {}
        self._live_job_ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def _output(text: str) -> OutputResponse:
        encoded = text.encode("utf-8")
        return OutputResponse(text=text, total_bytes=len(encoded), returned_bytes=len(encoded))

    @classmethod
    def _response(cls, job: _Job) -> JobResponse:
        return JobResponse(
            session_id=job.session_id,
            job_id=job.job_id,
            status=job.status,
            command=job.command,
            started_at=job.started_at,
            finished_at=job.finished_at,
            exit_code=job.exit_code,
            stdout=cls._output(job.stdout),
            stderr=cls._output(job.stderr),
        )

    def _timeout(self, requested_timeout: int | None) -> int:
        timeout = requested_timeout or self._config.default_timeout_seconds
        if timeout > self._config.maximum_timeout_seconds:
            raise InvalidRequestError(
                f"timeout_seconds must not exceed {self._config.maximum_timeout_seconds}"
            )
        return timeout

    async def run(self, session_id: str, command: str, timeout_seconds: int | None) -> JobResponse:
        if len(command.encode("utf-8")) > self._config.command_limit_bytes:
            raise InvalidRequestError(
                f"command must not exceed {self._config.command_limit_bytes} UTF-8 bytes"
            )
        timeout = self._timeout(timeout_seconds)
        async with self._session_lock(session_id):
            if session_id in self._live_job_ids:
                raise BusyError("session already has a live job")
            execution = self._executor.start(command)
            now = datetime.now(UTC)
            job = _Job(
                session_id=session_id,
                job_id=str(uuid4()),
                command=command,
                status=execution.status,
                started_at=now,
                timeout_seconds=timeout,
                stdout=execution.stdout,
                stderr=execution.stderr,
                finished_at=None if execution.status == "running" else now,
                exit_code=execution.exit_code,
            )
            self._jobs[(session_id, job.job_id)] = job
            if job.status == "running":
                self._live_job_ids[session_id] = job.job_id
            LOGGER.info(
                "job_started",
                extra={"session_id": session_id, "job_id": job.job_id, "status": job.status},
            )
            return self._response(job)

    async def poll(self, session_id: str, job_id: str) -> JobResponse:
        job = self._jobs.get((session_id, job_id))
        if job is None:
            raise NotFoundError("job was not found in this session")
        return self._response(job)

    async def cancel(self, session_id: str, job_id: str) -> JobResponse:
        async with self._session_lock(session_id):
            job = self._jobs.get((session_id, job_id))
            if job is None:
                raise NotFoundError("job was not found in this session")
            if job.status != "running":
                return self._response(job)
            execution = self._executor.cancel(job_id)
            job.status = execution.status
            job.stdout += execution.stdout
            job.stderr += execution.stderr
            job.exit_code = execution.exit_code
            job.finished_at = datetime.now(UTC)
            self._live_job_ids.pop(session_id, None)
            LOGGER.info(
                "job_cancelled",
                extra={"session_id": session_id, "job_id": job_id, "status": job.status},
            )
            return self._response(job)
