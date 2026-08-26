"""End-to-end startup behavior for the veops adapter (real subprocesses,
no network, no real credentials).  Covers the 0.2.0 fail-closed policy:

* ``MCP_ADAPTER=veops`` without ``VEOPS_BASE_URL`` -> non-zero exit, a
  configuration error, empty stdout.
* ``VEOPS_BASE_URL`` set but no credentials -> non-zero exit
  (mentioning ``VEOPS_KEY`` / ``VEOPS_SECRET``).
* legacy ``VEOPS_TOKEN`` set without the new pair -> non-zero exit with
  a *migration* error.
* full pair set -> stdio starts cleanly (exit 0 when stdin is closed).
"""

from __future__ import annotations

import subprocess
import sys


def _clean_env() -> dict[str, str]:
    import os

    out = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "VEOPS_"))}
    out["MCP_ADAPTER"] = "veops"
    return out


def _run_stdio(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


KEY = "veops-stdio-test-key-0000000000000000000"
SECRET = "veops-stdio-test-secret-0000000000000000"


class TestVeopsStartup:
    def test_missing_base_url_fails_closed(self) -> None:
        env = _clean_env()
        result = _run_stdio(env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert "VEOPS_BASE_URL" in result.stderr
        assert result.stdout == ""

    def test_missing_credentials_fails_closed(self) -> None:
        env = _clean_env()
        env["VEOPS_BASE_URL"] = "http://127.0.0.1:9"
        result = _run_stdio(env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert "VEOPS_KEY" in result.stderr
        assert "VEOPS_SECRET" in result.stderr
        assert result.stdout == ""

    def test_legacy_token_without_pair_is_migration_error(self) -> None:
        env = _clean_env()
        env["VEOPS_BASE_URL"] = "http://127.0.0.1:9"
        env["VEOPS_TOKEN"] = "veops-legacy-token-000000000000000000"
        result = _run_stdio(env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert "VEOPS_TOKEN" in result.stderr
        assert "VEOPS_KEY" in result.stderr
        assert result.stdout == ""

    def test_with_base_url_and_pair_starts(self) -> None:
        env = _clean_env()
        env["VEOPS_BASE_URL"] = "http://127.0.0.1:9"
        env["VEOPS_KEY"] = KEY
        env["VEOPS_SECRET"] = SECRET
        result = _run_stdio(env)
        # The server starts; when stdin is closed (input="") the stdio loop
        # ends and the process exits 0.  No configuration error.
        assert "configuration" not in result.stderr, result.stderr
        assert result.returncode == 0

    def test_bad_scheme_fails_closed(self) -> None:
        env = _clean_env()
        env["VEOPS_BASE_URL"] = "ftp://cmdb.example.com"
        env["VEOPS_KEY"] = KEY
        env["VEOPS_SECRET"] = SECRET
        result = _run_stdio(env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert result.stdout == ""
