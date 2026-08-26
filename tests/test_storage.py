from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pyvisor.config import AppConfig
from pyvisor.fake_executor import FakeExecution, FakeExecutor
from pyvisor.jobs import JobService
from pyvisor.storage import JobRecord, SessionStorage


def _record() -> JobRecord:
    return JobRecord(
        session_id="chat_01",
        job_id="00000000-0000-4000-8000-000000000000",
        command="print('hello')",
        status="completed",
        started_at=datetime.now(UTC),
        timeout_seconds=30,
        finished_at=datetime.now(UTC),
        exit_code=0,
    )


def test_storage_creates_documented_layout_and_atomic_state(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path / "sessions", output_limit_bytes=100)
    record = _record()

    storage.create_job(record)
    storage.append_output(record, stdout="hello\n", stderr="warning\n")
    storage.write_job(record)
    storage.append_trace(record)

    session = tmp_path / "sessions" / record.session_id
    job = session / "jobs" / record.job_id
    assert (session / "workspace").is_dir()
    assert json.loads((session / "session.json").read_text())["session_id"] == record.session_id
    state = json.loads((job / "state.json").read_text())
    assert state["stdout_path"] == "stdout.bin"
    assert state["stderr_path"] == "stderr.bin"
    assert (job / "stdout.bin").read_bytes() == b"hello\n"
    assert (job / "stderr.bin").read_bytes() == b"warning\n"
    trace = [json.loads(line) for line in (session / "trace.jsonl").read_text().splitlines()]
    assert trace == [
        {
            **state,
            "stdout_bytes": 6,
            "stderr_bytes": 8,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "api_output_limit_bytes": 100,
        }
    ]


def test_output_is_bounded_head_tail_and_replaces_invalid_utf8(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path / "sessions", output_limit_bytes=40)
    record = _record()
    storage.create_job(record)
    output_path = tmp_path / "sessions" / record.session_id / "jobs" / record.job_id / "stdout.bin"
    output_path.write_bytes(b"head-" + b"x" * 60 + b"-tail" + bytes([0xFF]))

    stdout, stderr = storage.output(record)

    assert stdout.truncated is True
    assert stdout.total_bytes == 71
    assert stdout.returned_bytes == 40
    assert stdout.text.startswith("head-")
    assert stdout.text.endswith("tail�")
    assert "... output truncated ..." in stdout.text
    assert stderr.text == ""


def test_trace_repair_discards_an_incomplete_final_record(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path / "sessions", output_limit_bytes=100)
    first = _record()
    storage.create_job(first)
    storage.append_trace(first)
    trace_path = tmp_path / "sessions" / first.session_id / "trace.jsonl"
    trace_path.write_bytes(trace_path.read_bytes() + b'{"job_id":"incomplete"')
    second = replace(first, job_id="00000000-0000-4000-8000-000000000001", command="second")
    storage.create_job(second)

    storage.append_trace(second)

    trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [entry["job_id"] for entry in trace] == [first.job_id, second.job_id]


@pytest.mark.asyncio
async def test_completed_job_can_be_polled_after_service_restart(tmp_path: Path) -> None:
    config = AppConfig(sessions_root=tmp_path / "sessions", runtime_root=tmp_path / "runtime")
    executor = FakeExecutor(lambda _: FakeExecution(stdout="durable\n"))
    first = JobService(config, executor)
    submitted = await first.run("chat_01", "echo durable", None)

    restarted = JobService(config, FakeExecutor())
    await restarted.startup()
    polled = await restarted.poll("chat_01", submitted.job_id)

    assert polled.status == "completed"
    assert polled.stdout.text == "durable\n"
    await restarted.aclose()


@pytest.mark.asyncio
async def test_restart_terminalizes_interrupted_job_and_writes_one_trace(tmp_path: Path) -> None:
    config = AppConfig(sessions_root=tmp_path / "sessions", runtime_root=tmp_path / "runtime")
    first = JobService(
        config, FakeExecutor(lambda _: FakeExecution(status="running", exit_code=None))
    )
    submitted = await first.run("chat_01", "wait", None)

    restarted = JobService(config, FakeExecutor())
    await restarted.startup()
    polled = await restarted.poll("chat_01", submitted.job_id)
    trace_path = tmp_path / "sessions" / "chat_01" / "trace.jsonl"

    assert polled.status == "runtime_error"
    assert polled.finished_at is not None
    assert json.loads(trace_path.read_text())["reason"] == "service_restart"

    later_restart = JobService(config, FakeExecutor())
    await later_restart.startup()
    assert len(trace_path.read_text().splitlines()) == 1
    await later_restart.aclose()
    await restarted.aclose()
    await first.aclose()
