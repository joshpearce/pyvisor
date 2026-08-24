"""Job lifecycle coordination backed by durable session storage."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from pyvisor.config import AppConfig
from pyvisor.executor import JobExecution, JobExecutor
from pyvisor.local_executor import LocalProcessExecutor
from pyvisor.models import JobResponse, JobStatus
from pyvisor.runsc_executor import RunscExecutor
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


@dataclass(slots=True)
class _LiveJob:
    record: JobRecord
    job_id: str
    execution: JobExecution
    watcher: asyncio.Task[None]


class JobService:
    """Coordinates durable jobs with an interchangeable execution backend."""

    def __init__(self, config: AppConfig, executor: JobExecutor | None = None) -> None:
        self._config = config
        self._executor = executor or self._default_executor(config)
        self._storage = SessionStorage(config.sessions_root, config.output_limit_bytes)
        self._live_jobs: dict[str, _LiveJob] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def startup(self) -> None:
        """Reconcile disposable executor state before the API accepts traffic."""
        reconcile_orphans = getattr(self._executor, "reconcile_orphans", None)
        if reconcile_orphans is not None:
            removed = await reconcile_orphans()
            LOGGER.info("runtime_orphans_reconciled", extra={"count": removed})
        for record in self._storage.reconcile_interrupted_jobs():
            LOGGER.warning(
                "job_reconciled",
                extra={"session_id": record.session_id, "job_id": record.job_id},
            )

    @staticmethod
    def _default_executor(config: AppConfig) -> JobExecutor:
        if config.execution_backend == "runsc":
            return RunscExecutor(config)
        return LocalProcessExecutor()

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
        timeout = (
            self._config.default_timeout_seconds
            if requested_timeout is None
            else requested_timeout
        )
        if timeout > self._config.maximum_timeout_seconds:
            raise InvalidRequestError(
                f"timeout_seconds must not exceed {self._config.maximum_timeout_seconds}"
            )
        return timeout

    async def _terminalize(
        self,
        record: JobRecord,
        *,
        status: JobStatus,
        exit_code: int | None = None,
        reason: str | None = None,
    ) -> JobRecord:
        """Durably apply the first terminal transition and release the live slot."""
        async with self._session_lock(record.session_id):
            current = self._storage.load_job(record.session_id, record.job_id)
            if current.terminal:
                return current
            terminal = replace(
                current,
                status=status,
                exit_code=exit_code,
                finished_at=datetime.now(UTC),
                reason=reason,
            )
            self._storage.write_job(terminal)
            self._storage.append_trace(terminal)
            live = self._live_jobs.get(record.session_id)
            if live is not None and live.job_id == record.job_id:
                self._live_jobs.pop(record.session_id, None)
            LOGGER.info(
                "job_terminal",
                extra={
                    "session_id": record.session_id,
                    "job_id": record.job_id,
                    "status": status,
                    "reason": reason,
                },
            )
            return terminal

    async def _watch(self, record: JobRecord, execution: JobExecution) -> None:
        try:
            exit_code = await asyncio.wait_for(
                asyncio.shield(execution.wait()), timeout=record.timeout_seconds
            )
        except TimeoutError:
            try:
                exit_code = await execution.terminate()
            except Exception:
                LOGGER.exception(
                    "job_timeout_termination_failed",
                    extra={"session_id": record.session_id, "job_id": record.job_id},
                )
                await self._terminalize(
                    record, status="runtime_error", reason="timeout_termination_failed"
                )
            else:
                await self._terminalize(
                    record, status="timed_out", exit_code=exit_code, reason="timeout"
                )
        except Exception:
            LOGGER.exception(
                "job_wait_failed", extra={"session_id": record.session_id, "job_id": record.job_id}
            )
            await self._terminalize(record, status="runtime_error", reason="executor_wait_failed")
        else:
            await self._terminalize(record, status="completed", exit_code=exit_code)

    async def run(self, session_id: str, command: str, timeout_seconds: int | None) -> JobResponse:
        if len(command.encode("utf-8")) > self._config.command_limit_bytes:
            raise InvalidRequestError(
                f"command must not exceed {self._config.command_limit_bytes} UTF-8 bytes"
            )
        timeout = self._timeout(timeout_seconds)
        async with self._session_lock(session_id):
            if session_id in self._live_jobs:
                raise BusyError("session already has a live job")
            record = JobRecord(
                session_id=session_id,
                job_id=str(uuid4()),
                command=command,
                status="running",
                started_at=datetime.now(UTC),
                timeout_seconds=timeout,
                sandbox_image=self._executor.sandbox_image,
            )
            # Persist acceptance and create the output files before executing any command.
            self._storage.create_job(record)
            workspace, stdout_path, stderr_path = self._storage.execution_paths(record)
            try:
                execution = await self._executor.start(
                    command,
                    session_id=session_id,
                    workspace=workspace,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            except Exception:
                LOGGER.exception(
                    "job_start_failed", extra={"session_id": session_id, "job_id": record.job_id}
                )
                record = replace(
                    record,
                    status="runtime_error",
                    finished_at=datetime.now(UTC),
                    reason="executor_start_failed",
                )
                self._storage.write_job(record)
                self._storage.append_trace(record)
                return self._response(record)
            watcher = asyncio.create_task(self._watch(record, execution))
            self._live_jobs[session_id] = _LiveJob(record, record.job_id, execution, watcher)
            LOGGER.info(
                "job_started",
                extra={"session_id": session_id, "job_id": record.job_id, "status": "running"},
            )

        # Release the launch lock while the watcher applies a terminal transition.
        try:
            await asyncio.wait_for(
                asyncio.shield(watcher), timeout=self._config.foreground_soft_cap_seconds
            )
        except TimeoutError:
            pass
        return self._response(self._storage.load_job(session_id, record.job_id))

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
            live = self._live_jobs.get(session_id)
            if live is None or live.job_id != job_id:
                # A durable running record with no trusted executor is not cancellable.
                terminal = replace(
                    record,
                    status="runtime_error",
                    finished_at=datetime.now(UTC),
                    reason="missing_live_executor",
                )
                self._storage.write_job(terminal)
                self._storage.append_trace(terminal)
                return self._response(terminal)
            try:
                exit_code = await live.execution.terminate()
            except Exception:
                LOGGER.exception(
                    "job_cancel_failed", extra={"session_id": session_id, "job_id": job_id}
                )
                terminal = replace(
                    record,
                    status="runtime_error",
                    finished_at=datetime.now(UTC),
                    reason="cancel_termination_failed",
                )
            else:
                terminal = replace(
                    record,
                    status="cancelled",
                    exit_code=exit_code,
                    finished_at=datetime.now(UTC),
                    reason="cancel_requested",
                )
            self._storage.write_job(terminal)
            self._storage.append_trace(terminal)
            self._live_jobs.pop(session_id, None)
            LOGGER.info(
                "job_cancelled",
                extra={"session_id": session_id, "job_id": job_id, "status": terminal.status},
            )
            return self._response(terminal)

    async def aclose(self) -> None:
        """Stop local watcher tasks and their process groups during service shutdown."""
        live_jobs = list(self._live_jobs.values())
        for live in live_jobs:
            try:
                await live.execution.terminate()
            except Exception:
                LOGGER.exception(
                    "job_shutdown_termination_failed",
                    extra={"session_id": live.record.session_id, "job_id": live.job_id},
                )
            await self._terminalize(
                live.record, status="runtime_error", reason="service_shutdown"
            )
        await asyncio.gather(*(live.watcher for live in live_jobs), return_exceptions=True)
        close = getattr(self._executor, "aclose", None)
        if close is not None:
            await close()
