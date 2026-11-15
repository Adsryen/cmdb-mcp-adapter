"""Shared test fixtures: keep the environment free of adapter settings."""

import pytest

ENV_KEYS = (
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
    "VEOPS_READONLY",
    "CMDB_RULES_PATH",
    "CMDB_RULES_SOURCE_PATH",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
