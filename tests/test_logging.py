from __future__ import annotations

import json
import logging
import sys

from pyvisor.logging import JsonFormatter


def test_json_formatter_includes_bounded_exception_detail() -> None:
    formatter = JsonFormatter()

    try:
        raise RuntimeError("runsc launch failed: permission denied")
    except RuntimeError:
        record = logging.LogRecord(
            "pyvisor.jobs",
            logging.ERROR,
            __file__,
            0,
            "job_start_failed",
            (),
            exc_info=sys.exc_info(),
        )

    event = json.loads(formatter.format(record))

    assert event["event"] == "job_start_failed"
    assert event["error_type"] == "RuntimeError"
    assert event["error"] == "runsc launch failed: permission denied"


def test_json_formatter_bounds_exception_detail() -> None:
    formatter = JsonFormatter()

    try:
        raise ValueError("x" * 5_000)
    except ValueError:
        record = logging.LogRecord(
            "pyvisor.jobs",
            logging.ERROR,
            __file__,
            0,
            "job_start_failed",
            (),
            exc_info=sys.exc_info(),
        )

    event = json.loads(formatter.format(record))

    assert event["error_type"] == "ValueError"
    assert event["error"] == "x" * 4_096
