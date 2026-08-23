"""Validation for identifiers that will later map to durable paths."""

from __future__ import annotations

import re
from uuid import UUID

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_session_id(value: str) -> bool:
    return SESSION_ID_PATTERN.fullmatch(value) is not None


def valid_job_id(value: str) -> bool:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return str(parsed) == value
