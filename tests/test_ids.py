from __future__ import annotations

import pytest

from pyvisor.ids import valid_job_id, valid_session_id


@pytest.mark.parametrize("value", ["a", "chat_01", "A-1", "a" * 64])
def test_valid_session_ids(value: str) -> None:
    assert valid_session_id(value)


@pytest.mark.parametrize("value", ["", "-start", "has space", "../escape", "a" * 65])
def test_invalid_session_ids(value: str) -> None:
    assert not valid_session_id(value)


def test_job_ids_must_be_canonical_lowercase_uuids() -> None:
    assert valid_job_id("00000000-0000-4000-8000-000000000000")
    assert valid_job_id("00000000-0000-4000-4000-000000000000")
    assert not valid_job_id("00000000-0000-4000-4000-00000000000A")
    assert not valid_job_id("00000000-0000-4000-8000-00000000000Z")
