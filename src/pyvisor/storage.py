"""Durable session, job, output, and trace storage."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyvisor.ids import valid_job_id, valid_session_id
from pyvisor.models import JobStatus, OutputResponse

_FORMAT_VERSION = 1
_TRUNCATION_MARKER = b"\n... output truncated ...\n"


class StorageError(RuntimeError):
    """A durable state operation could not be completed safely."""


class StoredJobNotFoundError(StorageError):
    """The requested durable job record does not exist."""


@dataclass(frozen=True, slots=True)
class JobRecord:
    """The durable fields in one job's ``state.json`` record."""

    session_id: str
    job_id: str
    command: str
    status: JobStatus
    started_at: datetime
    timeout_seconds: int
    finished_at: datetime | None = None
    exit_code: int | None = None
    reason: str | None = None
    sandbox_image: str = "fake-executor"
    stdout_path: str = "stdout.bin"
    stderr_path: str = "stderr.bin"

    @property
    def terminal(self) -> bool:
        return self.status != "running"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise StorageError("durable timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise StorageError(f"durable job state has an invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StorageError(f"durable job state has an invalid {field}") from error
    return _as_utc(parsed)


class SessionStorage:
    """Owns the durable layout below the configured sessions root."""

    def __init__(self, sessions_root: Path, output_limit_bytes: int) -> None:
        self._root = sessions_root
        self._output_limit_bytes = output_limit_bytes

    def _root_path(self) -> Path:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._root.resolve(strict=True)

    @staticmethod
    def _directory(path: Path, *, create: bool) -> None:
        try:
            details = path.lstat()
        except FileNotFoundError:
            if not create:
                raise
            path.mkdir(mode=0o700)
            return
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise StorageError("service-managed storage path is not a directory")

    @staticmethod
    def _regular_file(path: Path) -> bool:
        """Return whether a managed file exists, refusing symlinks and special files."""
        try:
            details = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise StorageError("service-managed storage path is not a regular file")
        return True

    def _session_path(self, session_id: str, *, create: bool) -> Path:
        if not valid_session_id(session_id):
            raise StorageError("invalid session id for storage")
        root = self._root_path()
        path = root / session_id
        try:
            self._directory(path, create=create)
        except FileNotFoundError as error:
            raise StoredJobNotFoundError("session was not found") from error
        if path.parent.resolve(strict=True) != root:
            raise StorageError("session path escaped sessions root")
        return path

    def _job_path(self, session_id: str, job_id: str, *, create: bool) -> Path:
        if not valid_job_id(job_id):
            raise StorageError("invalid job id for storage")
        session_path = self._session_path(session_id, create=create)
        jobs_path = session_path / "jobs"
        self._directory(jobs_path, create=create)
        job_path = jobs_path / job_id
        try:
            self._directory(job_path, create=create)
        except FileNotFoundError as error:
            raise StoredJobNotFoundError("job was not found in this session") from error
        if job_path.parent.resolve(strict=True) != jobs_path.resolve(strict=True):
            raise StorageError("job path escaped service-managed directory")
        return job_path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_json(cls, path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(value, file, sort_keys=True, separators=(",", ":"))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _ensure_session(self, session_id: str) -> Path:
        session_path = self._session_path(session_id, create=True)
        self._directory(session_path / "workspace", create=True)
        self._directory(session_path / "jobs", create=True)
        session_file = session_path / "session.json"
        if not self._regular_file(session_file):
            self._atomic_json(
                session_file,
                {
                    "format_version": _FORMAT_VERSION,
                    "session_id": session_id,
                    "created_at": _timestamp(datetime.now(UTC)),
                },
            )
        return session_path

    @staticmethod
    def _state_value(record: JobRecord) -> dict[str, Any]:
        return {
            "format_version": _FORMAT_VERSION,
            "session_id": record.session_id,
            "job_id": record.job_id,
            "command": record.command,
            "status": record.status,
            "started_at": _timestamp(record.started_at),
            "timeout_seconds": record.timeout_seconds,
            "finished_at": _timestamp(record.finished_at) if record.finished_at else None,
            "exit_code": record.exit_code,
            "reason": record.reason,
            "stdout_path": record.stdout_path,
            "stderr_path": record.stderr_path,
            "sandbox_image": record.sandbox_image,
        }

    @staticmethod
    def _record(value: object) -> JobRecord:
        if not isinstance(value, dict):
            raise StorageError("durable job state is not an object")
        required = ("format_version", "session_id", "job_id", "command", "status", "timeout_seconds")
        if any(field not in value for field in required):
            raise StorageError("durable job state is missing required fields")
        if value["format_version"] != _FORMAT_VERSION:
            raise StorageError("durable job state has an unsupported format version")
        session_id = value["session_id"]
        job_id = value["job_id"]
        status = value["status"]
        if (
            not isinstance(session_id, str)
            or not valid_session_id(session_id)
            or not isinstance(job_id, str)
            or not valid_job_id(job_id)
            or status not in {"running", "completed", "timed_out", "cancelled", "runtime_error"}
        ):
            raise StorageError("durable job state has invalid identifiers or status")
        command = value["command"]
        timeout_seconds = value["timeout_seconds"]
        if not isinstance(command, str) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise StorageError("durable job state has invalid command or timeout")
        if value.get("stdout_path") != "stdout.bin" or value.get("stderr_path") != "stderr.bin":
            raise StorageError("durable job state has invalid output paths")
        finished_at = value.get("finished_at")
        exit_code = value.get("exit_code")
        reason = value.get("reason")
        sandbox_image = value.get("sandbox_image")
        if finished_at is not None and not isinstance(finished_at, str):
            raise StorageError("durable job state has an invalid finished_at")
        if exit_code is not None and not isinstance(exit_code, int):
            raise StorageError("durable job state has an invalid exit_code")
        if reason is not None and not isinstance(reason, str):
            raise StorageError("durable job state has an invalid reason")
        if not isinstance(sandbox_image, str):
            raise StorageError("durable job state has an invalid sandbox image")
        return JobRecord(
            session_id=session_id,
            job_id=job_id,
            command=command,
            status=status,
            started_at=_parse_timestamp(value.get("started_at"), "started_at"),
            timeout_seconds=timeout_seconds,
            finished_at=_parse_timestamp(finished_at, "finished_at") if finished_at else None,
            exit_code=exit_code,
            reason=reason,
            sandbox_image=sandbox_image,
        )

    def create_job(self, record: JobRecord) -> None:
        self._ensure_session(record.session_id)
        job_path = self._job_path(record.session_id, record.job_id, create=True)
        state_path = job_path / "state.json"
        if self._regular_file(state_path):
            raise StorageError("durable job state already exists")
        for output_name in (record.stdout_path, record.stderr_path):
            with (job_path / output_name).open("xb"):
                pass
        self.write_job(record)

    def write_job(self, record: JobRecord) -> None:
        job_path = self._job_path(record.session_id, record.job_id, create=False)
        self._atomic_json(job_path / "state.json", self._state_value(record))

    def load_job(self, session_id: str, job_id: str) -> JobRecord:
        state_path = self._job_path(session_id, job_id, create=False) / "state.json"
        try:
            if not self._regular_file(state_path):
                raise FileNotFoundError(state_path)
            with state_path.open(encoding="utf-8") as file:
                record = self._record(json.load(file))
        except FileNotFoundError as error:
            raise StoredJobNotFoundError("job was not found in this session") from error
        except json.JSONDecodeError as error:
            raise StorageError("durable job state is corrupt") from error
        if record.session_id != session_id or record.job_id != job_id:
            raise StorageError("durable job state does not match its path")
        return record

    def append_output(self, record: JobRecord, *, stdout: str = "", stderr: str = "") -> None:
        job_path = self._job_path(record.session_id, record.job_id, create=False)
        for name, text in ((record.stdout_path, stdout), (record.stderr_path, stderr)):
            if not text:
                continue
            output_path = job_path / name
            if not self._regular_file(output_path):
                raise StorageError("durable output file is missing")
            with output_path.open("ab") as file:
                file.write(text.encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())

    @staticmethod
    def _bounded_output(path: Path, limit: int) -> OutputResponse:
        if not SessionStorage._regular_file(path):
            raise StorageError("durable output file is missing")
        total_bytes = path.stat().st_size
        if total_bytes <= limit:
            payload = path.read_bytes()
        elif limit <= len(_TRUNCATION_MARKER):
            with path.open("rb") as file:
                payload = file.read(limit)
        else:
            remaining = limit - len(_TRUNCATION_MARKER)
            head_size = remaining // 2
            tail_size = remaining - head_size
            with path.open("rb") as file:
                head = file.read(head_size)
                file.seek(-tail_size, os.SEEK_END)
                tail = file.read(tail_size)
            payload = head + _TRUNCATION_MARKER + tail
        return OutputResponse(
            text=payload.decode("utf-8", errors="replace"),
            truncated=total_bytes > limit,
            total_bytes=total_bytes,
            returned_bytes=len(payload),
        )

    def output(self, record: JobRecord) -> tuple[OutputResponse, OutputResponse]:
        job_path = self._job_path(record.session_id, record.job_id, create=False)
        return (
            self._bounded_output(job_path / record.stdout_path, self._output_limit_bytes),
            self._bounded_output(job_path / record.stderr_path, self._output_limit_bytes),
        )

    def _trace_job_ids(self, trace_path: Path) -> set[str]:
        if not self._regular_file(trace_path):
            return set()
        raw = trace_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raw = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
            with trace_path.open("wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
        job_ids: set[str] = set()
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise StorageError("durable trace contains invalid complete JSONL") from error
            job_id = value.get("job_id") if isinstance(value, dict) else None
            if isinstance(job_id, str):
                job_ids.add(job_id)
        return job_ids

    def append_trace(self, record: JobRecord) -> None:
        if not record.terminal:
            raise StorageError("cannot trace a running job")
        session_path = self._session_path(record.session_id, create=False)
        trace_path = session_path / "trace.jsonl"
        if record.job_id in self._trace_job_ids(trace_path):
            return
        stdout, stderr = self.output(record)
        value = {
            **self._state_value(record),
            "stdout_bytes": stdout.total_bytes,
            "stderr_bytes": stderr.total_bytes,
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "api_output_limit_bytes": self._output_limit_bytes,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        with trace_path.open("ab") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        self._fsync_directory(session_path)

    def reconcile_interrupted_jobs(self) -> list[JobRecord]:
        """Terminalize jobs that cannot have a trusted live executor after restart."""
        if not self._root.exists():
            return []
        reconciled: list[JobRecord] = []
        root = self._root_path()
        for session_path in root.iterdir():
            if not session_path.is_dir() or session_path.is_symlink() or not valid_session_id(session_path.name):
                continue
            jobs_path = session_path / "jobs"
            if not jobs_path.is_dir() or jobs_path.is_symlink():
                continue
            for job_path in jobs_path.iterdir():
                if not job_path.is_dir() or job_path.is_symlink() or not valid_job_id(job_path.name):
                    continue
                record = self.load_job(session_path.name, job_path.name)
                if record.terminal:
                    continue
                terminal = replace(
                    record,
                    status="runtime_error",
                    finished_at=datetime.now(UTC),
                    reason="service_restart",
                )
                self.write_job(terminal)
                self.append_trace(terminal)
                reconciled.append(terminal)
        return reconciled
