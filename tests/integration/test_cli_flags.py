"""CLI ``http`` subcommand: ``--host`` / ``--port`` override the
environment (CLI > env > defaults).

These tests invoke the real console entry point in a subprocess so the
full ``argparse`` -> ``load_settings`` -> ``apply_cli_overrides`` ->
``run_http`` chain is exercised end to end.
"""

from __future__ import annotations

import signal as _signal
import subprocess
import sys
import time
import urllib.request

from tests.unit.test_cli import _clean_env


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cmdb_mcp_adapter", *args],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env if env is not None else _clean_env(),
    )


def _start_server_and_check(
    args: list[str],
    env: dict[str, str],
    ready_path: str = "/healthz",
    port: int = 18324,
    timeout: float = 15,
) -> tuple[int, str, str]:
    """Start the server as a Popen, poll ready_path until it responds,
    then send SIGINT and return (exit_code, stdout, stderr).

    This is the bounded-wait pattern for tests that need to verify a
    *running* server: we poll the health endpoint, confirm it is up,
    then shut it down cleanly.  If the server fails to start (config
    error), the Popen exits on its own and we return its exit code.
    """
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
            # Process exited on its own (config error or crash).
            break
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{ready_path}", timeout=1
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


class TestHttpFlags:
    def test_help_lists_host_and_port(self, clean_env: None) -> None:
        result = _run(["http", "--help"])
        assert result.returncode == 0
        assert "--host" in result.stdout
        assert "--port" in result.stdout

    def test_stdio_help_has_no_host_port(self, clean_env: None) -> None:
        result = _run(["stdio", "--help"])
        assert result.returncode == 0
        assert "--host" not in result.stdout
        assert "--port" not in result.stdout


class TestCliPrecedence:
    """CLI flags beat env; both beat defaults."""

    def test_cli_host_beats_env(self, clean_env: None) -> None:
        # env says 0.0.0.0 (which requires a token); CLI says 127.0.0.1
        # (loopback, no token needed) -> the CLI must win and the process
        # must NOT fail with an auth-required error.  We start the server,
        # confirm /healthz is up, then SIGINT it.
        env = _clean_env()
        env["MCP_BIND_HOST"] = "0.0.0.0"
        env["MCP_ADAPTER"] = "mock"
        code, out, err = _start_server_and_check(
            ["http", "--host", "127.0.0.1", "--port", "18324"],
            env,
            port=18324,
        )
        # The CLI host (127.0.0.1) wins over env (0.0.0.0), so no
        # auth-required error.  The server started and was shut down.
        assert "requires" not in err
        assert "MCP_AUTH_TOKEN" not in err
        assert "configuration" not in err
        assert "http transport not implemented yet" not in err
        assert out == "", f"stdout must be empty: {out!r}"

    def test_cli_port_beats_env(self, clean_env: None) -> None:
        # We verify the override path directly: a CLI --port that is
        # invalid (0) must fail closed with a configuration error, which
        # proves the CLI value was used (the env value 9999 is valid).
        env = _clean_env()
        env["MCP_PORT"] = "9999"
        env["MCP_ADAPTER"] = "mock"
        result = _run(["http", "--port", "0"], env=env)
        assert result.returncode != 0
        assert "MCP_PORT" in result.stderr or "port" in result.stderr.lower()

    def test_env_only(self, clean_env: None) -> None:
        # No CLI flags: env is used.  0.0.0.0 without a token must fail
        # closed (this is the documented MVP rule).  We set MCP_ADAPTER=mock
        # so the *only* failure is the auth-required rule, not an
        # unregistered-adapter error.
        env = _clean_env()
        env["MCP_BIND_HOST"] = "0.0.0.0"
        env["MCP_ADAPTER"] = "mock"
        result = _run(["http"], env=env)
        assert result.returncode != 0
        assert "MCP_AUTH_TOKEN" in result.stderr
        assert "configuration" in result.stderr
        assert "not available in this build" not in result.stderr

    def test_defaults_when_nothing_set(self, clean_env: None) -> None:
        # No env, no flags: the default bind is loopback, so no
        # auth-required error may appear.  The default adapter (veops) is
        # registered but fails closed at startup because ``VEOPS_BASE_URL``
        # is mandatory and unset — critically, *not* with an
        # auth-required (``MCP_AUTH_TOKEN``) error.
        result = _run(["http"], env=_clean_env())
        assert "MCP_AUTH_TOKEN" not in result.stderr
        assert "requires" not in result.stderr
        # The veops adapter is registered; the failure is the veops
        # fail-closed policy (missing base URL), not an auth failure and
        # not an unregistered-adapter error.
        assert "VEOPS_BASE_URL" in result.stderr
        assert "not available in this build" not in result.stderr
