from __future__ import annotations

import pytest

from pyvisor.config import AppConfig, ConfigurationError


def test_config_uses_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYVISOR_SESSIONS_ROOT", raising=False)
    monkeypatch.delenv("PYVISOR_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("PYVISOR_EXECUTION_BACKEND", raising=False)
    monkeypatch.delenv("PYVISOR_SANDBOX_IMAGE_IDENTITY", raising=False)

    config = AppConfig.from_environment()

    assert str(config.sessions_root) == "/var/lib/pyvisor/sessions"
    assert config.default_timeout_seconds == 300
    assert config.maximum_timeout_seconds == 3_600
    assert config.listen_host == "127.0.0.1"
    assert config.listen_port == 8000


def test_config_rejects_default_timeout_above_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYVISOR_DEFAULT_TIMEOUT_SECONDS", "31")
    monkeypatch.setenv("PYVISOR_MAXIMUM_TIMEOUT_SECONDS", "30")

    with pytest.raises(ConfigurationError, match="must not exceed"):
        AppConfig.from_environment()


def test_config_rejects_unknown_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYVISOR_EXECUTION_BACKEND", "docker")

    with pytest.raises(ConfigurationError, match="EXECUTION_BACKEND"):
        AppConfig.from_environment()


@pytest.mark.parametrize(
    "image_identity",
    ("", "pyvisor-sandbox:unconfigured", "pyvisor-sandbox@sha256:REPLACE_WITH_NAR_HASH"),
)
def test_config_rejects_placeholder_runsc_image_identity(
    monkeypatch: pytest.MonkeyPatch, image_identity: str
) -> None:
    monkeypatch.setenv("PYVISOR_EXECUTION_BACKEND", "runsc")
    monkeypatch.setenv("PYVISOR_SANDBOX_IMAGE_IDENTITY", image_identity)

    with pytest.raises(ConfigurationError, match="non-placeholder"):
        AppConfig.from_environment()


def test_config_reads_listener_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYVISOR_LISTEN_HOST", "0.0.0.0")
    monkeypatch.setenv("PYVISOR_LISTEN_PORT", "8080")

    config = AppConfig.from_environment()

    assert config.listen_host == "0.0.0.0"
    assert config.listen_port == 8080
