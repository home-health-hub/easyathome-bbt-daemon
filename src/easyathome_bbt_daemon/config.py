"""Configuration loading for the daemon.

Only three sections exist in this phase: ``[daemon]`` (device identity and
collector behavior), ``[storage]`` (database path), and ``[api]`` (the two
read-only capability/health endpoints). Later phases (context-entry UI,
dashboards, reports, interpretation) will likely add their own sections
without needing to change these loaders.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass
class DaemonConfig:
    """Parsed ``[daemon]``/``[storage]`` sections."""

    config_path: Path
    address: str
    device_timezone: str
    retry_seconds: int
    log_level: str
    db_path: str


@dataclass
class ApiConfig:
    """Parsed ``[api]`` section: bind address, port, and optional bearer token."""

    enabled: bool
    host: str
    port: int
    token: str  # "" means no authentication required


DEFAULT_API_CONFIG = ApiConfig(enabled=False, host="127.0.0.1", port=8081, token="")

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _parse_bool(value: str, key: str) -> bool:
    """Parse a yes/no-style config value.

    Args:
        value: Raw string from the config file.
        key: Dotted key name, used in the error message.

    Returns:
        The parsed boolean.

    Raises:
        ConfigError: If ``value`` isn't a recognized yes/no spelling.
    """
    normalized = value.strip().lower()
    if normalized in ("yes", "true", "1", "on"):
        return True
    if normalized in ("no", "false", "0", "off"):
        return False
    raise ConfigError(f"{key} must be yes/no, got {value!r}")


def load_config(config_path: str) -> DaemonConfig:
    """Load and validate the ``[daemon]``/``[storage]`` sections.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed configuration.

    Raises:
        ConfigError: If the file is missing or a required value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy "
            "config/easyathome-bbt-daemon.ini.example to this path and edit it."
        )

    parser = configparser.ConfigParser()
    parser.read(path)

    daemon = parser["daemon"] if parser.has_section("daemon") else {}
    storage = parser["storage"] if parser.has_section("storage") else {}

    address = daemon.get("address", "").strip()
    if not address:
        raise ConfigError(
            "daemon.address must be set -- unlike some sibling daemons, "
            "easyathome-ble exposes no discover() helper of its own, so this "
            "daemon cannot auto-discover the EBT-300's BLE address (see "
            "CLAUDE.md); find it by hand (e.g. via bluetoothctl or nRF Connect) "
            "and set it here"
        )

    try:
        retry_seconds = int(daemon.get("retry_seconds", "30"))
    except ValueError as exc:
        raise ConfigError("daemon.retry_seconds must be an integer") from exc
    if retry_seconds <= 0:
        raise ConfigError("daemon.retry_seconds must be a positive number")

    device_timezone = daemon.get("device_timezone", "").strip()
    if not device_timezone:
        raise ConfigError(
            "daemon.device_timezone must be set -- the EBT-300 reports naive "
            "local timestamps with no offset, so this daemon needs an explicit "
            "assumption to resolve them into absolute instants (see "
            "device_taken_at_tz_assumption in the database schema)"
        )

    db_path = storage.get("db_path", "").strip()
    if not db_path:
        raise ConfigError("storage.db_path must be set")

    return DaemonConfig(
        config_path=path,
        address=address,
        device_timezone=device_timezone,
        retry_seconds=retry_seconds,
        log_level=daemon.get("log_level", "INFO").strip().upper(),
        db_path=db_path,
    )


def load_api_config(config_path: str) -> ApiConfig:
    """Load the ``[api]`` section, if present.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed API configuration, or ``DEFAULT_API_CONFIG`` (disabled,
        bound to loopback) if the file has no ``[api]`` section.

    Raises:
        ConfigError: If the file is missing or an ``[api]`` value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    if not parser.has_section("api"):
        return DEFAULT_API_CONFIG

    api = parser["api"]

    try:
        port = int(api.get("port", str(DEFAULT_API_CONFIG.port)))
    except ValueError as exc:
        raise ConfigError("api.port must be an integer") from exc

    return ApiConfig(
        enabled=_parse_bool(api.get("enabled", "no"), "api.enabled"),
        host=api.get("host", DEFAULT_API_CONFIG.host).strip() or DEFAULT_API_CONFIG.host,
        port=port,
        token=api.get("token", "").strip(),
    )


def is_insecurely_exposed(api_config: ApiConfig) -> bool:
    """Return whether the API is bound to a non-loopback address with no auth token.

    Not a hard failure -- a reverse proxy handling auth in front is a
    legitimate setup -- but it's the shape of a mistake worth surfacing
    rather than silently allowing.
    """
    return api_config.host not in _LOOPBACK_HOSTS and not api_config.token
