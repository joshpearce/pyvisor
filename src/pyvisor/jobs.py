"""Job lifecycle coordination backed by durable session storage."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from pyvisor.config import AppConfig
from pyvisor.fake_executor import FakeExecutor
from pyvisor.models import JobResponse
from pyvisor.storage import JobRecord, SessionStorage, StoredJobNotFoundError

LOGGER = logging.getLogger("pyvisor.jobs")


class JobServiceError(Exception):
    """Base class for errors translated by the HTTP layer."""


class BusyError(JobServiceError):
    pass


class NotFoundError(JobServiceError):
    pass


class InvalidRequestError(JobServiceError):
    pass


class JobService:
    """Coordinates fake jobs while preserving the future service boundary."""

    def __init__(self, config: AppConfig, executor: FakeExecutor | None = None) -> None:
        self._config = config
        self._executor = executor or FakeExecutor()
        self._storage = SessionStorage(config.sessions_root, config.output_limit_bytes)
        self._live_job_ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for record in self._storage.reconcile_interrupted_jobs():
            LOGGER.warning(
                "job_reconciled",
                extra={"session_id": record.session_id, "job_id": record.job_id},
            )

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _response(self, record: JobRecord) -> JobResponse:
        stdout, stderr = self._storage.output(record)
        return JobResponse(
            session_id=record.session_id,
            job_id=record.job_id,
            status=record.status,
            command=record.command,
            started_at=record.started_at,
            finished_at=record.finished_at,
            exit_code=record.exit_code,
            stdout=stdout,
            stderr=stderr,
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
            record = JobRecord(
                session_id=session_id,
                job_id=str(uuid4()),
                command=command,
                status="running",
                started_at=datetime.now(UTC),
                timeout_seconds=timeout,
            )
            # Persist the accepted job before handing it to an executor.
            self._storage.create_job(record)
            execution = self._executor.start(command)
            self._storage.append_output(record, stdout=execution.stdout, stderr=execution.stderr)
            if execution.status != "running":
                record = replace(
                    record,
                    status=execution.status,
                    finished_at=datetime.now(UTC),
                    exit_code=execution.exit_code,
                )
                self._storage.write_job(record)
                self._storage.append_trace(record)
            if record.status == "running":
                self._live_job_ids[session_id] = record.job_id
            LOGGER.info(
                "job_started",
                extra={"session_id": session_id, "job_id": record.job_id, "status": record.status},
            )
            return self._response(record)

    async def poll(self, session_id: str, job_id: str) -> JobResponse:
        try:
            record = self._storage.load_job(session_id, job_id)
        except StoredJobNotFoundError as error:
            raise NotFoundError("job was not found in this session") from error
        return self._response(record)

    async def cancel(self, session_id: str, job_id: str) -> JobResponse:
        async with self._session_lock(session_id):
            try:
                record = self._storage.load_job(session_id, job_id)
            except StoredJobNotFoundError as error:
                raise NotFoundError("job was not found in this session") from error
            if record.status != "running":
                return self._response(record)
            execution = self._executor.cancel(job_id)
            self._storage.append_output(record, stdout=execution.stdout, stderr=execution.stderr)
            record = replace(
                record,
                status=execution.status,
                exit_code=execution.exit_code,
                finished_at=datetime.now(UTC),
                reason="cancel_requested",
            )
            self._storage.write_job(record)
            self._storage.append_trace(record)
            self._live_job_ids.pop(session_id, None)
            LOGGER.info(
                "job_cancelled",
                extra={"session_id": session_id, "job_id": job_id, "status": record.status},
            )
            return self._response(record)
