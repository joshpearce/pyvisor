"""Small JSON structured logging setup for the service."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit stable, machine-readable lifecycle log records."""

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in ("session_id", "job_id", "status", "reason", "count"):
            value = getattr(record, key, None)
            if value is not None:
                event[key] = value
        return json.dumps(event, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str) -> None:
    """Configure the package logger without changing unrelated loggers."""
    logger = logging.getLogger("pyvisor")
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
