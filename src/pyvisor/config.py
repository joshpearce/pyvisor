"""Typed service configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when a Pyvisor environment setting is invalid."""


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return parsed


def _positive_float(name: str, default: float) -> float:
    value = os.getenv(name, str(default))
    try:
        parsed = float(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return parsed


def _enabled(name: str) -> bool:
    """Return whether a deliberately dangerous development switch is enabled."""
    value = os.getenv(name)
    if value is None:
        return False
    if value != "1":
        raise ConfigurationError(f"{name} must be exactly '1' when set")
    return True


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Configuration values used by the service and execution backends."""

    sessions_root: Path = Path("/var/lib/pyvisor/sessions")
    runtime_root: Path = Path("/var/lib/pyvisor/runtime")
    foreground_soft_cap_seconds: float = 5.0
    default_timeout_seconds: int = 300
    maximum_timeout_seconds: int = 3_600
    output_limit_bytes: int = 65_536
    command_limit_bytes: int = 65_536
    idle_reap_seconds: int = 1_800
    cpu_limit: int = 1
    memory_limit_bytes: int = 1_073_741_824
    session_disk_limit_bytes: int = 1_073_741_824
    scratch_disk_limit_bytes: int = 536_870_912
    execution_backend: str = "runsc"
    unsafe_local_execution: bool = False
    runsc_binary: Path = Path("/usr/local/bin/runsc")
    sandbox_rootfs: Path = Path("/var/lib/pyvisor/rootfs")
    sandbox_image_identity: str = "pyvisor-sandbox:unconfigured"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        """Reject unsafe local execution even for programmatically built configurations."""
        if self.execution_backend != "local":
            return
        if self.unsafe_local_execution is not True:
            raise ConfigurationError(
                "PYVISOR_EXECUTION_BACKEND=local requires PYVISOR_UNSAFE_LOCAL_EXECUTION=1"
            )
        if self.listen_host not in {"127.0.0.1", "::1"}:
            raise ConfigurationError(
                "local execution requires PYVISOR_LISTEN_HOST to be an exact loopback address"
            )

    @classmethod
    def from_environment(cls) -> AppConfig:
        """Read and validate the ``PYVISOR_*`` environment settings."""
        config = cls(
            sessions_root=Path(os.getenv("PYVISOR_SESSIONS_ROOT", "/var/lib/pyvisor/sessions")),
            runtime_root=Path(os.getenv("PYVISOR_RUNTIME_ROOT", "/var/lib/pyvisor/runtime")),
            foreground_soft_cap_seconds=_positive_float("PYVISOR_FOREGROUND_SOFT_CAP_SECONDS", 5),
            default_timeout_seconds=_positive_int("PYVISOR_DEFAULT_TIMEOUT_SECONDS", 300),
            maximum_timeout_seconds=_positive_int("PYVISOR_MAXIMUM_TIMEOUT_SECONDS", 3_600),
            output_limit_bytes=_positive_int("PYVISOR_OUTPUT_LIMIT_BYTES", 65_536),
            command_limit_bytes=_positive_int("PYVISOR_COMMAND_LIMIT_BYTES", 65_536),
            idle_reap_seconds=_positive_int("PYVISOR_IDLE_REAP_SECONDS", 1_800),
            cpu_limit=_positive_int("PYVISOR_CPU_LIMIT", 1),
            memory_limit_bytes=_positive_int("PYVISOR_MEMORY_LIMIT_BYTES", 1_073_741_824),
            session_disk_limit_bytes=_positive_int(
                "PYVISOR_SESSION_DISK_LIMIT_BYTES", 1_073_741_824
            ),
            scratch_disk_limit_bytes=_positive_int("PYVISOR_SCRATCH_DISK_LIMIT_BYTES", 536_870_912),
            execution_backend=os.getenv("PYVISOR_EXECUTION_BACKEND", "runsc").lower(),
            unsafe_local_execution=_enabled("PYVISOR_UNSAFE_LOCAL_EXECUTION"),
            runsc_binary=Path(os.getenv("PYVISOR_RUNSC_BINARY", "/usr/local/bin/runsc")),
            sandbox_rootfs=Path(os.getenv("PYVISOR_SANDBOX_ROOTFS", "/var/lib/pyvisor/rootfs")),
            sandbox_image_identity=os.getenv(
                "PYVISOR_SANDBOX_IMAGE_IDENTITY", "pyvisor-sandbox:unconfigured"
            ),
            listen_host=os.getenv("PYVISOR_LISTEN_HOST", "127.0.0.1"),
            listen_port=_positive_int("PYVISOR_LISTEN_PORT", 8000),
            log_level=os.getenv("PYVISOR_LOG_LEVEL", "INFO").upper(),
        )
        if config.default_timeout_seconds > config.maximum_timeout_seconds:
            raise ConfigurationError(
                "PYVISOR_DEFAULT_TIMEOUT_SECONDS must not exceed "
                "PYVISOR_MAXIMUM_TIMEOUT_SECONDS"
            )
        if config.sessions_root == config.runtime_root:
            raise ConfigurationError("PYVISOR_SESSIONS_ROOT and PYVISOR_RUNTIME_ROOT must differ")
        if config.execution_backend not in {"local", "runsc"}:
            raise ConfigurationError("PYVISOR_EXECUTION_BACKEND must be either 'local' or 'runsc'")
        if config.execution_backend == "runsc" and (
            not config.sandbox_image_identity
            or config.sandbox_image_identity == "pyvisor-sandbox:unconfigured"
            or "REPLACE_" in config.sandbox_image_identity
        ):
            raise ConfigurationError(
                "PYVISOR_SANDBOX_IMAGE_IDENTITY must be a non-placeholder value for runsc"
            )
        if not config.listen_host:
            raise ConfigurationError("PYVISOR_LISTEN_HOST must not be empty")
        return config
