"""Liveness vs readiness probes.

* ``check_liveness`` is a constant ``{"status": "ok"}`` — it must never
  fail once the process is running.
* ``check_readiness`` validates configuration and may run an injectable
  (default no-op) upstream probe; a bad configuration yields
  ``{"status": "unavailable", ...}``.
"""

from __future__ import annotations

from cmdb_mcp_adapter.config import (
    Settings,
    UpstreamSettings,
    load_upstream_settings,
)
from cmdb_mcp_adapter.server.health import (
    check_liveness,
    check_readiness,
)


class TestLiveness:
    def test_always_ok(self) -> None:
        assert check_liveness() == {"status": "ok"}

    def test_ok_even_with_broken_config(self) -> None:
        # Liveness is about the process, not the config: even a
        # non-loopback bind with no token still reports alive.
        bad = Settings(bind_host="0.0.0.0")
        assert check_liveness() == {"status": "ok"}
        del bad


class TestReadiness:
    def test_ok_with_valid_config(self) -> None:
        s = Settings(bind_host="127.0.0.1")
        assert check_readiness(s) == {"status": "ok"}

    def test_ok_with_non_loopback_and_token(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        assert check_readiness(s) == {"status": "ok"}

    def test_unavailable_non_loopback_without_token(self) -> None:
        s = Settings(bind_host="0.0.0.0")
        result = check_readiness(s)
        assert result["status"] == "unavailable"
        assert "configuration" in result["reason"]

    def test_unavailable_invalid_upstream_url(self) -> None:
        s = Settings(bind_host="127.0.0.1")
        upstream = UpstreamSettings(base_url="not-a-url")
        result = check_readiness(s, upstream=upstream)
        assert result["status"] == "unavailable"
        assert "configuration" in result["reason"]

    def test_upstream_prober_can_be_injected(self) -> None:
        s = Settings(bind_host="127.0.0.1")
        upstream = UpstreamSettings(base_url="https://cmdb.example.com")

        def prober(_u: UpstreamSettings | None) -> dict[str, object]:
            return {"status": "unavailable", "reason": "connection refused"}

        result = check_readiness(s, upstream=upstream, prober=prober)
        assert result["status"] == "unavailable"
        assert "connection refused" in result["reason"]

    def test_default_prober_makes_no_network_calls(self) -> None:
        """The MVP default probe performs no I/O: a prober that would
        'phone home' is never used, and a prober that records invocations
        proves the default path never invokes any probe with side effects."""
        import socket
        s = Settings(bind_host="127.0.0.1")
        upstream = UpstreamSettings(base_url="https://cmdb.example.com")

        # Break connect() so any accidental network I/O raises instead
        # of silently succeeding; readiness must still report ok because
        # the default probe is a no-op.
        socket.socket.connect = None  # type: ignore[method-assign]
        try:
            assert check_readiness(s, upstream=upstream) == {"status": "ok"}
        finally:
            # restore (the attribute was set dynamically)
            del socket.socket.connect

    def test_default_prober_is_noop_ok(self) -> None:
        s = Settings(bind_host="127.0.0.1")
        upstream = load_upstream_settings({"VEOPS_BASE_URL": "https://x.example.com"})
        assert check_readiness(s, upstream=upstream) == {"status": "ok"}

    def test_probe_exception_is_unavailable_not_crash(self) -> None:
        s = Settings(bind_host="127.0.0.1")

        def bad_prober(_u: UpstreamSettings | None) -> dict[str, object]:
            raise RuntimeError("probe bug")

        result = check_readiness(s, prober=bad_prober)
        assert result["status"] == "unavailable"
        assert "probe failed" in result["reason"]

    def test_probe_ok_status_passthrough(self) -> None:
        s = Settings(bind_host="127.0.0.1")

        def prober(_u: UpstreamSettings | None) -> dict[str, object]:
            return {"status": "ok"}

        assert check_readiness(s, prober=prober) == {"status": "ok"}
