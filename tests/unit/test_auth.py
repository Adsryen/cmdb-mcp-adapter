"""Inbound authentication policy (loopback open, non-loopback fail-closed).

The decision function :func:`cmdb_mcp_adapter.server.auth.authorize` is
pure: it takes validated settings and a request-header mapping and
returns ``None`` (authorized) or an error message.  This module tests
every combination of bind type x token presence x header state.
"""

from __future__ import annotations

from cmdb_mcp_adapter.config import Settings
from cmdb_mcp_adapter.server.auth import authorize, make_auth_middleware


class TestAuthorize:
    def test_loopback_no_token_allows(self) -> None:
        s = Settings(bind_host="127.0.0.1")
        assert authorize(s, {}) is None

    def test_loopback_with_token_still_allows(self) -> None:
        # A loopback bind does not require a token; if one is configured
        # we still allow (the local host is the trust boundary).
        s = Settings(bind_host="127.0.0.1", auth_token="t")
        assert authorize(s, {}) is None

    def test_non_loopback_no_token_rejects(self) -> None:
        s = Settings(bind_host="0.0.0.0")
        err = authorize(s, {})
        assert err is not None
        assert "token" in err

    def test_non_loopback_missing_header_rejects(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        err = authorize(s, {})
        assert err is not None
        assert "missing" in err

    def test_non_loopback_wrong_token_rejects(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="correct")
        err = authorize(s, {"Authorization": "Bearer wrong"})
        assert err is not None
        assert "invalid" in err

    def test_non_loopback_correct_token_allows(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="correct")
        assert authorize(s, {"Authorization": "Bearer correct"}) is None

    def test_bearer_scheme_case_insensitive(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        assert authorize(s, {"Authorization": "bearer t"}) is None
        assert authorize(s, {"Authorization": "BEARER t"}) is None

    def test_non_bearer_scheme_rejects(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        err = authorize(s, {"Authorization": "Basic t"})
        assert err is not None
        assert "missing" in err  # a non-bearer header is treated as absent

    def test_header_name_case_insensitive(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        assert authorize(s, {"authorization": "Bearer t"}) is None

    def test_non_string_header_value_rejects(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        err = authorize(s, {"Authorization": 12345})  # type: ignore[dict-item]
        assert err is not None

    def test_make_auth_middleware_matches_authorize(self) -> None:
        s = Settings(bind_host="0.0.0.0", auth_token="t")
        mw = make_auth_middleware(s)
        assert mw({"Authorization": "Bearer t"}) is None
        assert mw({"Authorization": "Bearer nope"}) is not None
        assert mw({}) is not None
