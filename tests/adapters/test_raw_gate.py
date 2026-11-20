"""0.4.0 contract: raw create/update share the field gate with intent tools;
sensitive columns WARN (not REJECT) on the raw channel; delete is opt-in."""
from __future__ import annotations

import pytest

from cmdb_mcp_adapter.core.errors import ConfigurationError, ValidationError
from tests.adapters._fakes import _RecordingTransport, build_adapter
from tests.adapters.test_adapter import CI_TYPES, _binding_routes

API = "/api/v0.1"
TICKET = "imp-selftest-20261120-t1"


class TestRawWriteGate:
    def test_update_without_ticket_rejected(self) -> None:
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): CI_TYPES, ("PUT", f"{API}/ci/514"): {"ci_id": 514}},
            {},
            require_signature=False,
        )
        adapter, _ = build_adapter(transport)
        with pytest.raises(ValidationError, match="TICKET_MISSING"):
            adapter.update_instance("quick_reference", "514", {"quick_re_title": "X"})
        assert not [r for r in transport.requests if r.method == "PUT"]

    def test_update_banned_symbol_rejected(self) -> None:
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): CI_TYPES, ("PUT", f"{API}/ci/514"): {"ci_id": 514}},
            {},
            require_signature=False,
        )
        adapter, _ = build_adapter(transport)
        with pytest.raises(ValidationError, match="BANNED_SYMBOL"):
            adapter.update_instance(
                "quick_reference", "514", {"quick_re_title": "⛔坏符号", "ticket_id": TICKET}
            )

    def test_raw_sensitive_column_warns_and_passes(self) -> None:
        # The sanctioned credential leg: plaintext key column rides raw with a
        # ticket; the gate must downgrade to WARN and let the PUT through.
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): CI_TYPES, ("PUT", f"{API}/ci/514"): {"ci_id": 514}},
            {},
            require_signature=False,
        )
        adapter, _ = build_adapter(transport)
        inst = adapter.update_instance(
            "credential", "514", {"credenti_secret_value": "plain-secret", "ticket_id": TICKET}
        )
        assert inst.instance_id == "514"

    def test_create_missing_four_piece_rejected(self) -> None:
        routes = {
            ("GET", f"{API}/ci_types"): CI_TYPES,
            **_binding_routes(),
            ("POST", f"{API}/ci"): {"ci_id": 900},
        }
        transport = _RecordingTransport(routes, {}, require_signature=False)
        adapter, _ = build_adapter(transport)
        with pytest.raises(ValidationError, match="FOUR_PIECE_COLUMN_MISSING"):
            adapter.create_instance(
                "credential", {"credenti_key_id": "ACC-9999", "ticket_id": TICKET}
            )


class TestDeleteOptIn:
    def test_delete_blocked_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VEOPS_ALLOW_DELETE", raising=False)
        transport = _RecordingTransport({("DELETE", f"{API}/ci/514"): (204, None)}, {})
        adapter, _ = build_adapter(transport)
        with pytest.raises(ConfigurationError, match="VEOPS_ALLOW_DELETE"):
            adapter.delete_instance("quick_reference", "514")
        assert not [r for r in transport.requests if r.method == "DELETE"]
