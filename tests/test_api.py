from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pyvisor.app import create_app
from pyvisor.config import AppConfig
from pyvisor.fake_executor import FakeExecution, FakeExecutor
from pyvisor.jobs import JobService


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "execution_backend": "runsc"}


@pytest.mark.asyncio
async def test_healthz_fails_for_explicit_unsafe_local_execution(tmp_path: Path) -> None:
    config = AppConfig(
        sessions_root=tmp_path / "sessions",
        runtime_root=tmp_path / "runtime",
        execution_backend="local",
        unsafe_local_execution=True,
    )
    app = create_app(config, JobService(config, FakeExecutor()))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as local_client:
        response = await local_client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unsafe_local_execution",
        "execution_backend": "local",
    }


@pytest.mark.asyncio
async def test_metrics_exposes_lifecycle_counters_without_identifiers(client: AsyncClient) -> None:
    started = await client.post("/v1/sessions/chat_01/jobs", json={"command": "print(2 + 2)"})
    assert started.status_code == 200

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'pyvisor_jobs_terminal_total{status="completed"} 1' in response.text
    assert "pyvisor_jobs_running 0" in response.text
    assert "chat_01" not in response.text


@pytest.mark.asyncio
async def test_run_completed_job(client: AsyncClient) -> None:
    response = await client.post("/v1/sessions/chat_01/jobs", json={"command": "print(2 + 2)"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["exit_code"] == 0
    assert body["reason"] is None
    assert body["stdout"] == {
        "text": "accepted: print(2 + 2)\\n",
        "truncated": False,
        "total_bytes": 24,
        "returned_bytes": 24,
    }


@pytest.mark.asyncio
async def test_run_start_failure_creates_a_durable_runtime_error(tmp_path: Path) -> None:
    config = AppConfig(sessions_root=tmp_path / "sessions", runtime_root=tmp_path / "runtime")
    service = JobService(
        config,
        FakeExecutor(lambda _: FakeExecution(start_error=RuntimeError("sandbox unavailable"))),
    )
    app = create_app(config, service)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/v1/sessions/chat_01/jobs", json={"command": "true"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "runtime_error"
    assert body["exit_code"] is None
    assert body["reason"] == "executor_start_failed"
    assert body["finished_at"] is not None
    state_path = tmp_path / "sessions" / "chat_01" / "jobs" / body["job_id"] / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "runtime_error"
    assert state["reason"] == "executor_start_failed"


@pytest.mark.asyncio
async def test_run_poll_cancel_running_job_and_busy_response(client: AsyncClient) -> None:
    started = await client.post("/v1/sessions/chat_01/jobs", json={"command": "long-running"})

    assert started.status_code == 202
    job_id = started.json()["job_id"]
    busy = await client.post("/v1/sessions/chat_01/jobs", json={"command": "another"})
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "busy"

    polled = await client.get(f"/v1/sessions/chat_01/jobs/{job_id}")
    assert polled.status_code == 200
    assert polled.json()["status"] == "running"

    cancelled = await client.post(f"/v1/sessions/chat_01/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["finished_at"] is not None

    repeated_cancel = await client.post(f"/v1/sessions/chat_01/jobs/{job_id}/cancel")
    assert repeated_cancel.status_code == 200
    assert repeated_cancel.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_invalid_requests_and_unknown_jobs(client: AsyncClient) -> None:
    invalid_session = await client.post("/v1/sessions/-invalid/jobs", json={"command": "x"})
    assert invalid_session.status_code == 400
    assert invalid_session.json()["error"]["code"] == "invalid_session_id"

    malformed = await client.post(
        "/v1/sessions/chat/jobs", json={"command": "", "unexpected": True}
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"

    invalid_job = await client.get("/v1/sessions/chat/jobs/not-a-uuid")
    assert invalid_job.status_code == 400
    assert invalid_job.json()["error"]["code"] == "invalid_request"

    unknown = await client.get("/v1/sessions/chat/jobs/00000000-0000-4000-8000-000000000000")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
