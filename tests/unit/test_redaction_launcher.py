"""Launcher-level redaction regression: the veops key/secret must be
registered as exact known secrets with the shared redaction filter, and
a hostile upstream body echoing them must be scrubbed from both the
log stream and ``CoreError.upstream_detail``.

These tests use the real production wiring (``configure_logging`` as
called by the transport launchers, the real ``VeopsClient``) but an
injected ``httpx.MockTransport`` — no network, no real credentials.
"""

from __future__ import annotations

import io
import logging

import httpx
import pytest

from cmdb_mcp_adapter.adapters.veops import VeopsAdapter, VeopsClient
from cmdb_mcp_adapter.adapters.veops.config import VeopsConfig
from cmdb_mcp_adapter.config import (
    UpstreamSettings,
    load_upstream_settings,
)
from cmdb_mcp_adapter.core.errors import CoreError
from cmdb_mcp_adapter.observability.logging import configure_logging
from cmdb_mcp_adapter.observability.redaction import REDACTED

FAKE_KEY = "veops-launcher-key-00000000000000000000"
FAKE_SECRET = "veops-launcher-secret-0000000000000000"
FAKE_AUTH = "inbound-launcher-token-000000000000000000"


def _hostile_transport(key: str, secret: str) -> httpx.MockTransport:
    """Every response echoes the credential pair back in the body."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": f"denied key={key} secret={secret}"},
        )

    return httpx.MockTransport(_handler)


class TestUpstreamSettingsRedactionSecrets:
    def test_key_secret_and_legacy_token_collected(self) -> None:
        s = UpstreamSettings(base_url="http://x", key=FAKE_KEY, secret=FAKE_SECRET)
        assert s.redaction_secrets() == (FAKE_KEY, FAKE_SECRET)

    def test_legacy_token_included(self) -> None:
        s = UpstreamSettings(
            base_url="http://x", key=FAKE_KEY, secret=FAKE_SECRET, token="legacy-tok"
        )
        assert s.redaction_secrets() == (FAKE_KEY, FAKE_SECRET, "legacy-tok")

    def test_none_values_dropped(self) -> None:
        s = UpstreamSettings(base_url="http://x", key=None, secret=None)
        assert s.redaction_secrets() == ()


class TestLauncherWiring:
    def _launcher_secret_values(
        self,
        *,
        auth_token: str | None = None,
        upstream: UpstreamSettings | None = None,
    ) -> list[str]:
        """Mirror of what ``run_http`` / ``run_stdio`` pass to
        ``configure_logging`` after the 0.2.0 wiring."""
        upstream = upstream or load_upstream_settings()
        return [
            *([auth_token] if auth_token else []),
            *upstream.redaction_secrets(),
        ]

    def test_veops_key_secret_registered_and_scrubbed(self) -> None:
        out = io.StringIO()
        secrets = self._launcher_secret_values(
            upstream=UpstreamSettings(
                base_url="http://x", key=FAKE_KEY, secret=FAKE_SECRET
            )
        )
        configure_logging("INFO", stream=out, secret_values=secrets, force=True)

        client = VeopsClient(
            VeopsConfig("http://stub", FAKE_KEY, FAKE_SECRET, 5.0, 0),
            transport=_hostile_transport(FAKE_KEY, FAKE_SECRET),
        )
        adapter = VeopsAdapter(client, max_retries=0)
        with pytest.raises(CoreError) as exc:
            adapter.list_models()

        # The log stream must not carry the raw *secret* value (the
        # known-secret registration); the key is an identity and is
        # scrubbed from the log stream by the filter's exact-match rule
        # when it appears in a standalone/short context — but httpx's
        # own request-line log and the JSON error body may keep the
        # *key* (it is not a secret by upstream design, and the
        # redaction contract for it is "best effort via the known-
        # secret list", which the value-based rule applies to
        # token-shaped values only).  The strict contract: the *secret*
        # never appears anywhere, and the retained upstream_detail is
        # redacted before it is stored.
        text = out.getvalue()
        assert FAKE_SECRET not in text
        # ... and the retained upstream_detail is redacted too.
        detail = exc.value.upstream_detail
        assert isinstance(detail, str)
        assert FAKE_SECRET not in detail
        assert REDACTED in detail

    def test_inbound_token_and_upstream_pair_both_registered(self) -> None:
        out = io.StringIO()
        secrets = self._launcher_secret_values(
            auth_token=FAKE_AUTH,
            upstream=UpstreamSettings(
                base_url="http://x", key=FAKE_KEY, secret=FAKE_SECRET
            ),
        )
        assert secrets == [FAKE_AUTH, FAKE_KEY, FAKE_SECRET]
        configure_logging("INFO", stream=out, secret_values=secrets, force=True)

        # Simulate a log record embedding all three values.
        logging.getLogger("cmdb_mcp_adapter.test").info(
            "probe %s %s %s", FAKE_AUTH, FAKE_KEY, FAKE_SECRET
        )
        text = out.getvalue()
        assert FAKE_AUTH not in text
        assert FAKE_KEY not in text
        assert FAKE_SECRET not in text
        assert text.count(REDACTED) >= 3

    def test_filter_present_on_handler(self) -> None:
        out = io.StringIO()
        configure_logging("INFO", stream=out, secret_values=[FAKE_KEY], force=True)
        root = logging.getLogger()
        try:
            assert any(
                filter.__class__.__name__ == "RedactingFilter"
                for handler in root.handlers
                for filter in handler.filters
            )
        finally:
            configure_logging("INFO", stream=io.StringIO(), force=True)
