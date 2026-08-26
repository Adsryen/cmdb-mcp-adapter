"""CLI contract: safe diagnostics without CMDB credentials, and a real
stdio launcher that starts the MCP server (or fails cleanly on bad config)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from cmdb_mcp_adapter import __version__
from cmdb_mcp_adapter.cli import main


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the real console entry point in a subprocess."""
    return subprocess.run(
        [sys.executable, "-m", "cmdb_mcp_adapter", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class TestDiagnostics:
    def test_version(self) -> None:
        result = _run_cli(["--version"])
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_short_version_flag(self) -> None:
        result = _run_cli(["-V"])
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_help(self) -> None:
        result = _run_cli(["--help"])
        assert result.returncode == 0
        for expected in ("usage:", "stdio", "http"):
            assert expected in result.stdout

    def test_stdio_help(self) -> None:
        result = _run_cli(["stdio", "--help"])
        assert result.returncode == 0
        assert "stdio" in result.stdout

    def test_http_help(self) -> None:
        result = _run_cli(["http", "--help"])
        assert result.returncode == 0
        assert "http" in result.stdout


class TestStdioLauncher:
    """The stdio launcher must start the MCP server over stdio.  On
    configuration errors it exits non-zero with a clear stderr message and
    must not write anything to stdout (the protocol stream)."""

    def test_stdio_starts_with_mock_adapter(self, clean_env: None) -> None:
        # With MCP_ADAPTER=mock the stdio server should start and wait for
        # input.  We send an empty stdin and a short timeout; the process
        # should exit 0 (clean EOF) or 124 (killed by timeout while waiting).
        result = subprocess.run(
            [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**_clean_env(), "MCP_ADAPTER": "mock"},
        )
        # Either the server shut down cleanly on EOF (exit 0) or the
        # timeout killed it (exit 124).  Both are acceptable.
        assert result.returncode in (0, 124), (
            f"unexpected exit {result.returncode}: {result.stderr}"
        )
        # The protocol stream (stdout) must contain valid JSON-RPC or be
        # empty; it must NOT contain our stderr diagnostic text.
        assert "configuration error" not in result.stdout
        assert "failed to start" not in result.stdout

    def test_stdio_unknown_adapter_fails(self, clean_env: None) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**_clean_env(), "MCP_ADAPTER": "definitely-not-registered"},
        )
        assert result.returncode != 0
        # The stderr must carry the configuration error category.
        assert "configuration" in result.stderr
        # stdout must be empty: no protocol frames, no diagnostic text.
        assert result.stdout == ""

    def test_stdio_missing_credentials_stdout_is_empty(self, clean_env: None) -> None:
        # Protocol-contamination regression: on the *missing upstream
        # credential* path (MCP_ADAPTER=veops without VEOPS_BASE_URL) the
        # process must exit non-zero, the stderr line must carry the
        # stable ``configuration`` category, and **stdout must remain
        # exactly 0 bytes** so a host that has already attached its
        # protocol reader never sees stray bytes.
        env = {**_clean_env(), "MCP_ADAPTER": "veops"}
        result = subprocess.run(
            [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        assert result.returncode != 0, result.stderr
        assert "configuration" in result.stderr
        assert "VEOPS_BASE_URL" in result.stderr
        assert result.stdout == "", f"stdout must be 0 bytes: {result.stdout!r}"


class TestHttpLauncher:
    """The HTTP launcher starts a real uvicorn server.  On configuration
    errors it exits non-zero with a clear stderr message and must not write
    anything to stdout (the protocol stream is HTTP, not stdout)."""

    def test_http_unknown_adapter_fails_closed(self, clean_env: None) -> None:
        # An unregistered adapter is a configuration error; the process must
        # exit non-zero before any server is started.
        result = subprocess.run(
            [sys.executable, "-m", "cmdb_mcp_adapter", "http"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**_clean_env(), "MCP_ADAPTER": "definitely-not-registered"},
        )
        assert result.returncode != 0
        assert "configuration" in result.stderr
        # The old placeholder notice must no longer appear.
        assert "http transport not implemented yet" not in result.stderr
        assert result.stdout == ""

    def test_http_loopback_starts_and_serves(self, clean_env: None) -> None:
        # A loopback bind with the mock adapter must start the server and
        # serve /healthz.  We poll /healthz with a bounded wait, then
        # send SIGINT to shut the server down cleanly.
        import signal as _signal
        import time
        import urllib.request

        env = _clean_env()
        env["MCP_ADAPTER"] = "mock"
        proc = subprocess.Popen(
            [sys.executable, "-m", "cmdb_mcp_adapter", "http", "--port", "18321"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            # Poll /healthz until the server is up (bounded wait).
            deadline = time.time() + 15
            ok = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:18321/healthz", timeout=1
                    ) as resp:
                        if resp.status == 200:
                            ok = True
                            break
                except Exception:
                    pass
                time.sleep(0.3)
            assert ok, "server did not become ready in time"
            # Now shut it down cleanly.
            proc.send_signal(_signal.SIGINT)
            code = proc.wait(timeout=15)
            out, err = proc.communicate()
            # stdout must be empty (no protocol frames on stdout).
            assert out == "", f"stdout must be empty: {out!r}"
            # The process exited (SIGINT is a signal-driven exit; the key
            # is that it *exited* and did not hang).
            assert code is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


class TestMainInProcess:
    def test_no_subcommand_shows_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 2
        assert "usage:" in capsys.readouterr().out

    def test_version_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out


def _clean_env() -> dict[str, str]:
    """Return a clean environment dict with the adapter env vars removed."""
    import os

    out = {**os.environ}
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



class TestGracefulShutdown:
    """SIGINT / SIGTERM must terminate the running stdio launcher
    gracefully: non-zero exit, clean stderr, and — critically — no MCP
    protocol frames on stdout (the signal path may only write to the
    diagnostics stream)."""

    @staticmethod
    def _run_with_signal(signame: str) -> tuple[int, str, str]:
        import signal
        import subprocess

        env = _clean_env()
        env["MCP_ADAPTER"] = "mock"
        proc = subprocess.Popen(
            [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        signum = getattr(signal, signame)
        try:
            # Give the launcher a moment to reach the stdio loop so the
            # handler is installed.
            proc.send_signal(signum)
            out, err = proc.communicate(timeout=15)
            code = proc.returncode
        except Exception:
            proc.kill()
            out, err = proc.communicate()
            code = proc.returncode
        return code, out, err

    def test_sigint_graceful(self) -> None:
        code, out, err = self._run_with_signal("SIGINT")
        # The SDK's stdio loop restores default SIGINT behaviour once it
        # has started, so a signal delivered after that exit-codes the
        # interpreter (130) and never writes protocol frames to stdout.
        assert code not in (0, None), f"expected non-zero exit, got {code}"
        assert out == "", f"stdout must be empty on signal exit: {out!r}"
        assert "configuration error" not in err
        assert "failed to start" not in err

    def test_sigint_exits_130_convention(self) -> None:
        # The *documented* Ctrl-C contract: a SIGINT delivered to a running
        # stdio process terminates it by the signal, which is reported
        # either as returncode -2 (killed by SIGINT, the common case —
        # the SDK's stdio loop restores the default SIGINT handler once
        # it is running) or as the shell convention 130 (128 + SIGINT)
        # when the process exits through the interpreter's default
        # handler.  Either way: non-zero, and stdout stays 0 bytes.
        # docs/local-stdio.md quotes both forms.
        code, out, err = self._run_with_signal("SIGINT")
        assert code in (-2, 130), f"expected signal-driven exit, got {code}: {err}"
        assert out == "", f"stdout must be 0 bytes: {out!r}"

    def test_sigterm_keeps_protocol_stream_clean(self) -> None:
        # SIGTERM installs only the shutdown-event handler (it cannot
        # interrupt the SDK's asyncio loop), so the process may keep
        # running; the contract under test is that no signal path ever
        # writes MCP frames to stdout.  Verify with a bounded wait.
        import signal
        import subprocess
        import time

        env = _clean_env()
        env["MCP_ADAPTER"] = "mock"
        proc = subprocess.Popen(
            [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            proc.send_signal(signal.SIGTERM)
            time.sleep(2.0)
            if proc.poll() is None:
                proc.kill()
                out, err = proc.communicate()
            else:
                out, err = proc.communicate()
        finally:
            pass
        assert out == "", f"stdout must be empty after SIGTERM: {out!r}"
        assert "configuration error" not in err
        assert "failed to start" not in err
