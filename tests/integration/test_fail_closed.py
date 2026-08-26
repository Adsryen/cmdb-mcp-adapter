"""End-to-end fail-closed startup behavior (real subprocess).

These tests drive the real console entry point with a hostile
environment and assert that the process refuses to start (non-zero exit,
a configuration error on stderr, and nothing on stdout).  They cover the
two hard MVP rules:

* a **non-loopback bind without ``MCP_AUTH_TOKEN``** must fail closed;
* an **invalid upstream ``VEOPS_BASE_URL`` scheme** must fail closed.
"""

from __future__ import annotations

import signal as _signal
import subprocess
import sys
import time
import urllib.request

from tests.unit.test_cli import _clean_env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cmdb_mcp_adapter", *args],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )


def _start_and_check(
    args: list[str],
    env: dict[str, str],
    port: int,
    timeout: float = 15,
) -> tuple[int, str, str]:
    """Start the server, poll /healthz, then SIGINT.  Return
    (exit_code, stdout, stderr)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "cmdb_mcp_adapter", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=1
            ) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(0.3)
    if ready:
        proc.send_signal(_signal.SIGINT)
        proc.wait(timeout=15)
    if proc.poll() is None:
        proc.kill()
    out, err = proc.communicate()
    return proc.returncode or 0, out, err


class TestNonLoopbackWithoutToken:
    def test_http_fails_closed(self) -> None:
        env = _clean_env()
        env["MCP_BIND_HOST"] = "0.0.0.0"
        env["MCP_ADAPTER"] = "mock"
        result = _run(["http"], env=env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert "MCP_AUTH_TOKEN" in result.stderr
        # stdout must stay empty: no protocol frames, no diagnostic text.
        assert result.stdout == ""

    def test_stdin_fails_closed(self) -> None:
        env = _clean_env()
        env["MCP_BIND_HOST"] = "0.0.0.0"
        env["MCP_ADAPTER"] = "mock"
        result = _run(["stdio"], env=env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert result.stdout == ""

    def test_with_token_starts(self) -> None:
        # The same non-loopback bind *with* a valid token must not fail
        # with an auth-required error.  The server starts, we confirm
        # /healthz is up, then SIGINT it.
        env = _clean_env()
        env["MCP_BIND_HOST"] = "0.0.0.0"
        env["MCP_ADAPTER"] = "mock"
        env["MCP_AUTH_TOKEN"] = "a" * 32
        code, out, err = _start_and_check(
            ["http", "--port", "18322"], env, port=18322
        )
        # No configuration error: the server started and was shut down.
        assert "requires" not in err
        assert "MCP_AUTH_TOKEN" not in err
        assert "configuration" not in err
        assert out == "", f"stdout must be empty: {out!r}"


class TestInvalidUpstreamUrl:
    def test_http_fails_closed_on_bad_scheme(self) -> None:
        env = _clean_env()
        env["MCP_ADAPTER"] = "mock"
        env["VEOPS_BASE_URL"] = "ftp://cmdb.example.com"
        result = _run(["http"], env=env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert result.stdout == ""

    def test_stdio_fails_closed_on_bad_scheme(self) -> None:
        env = _clean_env()
        env["MCP_ADAPTER"] = "mock"
        env["VEOPS_BASE_URL"] = "not-a-scheme"
        result = _run(["stdio"], env=env)
        assert result.returncode != 0
        assert "configuration" in result.stderr
        assert result.stdout == ""

    def test_valid_url_does_not_fail_on_scheme(self) -> None:
        env = _clean_env()
        env["MCP_ADAPTER"] = "mock"
        env["VEOPS_BASE_URL"] = "https://cmdb.example.com/api"
        code, out, err = _start_and_check(
            ["http", "--port", "18323"], env, port=18323
        )
        assert "http(s) URL" not in err
        assert "configuration" not in err
        assert "http transport not implemented yet" not in err
        assert out == "", f"stdout must be empty: {out!r}"
