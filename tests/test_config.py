from __future__ import annotations

import pytest

from pyvisor.config import AppConfig, ConfigurationError


def test_config_uses_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYVISOR_SESSIONS_ROOT", raising=False)
    monkeypatch.delenv("PYVISOR_RUNTIME_ROOT", raising=False)

    config = AppConfig.from_environment()

    assert str(config.sessions_root) == "/var/lib/pyvisor/sessions"
    assert config.default_timeout_seconds == 300
    assert config.maximum_timeout_seconds == 3_600


def test_config_rejects_default_timeout_above_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYVISOR_DEFAULT_TIMEOUT_SECONDS", "31")
    monkeypatch.setenv("PYVISOR_MAXIMUM_TIMEOUT_SECONDS", "30")

    with pytest.raises(ConfigurationError, match="must not exceed"):
        AppConfig.from_environment()
