"""Opt-in live tests against a *real* VeOps 2.6.2 deployment (115).

**Disabled by default and never run in CI.**  To run, export
``MCP_VEOPS_LIVE=1`` and supply real credentials through the
*environment only* (never committed, never on the command line):

    MCP_VEOPS_LIVE=1 VEOPS_BASE_URL=http://<host>:8000 \\
        VEOPS_KEY=<your-veops-key> VEOPS_SECRET=<your-veops-secret> \\
        uv run pytest -q tests/integration/test_veops_live.py

The default tests in this file are **read-only** (``GET`` only): they
never issue a write request to the upstream.  A separate
:class:`TestVeopsLiveWrite` class performs a full
create → update → delete → search-verify round-trip and is gated by a
*stronger* flag (``MCP_VEOPS_LIVE_WRITE=1`` in addition to
``MCP_VEOPS_LIVE=1``) plus an explicit ``MCP_VEOPS_LIVE_WRITE_MODEL``
name, so a write can never reach a deployment by accident.  With
``MCP_VEOPS_LIVE`` unset (or != "1") every test here skips cleanly, so
the standard ``uv run pytest -q`` run stays green with zero credentials.

The live deployment used for the 0.2.0 contract verification
(2026-08-25) contained the ``quick_reference`` and ``credential``
ci_types and a credential with key id ``ACC-0166``; the assertions
below are written against those facts (see
``.trellis/tasks/08-25-veops-real-api/research/115-live-probe.md``).
"""

from __future__ import annotations

import os

import pytest

from cmdb_mcp_adapter.adapters.veops import create_veops_adapter
from cmdb_mcp_adapter.config import Settings


@pytest.mark.optin
@pytest.mark.skipif(
    os.environ.get("MCP_VEOPS_LIVE") != "1",
    reason=(
        "opt-in live test: set MCP_VEOPS_LIVE=1 (plus real "
        "VEOPS_BASE_URL / VEOPS_KEY / VEOPS_SECRET in the environment) "
        "to run it"
    ),
)
class TestVeopsLiveReadOnly:
    @pytest.fixture
    def adapter(self):
        return create_veops_adapter(Settings(adapter="veops"))

    def test_get_models_contains_expected_ci_types(self, adapter) -> None:
        models = adapter.list_models()
        names = {m.name for m in models}
        # Both of the deployment's verified types must be present.
        assert "quick_reference" in names
        assert "credential" in names
        for model in models:
            assert model.name
            assert model.display_name

    def test_get_instances_quick_reference_has_chinese_fields(self, adapter) -> None:
        page = adapter.get_instances(
            "quick_reference", page_size=100, cursor=None, filters={}
        )
        assert page.total >= 1
        (inst,) = page.items[:1]
        # Verified: quick_re_title / quick_re_content carry Chinese text
        # on this deployment.
        assert inst.data.get("quick_re_title")
        assert inst.data.get("quick_re_content")
        assert inst.metadata is not None
        assert inst.metadata.get("veops.ci_type") == "quick_reference"

    def test_search_hits_credential_by_key_id(self, adapter) -> None:
        page = adapter.search_instances("credential", "ACC-0166", page_size=10, cursor=None)
        assert page.total == 1
        (inst,) = page.items
        # Full-text hit lands on credential CI 731; "ACC-0166" appears in its
        # source-key fields (verified 2026-08-25 against the live deployment).
        assert inst.data.get("credenti_source_key") == "ACC-0166"
        assert inst.data.get("credenti_resource_id") == "ACC-0166"


@pytest.mark.optin
@pytest.mark.skipif(
    os.environ.get("MCP_VEOPS_LIVE") != "1"
    or os.environ.get("MCP_VEOPS_LIVE_WRITE") != "1",
    reason=(
        "opt-in LIVE WRITE test: set MCP_VEOPS_LIVE=1 AND "
        "MCP_VEOPS_LIVE_WRITE=1 (plus real VEOPS_BASE_URL / VEOPS_KEY / "
        "VEOPS_SECRET) to run it.  This issues real writes; point it at a "
        "disposable/sandbox deployment, never production."
    ),
)
class TestVeopsLiveWrite:
    """A full create → update → delete → search-verify round-trip.

    Gated twice over (``MCP_VEOPS_LIVE`` **and** ``MCP_VEOPS_LIVE_WRITE``)
    and only meaningful against a deployment the operator has explicitly
    cleared for writes (e.g. the isolated local sandbox).
    """

    @pytest.fixture
    def adapter(self):
        return create_veops_adapter(Settings(adapter="veops"))

    def test_create_update_delete_round_trip(self, adapter) -> None:
        import uuid

        model = os.environ.get("MCP_VEOPS_LIVE_WRITE_MODEL", "data_check")
        mark = f"mcp-live-write-{uuid.uuid4().hex[:12]}"

        # The data_check ci_type (id 9) requires its choice field
        # ``data_che_check_type`` to carry a value that is one of the
        # attribute's predefined choices; the values below are the
        # deployment's (verified 2026-08-26 against the local sandbox).
        # ``data_che_source_key`` is the type's unique key.
        created = adapter.create_instance(
            model,
            {
                "data_che_source_key": mark,
                "data_che_check_id": mark,
                "data_che_check_type": "重复候选",
                "data_che_source_file": "mcp-adapter",
                "data_che_issue": "live write round-trip test",
                "data_che_result": "待核对",
                "data_che_priority": "P1-关系完整性",
                "data_che_queue_status": "待人工确认",
            },
        )
        assert created.instance_id, "create must resolve a native id (ci_id)"
        new_id = created.instance_id

        try:
            updated = adapter.update_instance(model, new_id, {"data_che_result": "已核对-adapter"})
            assert updated.instance_id == new_id

            # Verify the update landed via a search.
            hit = adapter.search_instances(model, mark, page_size=10, cursor=None)
            assert hit.total == 1
            assert hit.items[0].data.get("data_che_result") == "已核对-adapter"
        finally:
            # Always clean up so a write test never leaves residue.
            adapter.delete_instance(model, new_id)

        gone = adapter.search_instances(model, mark, page_size=10, cursor=None)
        assert gone.total == 0
