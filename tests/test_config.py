from __future__ import annotations

import pytest

from pyvisor.config import AppConfig, ConfigurationError
from pyvisor.jobs import JobService
from pyvisor.runsc_executor import RunscExecutor


def test_config_uses_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYVISOR_SESSIONS_ROOT", raising=False)
    monkeypatch.delenv("PYVISOR_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("PYVISOR_EXECUTION_BACKEND", raising=False)
    monkeypatch.delenv("PYVISOR_UNSAFE_LOCAL_EXECUTION", raising=False)
    monkeypatch.delenv("PYVISOR_SANDBOX_IMAGE_IDENTITY", raising=False)
    monkeypatch.setenv("PYVISOR_SANDBOX_IMAGE_IDENTITY", "python-3.13.5@sha256:example")

    config = AppConfig.from_environment()

    assert str(config.sessions_root) == "/var/lib/pyvisor/sessions"
    assert config.default_timeout_seconds == 300
    assert config.maximum_timeout_seconds == 3_600
    assert config.listen_host == "127.0.0.1"
    assert config.listen_port == 8000
    assert config.execution_backend == "runsc"
    assert not config.unsafe_local_execution


def test_config_rejects_local_execution_without_explicit_unsafe_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYVISOR_EXECUTION_BACKEND", "local")
    monkeypatch.delenv("PYVISOR_UNSAFE_LOCAL_EXECUTION", raising=False)

    with pytest.raises(ConfigurationError, match="UNSAFE_LOCAL_EXECUTION=1"):
        AppConfig.from_environment()


def test_config_allows_explicit_unsafe_local_execution_only_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYVISOR_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("PYVISOR_UNSAFE_LOCAL_EXECUTION", "1")
    monkeypatch.setenv("PYVISOR_LISTEN_HOST", "127.0.0.1")

    config = AppConfig.from_environment()

    assert config.execution_backend == "local"
    assert config.unsafe_local_execution


def test_config_rejects_unsafe_local_execution_on_non_loopback_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYVISOR_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("PYVISOR_UNSAFE_LOCAL_EXECUTION", "1")
    monkeypatch.setenv("PYVISOR_LISTEN_HOST", "0.0.0.0")

    with pytest.raises(ConfigurationError, match="exact loopback"):
        AppConfig.from_environment()


def test_config_rejects_noncanonical_unsafe_local_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYVISOR_UNSAFE_LOCAL_EXECUTION", "true")

    with pytest.raises(ConfigurationError, match="exactly '1'"):
        AppConfig.from_environment()


def test_programmatic_local_config_requires_unsafe_opt_in() -> None:
    with pytest.raises(ConfigurationError, match="UNSAFE_LOCAL_EXECUTION=1"):
        AppConfig(execution_backend="local")


def test_programmatic_local_config_refuses_non_loopback_listener() -> None:
    with pytest.raises(ConfigurationError, match="exact loopback"):
        AppConfig(
            execution_backend="local", unsafe_local_execution=True, listen_host="0.0.0.0"
        )


def test_job_service_defaults_to_runsc_executor() -> None:
    service = JobService(AppConfig())

    assert isinstance(service._executor, RunscExecutor)


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
    monkeypatch.setenv("PYVISOR_SANDBOX_IMAGE_IDENTITY", "python-3.13.5@sha256:example")

    config = AppConfig.from_environment()

    assert config.listen_host == "0.0.0.0"
    assert config.listen_port == 8080
