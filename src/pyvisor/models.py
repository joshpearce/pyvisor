"""Stable HTTP request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["running", "completed", "timed_out", "cancelled", "runtime_error"]


class RunJobRequest(BaseModel):
    """The caller-controlled part of a job submission."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    timeout_seconds: int | None = Field(default=None, strict=True, ge=1)


class OutputResponse(BaseModel):
    text: str
    truncated: bool = False
    total_bytes: int = Field(ge=0)
    returned_bytes: int = Field(ge=0)


class JobResponse(BaseModel):
    session_id: str
    job_id: str
    status: JobStatus
    command: str
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    stdout: OutputResponse
    stderr: OutputResponse


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
