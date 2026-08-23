from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pyvisor.config import AppConfig
from pyvisor.jobs import BusyError, JobService


def _service(tmp_path: Path, **config_values: object) -> JobService:
    values = {
        "sessions_root": tmp_path / "sessions",
        "runtime_root": tmp_path / "runtime",
        "foreground_soft_cap_seconds": 0.05,
        **config_values,
    }
    config = AppConfig(**values)  # type: ignore[arg-type]
    return JobService(config)


@pytest.mark.asyncio
async def test_local_command_completes_in_workspace_and_keeps_streams(tmp_path: Path) -> None:
    jobs = _service(tmp_path)

    response = await jobs.run(
        "chat_01",
        "printf 'out'; printf 'err' >&2; printf workspace > created.txt",
        None,
    )

    assert response.status == "completed"
    assert response.exit_code == 0
    assert response.stdout.text == "out"
    assert response.stderr.text == "err"
    workspace_file = tmp_path / "sessions" / "chat_01" / "workspace" / "created.txt"
    assert workspace_file.read_text() == "workspace"


@pytest.mark.asyncio
async def test_running_job_returns_after_soft_cap_then_can_be_polled(tmp_path: Path) -> None:
    jobs = _service(tmp_path)

    started = await jobs.run("chat_01", "printf start; sleep 0.15; printf finish", None)

    assert started.status == "running"
    assert started.stdout.text == "start"
    await asyncio.sleep(0.2)
    finished = await jobs.poll("chat_01", started.job_id)
    assert finished.status == "completed"
    assert finished.stdout.text == "startfinish"


@pytest.mark.asyncio
async def test_timeout_kills_process_group_and_preserves_partial_output(tmp_path: Path) -> None:
    jobs = _service(tmp_path, foreground_soft_cap_seconds=0.01)

    started = await jobs.run("chat_01", "printf before; sleep 10", timeout_seconds=1)

    assert started.status == "running"
    await asyncio.sleep(1.1)
    terminal = await jobs.poll("chat_01", started.job_id)
    assert terminal.status == "timed_out"
    assert terminal.stdout.text == "before"
    assert terminal.exit_code is not None


@pytest.mark.asyncio
async def test_cancel_kills_process_group_and_releases_session(tmp_path: Path) -> None:
    jobs = _service(tmp_path)
    pid_path = tmp_path / "child.pid"

    started = await jobs.run(
        "chat_01", f"sleep 10 & child=$!; printf '%s' \"$child\" > {pid_path}; wait", None
    )
    assert started.status == "running"
    for _ in range(20):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    child_pid = int(pid_path.read_text())

    cancelled = await jobs.cancel("chat_01", started.job_id)

    assert cancelled.status == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    next_job = await jobs.run("chat_01", "printf next", None)
    assert next_job.status == "completed"
    assert next_job.stdout.text == "next"


@pytest.mark.asyncio
async def test_live_job_makes_second_run_busy(tmp_path: Path) -> None:
    jobs = _service(tmp_path)
    started = await jobs.run("chat_01", "sleep 10", None)
    assert started.status == "running"

    with pytest.raises(BusyError):
        await jobs.run("chat_01", "printf no", None)

    await jobs.cancel("chat_01", started.job_id)
