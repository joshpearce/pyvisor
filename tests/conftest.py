from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from pyvisor.app import create_app
from pyvisor.config import AppConfig
from pyvisor.fake_executor import FakeExecution, FakeExecutor
from pyvisor.jobs import JobService


@pytest.fixture
def app() -> FastAPI:
    def planner(command: str) -> FakeExecution:
        if command == "long-running":
            return FakeExecution(status="running", exit_code=None)
        return FakeExecution(stdout=f"accepted: {command}\\n")

    config = AppConfig(sessions_root=Path("/tmp/sessions"))
    service = JobService(config, FakeExecutor(planner))
    return create_app(config, service)
