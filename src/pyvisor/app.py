"""FastAPI application factory and HTTP error translation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from pyvisor.config import AppConfig
from pyvisor.ids import valid_job_id, valid_session_id
from pyvisor.jobs import BusyError, InvalidRequestError, JobService, NotFoundError
from pyvisor.logging import configure_logging
from pyvisor.models import ErrorResponse, HealthResponse, JobResponse, RunJobRequest
from pyvisor.storage import StorageError

LOGGER = logging.getLogger("pyvisor.app")


class InvalidIdentifierError(ValueError):
    """An identifier failed validation before reaching service state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(error={"code": code, "message": message})
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _validated_session_id(session_id: str) -> str:
    if not valid_session_id(session_id):
        raise InvalidIdentifierError("invalid_session_id", "session_id is invalid")
    return session_id


def _validated_job_id(job_id: str) -> str:
    if not valid_job_id(job_id):
        raise InvalidIdentifierError("invalid_request", "job_id is invalid")
    return job_id


def create_app(config: AppConfig | None = None, jobs: JobService | None = None) -> FastAPI:
    """Create the service application, allowing dependency injection in tests."""
    config = config or AppConfig.from_environment()
    configure_logging(config.log_level)
    jobs = jobs or JobService(config)
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await jobs.startup()
        yield
        await jobs.aclose()

    app = FastAPI(
        title="Pyvisor", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error(400, "invalid_request", "request body is invalid")

    @app.exception_handler(InvalidIdentifierError)
    async def identifier_error(_: Request, error: InvalidIdentifierError) -> JSONResponse:
        return _error(400, error.code, str(error))

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse | JSONResponse:
        if config.execution_backend != "runsc":
            response = HealthResponse(
                status="unsafe_local_execution", execution_backend=config.execution_backend
            )
            return JSONResponse(status_code=503, content=response.model_dump())
        return HealthResponse(execution_backend=config.execution_backend)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            jobs.prometheus_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    @app.post(
        "/v1/sessions/{session_id}/jobs",
        response_model=JobResponse,
        responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def run_job(session_id: str, request: RunJobRequest) -> JSONResponse | JobResponse:
        _validated_session_id(session_id)
        try:
            response = await jobs.run(session_id, request.command, request.timeout_seconds)
        except BusyError as error:
            return _error(409, "busy", str(error))
        except InvalidRequestError as error:
            return _error(400, "invalid_request", str(error))
        except StorageError:
            LOGGER.exception("durable_storage_failure", extra={"session_id": session_id})
            return _error(500, "internal_error", "durable job storage failed")
        status_code = 202 if response.status == "running" else 200
        return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))

    @app.get(
        "/v1/sessions/{session_id}/jobs/{job_id}",
        response_model=JobResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def poll_job(session_id: str, job_id: str) -> JobResponse:
        _validated_session_id(session_id)
        _validated_job_id(job_id)
        try:
            return await jobs.poll(session_id, job_id)
        except NotFoundError as error:
            return _error(404, "not_found", str(error))  # type: ignore[return-value]
        except StorageError:
            LOGGER.exception(
                "durable_storage_failure", extra={"session_id": session_id, "job_id": job_id}
            )
            return _error(500, "internal_error", "durable job storage failed")  # type: ignore[return-value]

    @app.post(
        "/v1/sessions/{session_id}/jobs/{job_id}/cancel",
        response_model=JobResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def cancel_job(session_id: str, job_id: str) -> JobResponse:
        _validated_session_id(session_id)
        _validated_job_id(job_id)
        try:
            return await jobs.cancel(session_id, job_id)
        except NotFoundError as error:
            return _error(404, "not_found", str(error))  # type: ignore[return-value]
        except StorageError:
            LOGGER.exception(
                "durable_storage_failure", extra={"session_id": session_id, "job_id": job_id}
            )
            return _error(500, "internal_error", "durable job storage failed")  # type: ignore[return-value]

    LOGGER.info("service_boot", extra={"execution_backend": config.execution_backend})
    return app


def main() -> None:
    """Run the development HTTP server."""
    config = AppConfig.from_environment()
    uvicorn.run(create_app(config), host=config.listen_host, port=config.listen_port, reload=False)
