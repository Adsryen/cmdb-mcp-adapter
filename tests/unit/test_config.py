"""Configuration loading, precedence, and fail-closed validation.

The env-var contract is:
    CLI flags (http --host/--port) > environment > safe defaults.

Secrets (MCP_AUTH_TOKEN, VEOPS_KEY, VEOPS_SECRET) are only ever read from the
environment; this module never opens a file.  The examples/ directory
(if it exists) documents variable *names* only and is not a secret
source — verified by the secret-scan regression test in
tests/integration/test_secret_scan.py.
"""

from __future__ import annotations

import os

import pytest

from cmdb_mcp_adapter.config import (
    DEFAULT_ADAPTER,
    DEFAULT_BIND_HOST,
    DEFAULT_HTTP_MAX_RETRIES,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_PORT,
    Settings,
    UpstreamSettings,
    apply_cli_overrides,
    load_settings,
    load_upstream_settings,
    validate_settings,
    validate_upstream_settings,
)
from cmdb_mcp_adapter.core import ConfigurationError


def _clean_env() -> dict[str, str]:
    """Return a clean environment dict with all adapter env vars removed."""
    out: dict[str, str] = {**os.environ}
    for key in (
        "MCP_BIND_HOST",
        "MCP_PORT",
        "MCP_ADAPTER",
        "MCP_AUTH_TOKEN",
        "MCP_HTTP_TIMEOUT",
        "MCP_HTTP_MAX_RETRIES",
        "MCP_LOG_LEVEL",
        "VEOPS_BASE_URL",
        "VEOPS_KEY",
        "VEOPS_SECRET",
        "VEOPS_TOKEN",
    ):
        out.pop(key, None)
    return out

# ---------------------------------------------------------------------------
# load_settings: defaults
# ---------------------------------------------------------------------------
class TestLoadSettingsDefaults:
    def test_defaults(self) -> None:
        s = load_settings({})
        assert s.bind_host == DEFAULT_BIND_HOST
        assert s.port == DEFAULT_PORT
        assert s.adapter == DEFAULT_ADAPTER
        assert s.auth_token is None
        assert s.http_timeout_seconds == DEFAULT_HTTP_TIMEOUT_SECONDS
        assert s.http_max_retries == DEFAULT_HTTP_MAX_RETRIES
        assert s.log_level == DEFAULT_LOG_LEVEL

    def test_env_values(self) -> None:
        env: dict[str, str] = {
            "MCP_BIND_HOST": "0.0.0.0",
            "MCP_PORT": "9001",
            "MCP_ADAPTER": "mock",
            "MCP_AUTH_TOKEN": "some-token",
            "MCP_HTTP_TIMEOUT": "5.5",
            "MCP_HTTP_MAX_RETRIES": "0",
            "MCP_LOG_LEVEL": "debug",
        }
        s = load_settings(env)
        assert s.bind_host == "0.0.0.0"
        assert s.port == 9001
        assert s.adapter == "mock"
        assert s.auth_token == "some-token"
        assert s.http_timeout_seconds == 5.5
        assert s.http_max_retries == 0
        assert s.log_level == "DEBUG"

    def test_empty_token_becomes_none(self) -> None:
        s = load_settings({"MCP_AUTH_TOKEN": ""})
        assert s.auth_token is None

    def test_whitespace_only_token_becomes_none(self) -> None:
        s = load_settings({"MCP_AUTH_TOKEN": "   "})
        assert s.auth_token is None


# ---------------------------------------------------------------------------
# load_settings: invalid values (fail closed at load time)
# ---------------------------------------------------------------------------
class TestLoadSettingsInvalid:
    def test_invalid_port(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_PORT": "not-a-number"})

    def test_negative_port(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_PORT": "-1"})

    def test_port_above_max(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_PORT": "70000"})

    def test_invalid_timeout(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_HTTP_TIMEOUT": "abc"})

    def test_zero_timeout(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_HTTP_TIMEOUT": "0"})

    def test_negative_retries(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_HTTP_MAX_RETRIES": "-1"})

    def test_invalid_log_level(self) -> None:
        with pytest.raises(ConfigurationError):
            load_settings({"MCP_LOG_LEVEL": "VERBOSE"})

    def test_log_level_case_insensitive(self) -> None:
        assert load_settings({"MCP_LOG_LEVEL": "warning"}).log_level == "WARNING"


# ---------------------------------------------------------------------------
# CLI overrides: CLI > env > defaults
# ---------------------------------------------------------------------------
class TestCliOverrides:
    def test_cli_host_overrides_env(self) -> None:
        env: dict[str, str] = {"MCP_BIND_HOST": "0.0.0.0", "MCP_PORT": "9999"}
        base = load_settings(env)
        overridden = apply_cli_overrides(base, host="127.0.0.1", port=None)
        assert overridden.bind_host == "127.0.0.1"
        assert overridden.port == 9999  # env port preserved
        assert overridden.auth_token is None

    def test_cli_port_overrides_env(self) -> None:
        base = load_settings({"MCP_PORT": "9999"})
        overridden = apply_cli_overrides(base, host=None, port=8123)
        assert overridden.port == 8123
        assert overridden.bind_host == DEFAULT_BIND_HOST  # default host preserved

    def test_cli_none_keeps_env(self) -> None:
        base = load_settings({"MCP_BIND_HOST": "10.0.0.5", "MCP_PORT": "7000"})
        overridden = apply_cli_overrides(base, host=None, port=None)
        assert overridden.bind_host == "10.0.0.5"
        assert overridden.port == 7000

    def test_cli_overrides_keep_other_fields(self) -> None:
        env: dict[str, str] = {
            "MCP_ADAPTER": "mock",
            "MCP_AUTH_TOKEN": "tok",
            "MCP_LOG_LEVEL": "DEBUG",
            "MCP_HTTP_TIMEOUT": "1.25",
            "MCP_HTTP_MAX_RETRIES": "3",
        }
        base = load_settings(env)
        overridden = apply_cli_overrides(base, host="1.2.3.4", port=5555)
        assert overridden.adapter == "mock"
        assert overridden.auth_token == "tok"
        assert overridden.log_level == "DEBUG"
        assert overridden.http_timeout_seconds == 1.25
        assert overridden.http_max_retries == 3


# ---------------------------------------------------------------------------
# is_loopback
# ---------------------------------------------------------------------------
class TestIsLoopback:
    def test_loopback_addresses(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            assert Settings(bind_host=host).is_loopback()

    def test_non_loopback(self) -> None:
        for host in ("0.0.0.0", "10.0.0.1", "192.168.1.10", "127.0.0.2"):
            assert not Settings(bind_host=host).is_loopback()


# ---------------------------------------------------------------------------
# validate_settings: fail closed
# ---------------------------------------------------------------------------
class TestValidateSettings:
    def test_loopback_without_token_ok(self) -> None:
        validate_settings(Settings(bind_host="127.0.0.1"))  # no exception

    def test_non_loopback_without_token_raises(self) -> None:
        with pytest.raises(ConfigurationError) as exc:
            validate_settings(Settings(bind_host="0.0.0.0"))
        assert "MCP_AUTH_TOKEN" in str(exc.value)

    def test_non_loopback_with_token_ok(self) -> None:
        validate_settings(Settings(bind_host="0.0.0.0", auth_token="tok"))

    def test_empty_token_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_settings(Settings(bind_host="127.0.0.1", auth_token="   "))

    def test_token_with_whitespace_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_settings(
                Settings(bind_host="127.0.0.1", auth_token="a b")
            )

    def test_token_like_value_pattern_raises(self) -> None:
        # A value that is not token-shaped (contains a URL scheme) is
        # treated as misconfiguration.
        with pytest.raises(ConfigurationError):
            validate_settings(
                Settings(bind_host="127.0.0.1", auth_token="https://example.com/x")
            )

    def test_valid_long_token_ok(self) -> None:
        validate_settings(
            Settings(bind_host="127.0.0.1", auth_token="a" * 32)
        )


# ---------------------------------------------------------------------------
# Upstream settings (VEOPS_* domain)
# ---------------------------------------------------------------------------
class TestUpstreamSettings:
    def test_defaults_empty(self) -> None:
        u = load_upstream_settings({})
        assert u.base_url is None
        assert u.key is None
        assert u.secret is None
        assert u.token is None

    def test_env_values(self) -> None:
        u = load_upstream_settings(
            {
                "VEOPS_BASE_URL": "https://cmdb.example.com/api",
                "VEOPS_KEY": "key-1",
                "VEOPS_SECRET": "sekret",
                "VEOPS_TOKEN": "legacy",
            }
        )
        assert u.base_url == "https://cmdb.example.com/api"
        assert u.key == "key-1"
        assert u.secret == "sekret"
        assert u.token == "legacy"

    def test_invalid_scheme_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            load_upstream_settings({"VEOPS_BASE_URL": "ftp://example.com"})

    def test_missing_host_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            load_upstream_settings({"VEOPS_BASE_URL": "http://"})

    def test_validate_upstream_settings(self) -> None:
        validate_upstream_settings(UpstreamSettings(base_url="http://x"))
        with pytest.raises(ConfigurationError):
            validate_upstream_settings(UpstreamSettings(base_url="not-a-url"))

    def test_domains_are_independent(self) -> None:
        # The two credential domains must not share a value; the loader
        # reads only its own env keys.
        s = load_settings({"MCP_AUTH_TOKEN": "inbound"})
        u = load_upstream_settings(
            {"VEOPS_KEY": "out-key", "VEOPS_SECRET": "outbound"}
        )
        assert s.auth_token == "inbound"
        assert u.key == "out-key"
        assert u.secret == "outbound"
        assert s.auth_token != u.secret
