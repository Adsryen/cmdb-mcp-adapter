"""Adapter-level tests for :class:`VeopsAdapter` against a scripted fake
transport that emulates the *real* VeOps 2.6.2 contract (no network).

Covers the seven protocol methods, the verified quirks (full-page
instance listing, search pagination + client-side filtering, list-wrapped
ci_types, untyped second_cis), error-category preservation, and the
read-vs-write retry policy.  The transport enforces ``_key``/``_secret``
signature verification with the production :func:`sign` — a signing
regression shows up as 401 in these tests.
"""

from __future__ import annotations

import httpx
import pytest

from cmdb_mcp_adapter.adapters.veops import VeopsAdapter
from cmdb_mcp_adapter.core.errors import (
    ErrorCategory,
    UnsupportedOperationError,
    ValidationError,
)
from tests.adapters._fakes import (
    FAKE_KEY,
    _RecordingTransport,
    build_adapter,
    build_client,
    make_config,
)

API = "/api/v0.1"

# The deployment's ci_types dictionary (id + name + alias only — the
# real payload carries more; the mapper only needs these).
CI_TYPES = {
    "numfound": 2,
    "ci_types": [
        {"id": 1, "name": "quick_reference", "alias": "快速查询"},
        {"id": 7, "name": "credential", "alias": "配置与凭据"},
    ],
}

# The global attribute dictionary (subset of the real one).
ATTRIBUTES = {
    "attributes": [
        {"name": "quick_re_title", "alias": "快速查询与联查", "value_type": "2"},
        {"name": "quick_re_content", "alias": "内容", "value_type": "2"},
        {"name": "credenti_key_id", "alias": "凭据编号", "value_type": "2"},
        {"name": "credenti_environment", "alias": "环境", "value_type": "2"},
        {"name": "server_credential_id", "alias": "凭据编号", "value_type": "2"},
    ],
}

CIS = {
    "cis": [
        {
            "_id": 514,
            "_type": 1,
            "_updated_at": "2026-08-07 18:04:42",
            "_updated_by": "admin",
            "ci_type": "quick_reference",
            "ci_type_alias": "快速查询",
            "unique": "quick_re_source_key",
            "unique_alias": "导入唯一键",
            "quick_re_title": "用途",
            "quick_re_content": "内容A",
        },
        {
            "_id": 515,
            "_type": 1,
            "_updated_at": "2026-08-07 18:05:00",
            "_updated_by": "admin",
            "ci_type": "quick_reference",
            "ci_type_alias": "快速查询",
            "unique": "quick_re_source_key",
            "unique_alias": "导入唯一键",
            "quick_re_title": "别名",
            "quick_re_content": "内容B",
        },
    ]
}


class TestListModels:
    def test_list_models_maps_ci_types(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        models = adapter.list_models()
        assert [m.name for m in models] == ["quick_reference", "credential"]
        assert models[0].display_name == "快速查询"
        # credential's attributes resolve via the credenti_* convention.
        assert [f.name for f in models[1].fields] == [
            "credenti_key_id",
            "credenti_environment",
        ]

    def test_list_models_attributes_unavailable_degrades(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): (500, {"error": "boom"}),
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        models = adapter.list_models()
        # Best-effort: models still listed, fields empty.
        assert [m.name for m in models] == ["quick_reference", "credential"]
        assert all(m.fields == () for m in models)


class TestGetInstances:
    def _transport(self, **overrides):
        handlers = {
            ("GET", f"{API}/ci_types"): CI_TYPES,
            ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            ("GET", f"{API}/ci/type/1"): CIS,
        }
        handlers.update(overrides)
        return _RecordingTransport(handlers, {})

    def test_full_page_single_response(self) -> None:
        adapter, _ = build_adapter(self._transport())
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        assert len(page.items) == 2
        assert page.total == 2
        assert page.page == 1
        # The upstream is not paginated: no next cursor is ever issued.
        assert page.next_cursor is None
        assert page.has_next is False

    def test_instance_split(self) -> None:
        adapter, _ = build_adapter(self._transport())
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        (inst,) = [i for i in page.items if i.instance_id == "514"]
        # Declared fields land in data.
        assert inst.data == {"quick_re_title": "用途", "quick_re_content": "内容A"}
        # Vendor metadata is quarantined under the veops. prefix.
        assert inst.metadata is not None
        assert inst.metadata["veops._type"] == 1
        assert inst.metadata["veops.ci_type"] == "quick_reference"
        assert inst.metadata["veops.unique"] == "quick_re_source_key"
        assert inst.metadata["veops.id"] == "514"

    def test_cursor_rejected(self) -> None:
        adapter, _ = build_adapter(self._transport())
        with pytest.raises(ValidationError):
            adapter.get_instances(
                "quick_reference", page_size=10, cursor="veops-search-page-2", filters={}
            )

    def test_unknown_model_is_validation_error(self) -> None:
        adapter, _ = build_adapter(self._transport())
        with pytest.raises(ValidationError) as exc:
            adapter.get_instances("nope", page_size=10, cursor=None, filters={})
        assert "nope" in exc.value.message

    def test_filters_are_client_side(self) -> None:
        adapter, _ = build_adapter(self._transport())
        page = adapter.get_instances(
            "quick_reference",
            page_size=10,
            cursor=None,
            filters={"quick_re_title": "别名"},
        )
        assert [i.instance_id for i in page.items] == ["515"]
        assert page.total == 1

    def test_filters_none_matches_missing_key(self) -> None:
        # Instance 515 has no quick_re_content key? give it one missing:
        cis = {
            "cis": [
                {
                    "_id": 514,
                    "_type": 1,
                    "ci_type": "quick_reference",
                    "quick_re_title": "用途",
                    "quick_re_content": "内容A",
                },
                {"_id": 515, "_type": 1, "ci_type": "quick_reference"},
            ]
        }
        handlers = {
            ("GET", f"{API}/ci_types"): CI_TYPES,
            ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            ("GET", f"{API}/ci/type/1"): cis,
        }
        adapter, _ = build_adapter(_RecordingTransport(handlers, {}))
        page = adapter.get_instances(
            "quick_reference",
            page_size=10,
            cursor=None,
            filters={"quick_re_content": None},
        )
        assert [i.instance_id for i in page.items] == ["515"]


class TestSearch:
    def _transport(self, page_payload):
        handlers = {
            ("GET", f"{API}/ci_types"): CI_TYPES,
            ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            ("GET", f"{API}/ci/search"): page_payload,
        }
        return _RecordingTransport(handlers, {})

    def test_search_first_page(self) -> None:
        payload = {
            "numfound": 1,
            "total": 1,
            "page": 1,
            "result": [
                {
                    "_id": 731,
                    "_type": 7,
                    "ci_type": "credential",
                    "ci_type_alias": "配置与凭据",
                    "credenti_key_id": "ACC-0166",
                }
            ],
        }
        adapter, client = build_adapter(self._transport(payload))
        page = adapter.search_instances("credential", "ACC-0166", page_size=10, cursor=None)
        assert [i.instance_id for i in page.items] == ["731"]
        assert page.total == 1
        assert page.next_cursor is None

    def test_search_sends_q_ci_type_page(self) -> None:
        payload = {"numfound": 0, "total": 0, "page": 1, "result": []}
        transport = self._transport(payload)
        adapter, client = build_adapter(transport)
        adapter.search_instances("credential", "ACC-0166", page_size=10, cursor=None)
        search_reqs = [r for r in transport.requests if r.url.path.endswith("/ci/search")]
        (req,) = search_reqs
        query = dict(req.url.params)
        assert query["q"] == "ACC-0166"
        assert query["ci_type"] == "7"
        assert query["page"] == "1"
        assert query["page_size"] == "10"

    def test_search_next_cursor_from_total(self) -> None:
        payload = {
            "numfound": 2,
            "total": 5,
            "page": 1,
            "result": [
                {"_id": 1, "ci_type": "credential", "credenti_key_id": "a"},
                {"_id": 2, "ci_type": "credential", "credenti_key_id": "b"},
            ],
        }
        adapter, _ = build_adapter(self._transport(payload))
        page = adapter.search_instances("credential", "", page_size=2, cursor=None)
        assert page.next_cursor == "veops-search-page-2"
        assert page.has_next is True

    def test_search_page_two_via_cursor(self) -> None:
        p1 = {"numfound": 1, "total": 2, "page": 1, "result": [{"_id": 1}]}
        p2 = {"numfound": 1, "total": 2, "page": 2, "result": [{"_id": 2}]}

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=p1 if page == 1 else p2)

        handlers = {
            ("GET", f"{API}/ci_types"): CI_TYPES,
            ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            ("GET", f"{API}/ci/search"): handler,
        }
        adapter, _ = build_adapter(_RecordingTransport(handlers, {}))
        first = adapter.search_instances("credential", "", page_size=1, cursor=None)
        assert first.next_cursor == "veops-search-page-2"
        second = adapter.search_instances(
            "credential", "", page_size=1, cursor=first.next_cursor
        )
        assert second.items[0].instance_id == "2"
        assert second.next_cursor is None

    def test_bad_cursor_is_validation_error(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        with pytest.raises(ValidationError):
            adapter.search_instances("credential", "x", page_size=10, cursor="garbage")


class TestRelations:
    def test_get_relations_second_cis(self) -> None:
        payload = {
            "numfound": 1,
            "total": 1,
            "page": 1,
            "second_cis": [
                {
                    "_id": 1,
                    "_type": 3,
                    "ci_type": "server",
                    "ci_type_alias": "服务器",
                    "server_credential_id": "ACC-0166",
                }
            ],
        }
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_relations/731/second_cis"): payload}, {}
        )
        adapter, _ = build_adapter(transport)
        rels = adapter.get_relations("credential", "731")
        assert len(rels) == 1
        assert rels[0].source_instance_id == "731"
        assert rels[0].target_instance_id == "1"
        # The upstream response carries no relation_type_id (verified):
        # the type stays empty rather than being invented.
        assert rels[0].relation_type == ""
        assert rels[0].metadata["veops.ci_type"] == "server"

    def test_relations_empty(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_relations/1/second_cis"): {
                    "numfound": 0,
                    "total": 0,
                    "page": 1,
                    "second_cis": [],
                }
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        assert adapter.get_relations("credential", "1") == []


class TestWriteMethods:
    def test_create_instance_sends_ci_type_and_fields(self) -> None:
        # The 2.6.2 write response shape is not contractually fixed;
        # the mapper accepts an {"id": ...} stub.
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
                ("POST", f"{API}/ci"): {"ci_id": 900},
            },
            {},
            require_signature=False,  # body-signed POSTs: verify below
        )
        adapter, client = build_adapter(transport)
        inst = adapter.create_instance("credential", {"credenti_key_id": "ACC-9999"})
        assert inst.instance_id == "900"
        (req,) = [r for r in transport.requests if r.method == "POST"]
        import json as _json

        body = _json.loads(req.content)
        assert body["ci_type"] == 7
        assert body["credenti_key_id"] == "ACC-9999"
        # The credentials ride in the JSON body and the signature covers
        # the body's scalar values (the upstream reads request.values =
        # request.json for a JSON-body write).
        from cmdb_mcp_adapter.adapters.veops import signing
        from tests.adapters._fakes import FAKE_SECRET

        assert body["_key"] == FAKE_KEY
        assert body["_secret"] == signing.sign(
            "/api/v0.1/ci",
            FAKE_SECRET,
            {k: v for k, v in body.items() if k not in ("_key", "_secret")},
        )
        assert "_key" not in dict(req.url.params)

    def test_create_instance_echoes_flat_instance(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
                ("POST", f"{API}/ci"): {
                    "_id": 901,
                    "_type": 7,
                    "ci_type": "credential",
                    "credenti_key_id": "ACC-9999",
                },
            },
            {},
            require_signature=False,
        )
        adapter, _ = build_adapter(transport)
        inst = adapter.create_instance("credential", {"credenti_key_id": "ACC-9999"})
        assert inst.instance_id == "901"
        assert inst.data["credenti_key_id"] == "ACC-9999"

    def test_update_instance(self) -> None:
        transport = _RecordingTransport(
            {
                ("PUT", f"{API}/ci/514"): {"ci_id": 514},
            },
            {},
            require_signature=False,
        )
        adapter, client = build_adapter(transport)
        inst = adapter.update_instance("quick_reference", "514", {"quick_re_title": "X"})
        assert inst.instance_id == "514"
        (req,) = [r for r in transport.requests if r.method == "PUT"]
        import json as _json

        body = _json.loads(req.content)
        # The field is present; the client also injects the (fake) creds.
        assert body["quick_re_title"] == "X"
        assert body["_key"] == FAKE_KEY
        assert "_secret" in body

    def test_create_response_ci_stub_resolves_id(self) -> None:
        # Live 2.6.2 returns ``{"ci_id": <int>}`` for both POST and PUT.
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
                ("POST", f"{API}/ci"): {"ci_id": 3262},
            },
            {},
            require_signature=False,
        )
        adapter, _ = build_adapter(transport)
        inst = adapter.create_instance("credential", {"credenti_key_id": "ACC-9999"})
        assert inst.instance_id == "3262"

    def test_delete_instance(self) -> None:
        transport = _RecordingTransport(
            {("DELETE", f"{API}/ci/514"): (204, None)}, {}
        )
        adapter, _ = build_adapter(transport)
        adapter.delete_instance("quick_reference", "514")  # must not raise

    def test_delete_405_maps_to_unsupported(self) -> None:
        transport = _RecordingTransport(
            {("DELETE", f"{API}/ci/514"): (405, {"error": "method not allowed"})},
            {},
        )
        adapter, _ = build_adapter(transport)
        with pytest.raises(UnsupportedOperationError) as exc:
            adapter.delete_instance("quick_reference", "514")
        assert exc.value.category == ErrorCategory.UNSUPPORTED_OPERATION


class TestRetryPolicy:
    def test_read_retries_on_5xx_then_succeeds(self) -> None:
        responses = [
            (500, {"error": "boom"}),
            (500, {"error": "boom"}),
            CI_TYPES,
        ]
        transport = _RecordingTransport(
            {"GET".join(()) if False else ("GET", f"{API}/attributes/s"): ATTRIBUTES},
            (200, CI_TYPES),
            scripts={("GET", f"{API}/ci_types"): responses},
        )
        adapter, _ = build_adapter(transport, max_retries=2)
        models = adapter.list_models()
        assert [m.name for m in models] == ["quick_reference", "credential"]
        ci_type_reqs = [
            r for r in transport.requests if r.url.path.endswith("/ci_types")
        ]
        # Three attempts were made (two 500s + one success).
        assert len(ci_type_reqs) == 3

    def test_write_is_not_retried_on_5xx(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
            },
            (500, {"error": "boom"}),
            require_signature=False,
        )
        adapter, _ = build_adapter(transport, max_retries=5)
        with pytest.raises(Exception) as exc:
            adapter.create_instance("credential", {"credenti_key_id": "x"})
        # Exactly one attempt to the POST: writes are never retried.
        post_reqs = [r for r in transport.requests if r.method == "POST"]
        assert len(post_reqs) == 1
        assert exc.value.category == ErrorCategory.UPSTREAM_SERVER

    def test_read_not_retried_on_auth_error(self) -> None:
        transport = _RecordingTransport({}, (401, {"message": "401 Unauthorized: unauthorized"}))
        adapter, _ = build_adapter(transport, max_retries=5)
        with pytest.raises(Exception) as exc:
            adapter.list_models()
        # 401 is not transient -> no retry.
        assert len(transport.requests) == 1
        assert exc.value.category == ErrorCategory.AUTHENTICATION

    def test_read_retry_exhausted_preserves_category(self) -> None:
        transport = _RecordingTransport({}, (500, {"error": "boom"}))
        adapter, _ = build_adapter(transport, max_retries=1)
        with pytest.raises(Exception) as exc:
            adapter.list_models()
        assert len(transport.requests) == 2
        assert exc.value.category == ErrorCategory.UPSTREAM_SERVER


class TestSignatureRegression:
    def test_bad_signature_is_rejected_by_stub(self) -> None:
        # A client whose signing drifted would produce a 401 from the
        # stub, surfacing as AuthenticationError end-to-end.
        from tests.adapters._fakes import FAKE_KEY, make_config

        config = make_config(key=FAKE_KEY, secret="rotated-secret-00000000000000")
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): CI_TYPES},
            (401, {"message": "401 Unauthorized: unauthorized"}),
        )
        adapter, _ = build_adapter(transport, config=config)
        with pytest.raises(Exception) as exc:
            adapter.list_models()
        assert exc.value.category == ErrorCategory.AUTHENTICATION

    def test_request_params_signed_not_leaked_in_extra(self) -> None:
        # Sanity for the redaction contract: the client registers both
        # credentials as known secrets.
        from cmdb_mcp_adapter.observability.redaction import redact
        from tests.adapters._fakes import FAKE_KEY, FAKE_SECRET

        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): CI_TYPES}, {},
        )
        adapter, client = build_adapter(transport)
        adapter.list_models()
        (req,) = [r for r in transport.requests if r.url.path.endswith("/ci_types")]
        query = dict(req.url.params)
        blob = str(redact(query, frozenset({FAKE_KEY, FAKE_SECRET})))
        assert FAKE_KEY not in blob
        assert FAKE_SECRET not in blob

# ---------------------------------------------------------------------------
# Read-only mode (VEOPS_READONLY)
# ---------------------------------------------------------------------------


class TestReadOnlyMode:
    """The VEOPS_READONLY switch must hard-disable the three write
    operations at the adapter boundary (before any upstream call) while
    leaving every read operation fully functional."""

    def _readonly_adapter(self):
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
                ("GET", f"{API}/ci/type/1"): CIS,
                ("GET", f"{API}/ci/search"): {"total": 0, "result": []},
            },
            {},
        )
        client = build_client(transport)
        return VeopsAdapter(client, max_retries=0, readonly=True)

    def test_create_refused(self) -> None:
        adapter = self._readonly_adapter()
        with pytest.raises(UnsupportedOperationError) as exc:
            adapter.create_instance("quick_reference", {"quick_re_title": "x"})
        assert "read-only" in exc.value.message
        assert exc.value.category == ErrorCategory.UNSUPPORTED_OPERATION

    def test_update_refused(self) -> None:
        adapter = self._readonly_adapter()
        with pytest.raises(UnsupportedOperationError) as exc:
            adapter.update_instance("quick_reference", "514", {"a": 1})
        assert "read-only" in exc.value.message

    def test_delete_refused(self) -> None:
        adapter = self._readonly_adapter()
        with pytest.raises(UnsupportedOperationError) as exc:
            adapter.delete_instance("quick_reference", "514")
        assert "read-only" in exc.value.message

    def test_writes_make_no_upstream_call(self) -> None:
        # The refusal must happen *before* any HTTP request is issued.
        transport = _RecordingTransport(
            {("GET", f"{API}/ci_types"): CI_TYPES},
            {},
        )
        adapter = VeopsAdapter(build_client(transport), max_retries=0, readonly=True)
        with pytest.raises(UnsupportedOperationError):
            adapter.create_instance("quick_reference", {"quick_re_title": "x"})
        assert transport.requests == []

    def test_reads_still_work(self) -> None:
        adapter = self._readonly_adapter()
        models = adapter.list_models()
        assert [m.name for m in models] == ["quick_reference", "credential"]
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        assert len(page.items) == 2
        search = adapter.search_instances(
            "quick_reference", "x", page_size=10, cursor=None
        )
        assert search.total == 0

    def test_default_is_write_enabled(self) -> None:
        # Without the flag the same client must *not* refuse writes.
        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): CI_TYPES,
                ("GET", f"{API}/attributes/s"): ATTRIBUTES,
                ("POST", f"{API}/ci"): {"ci_id": 3263},
            },
            {},
        )
        adapter = VeopsAdapter(build_client(transport), max_retries=0)
        inst = adapter.create_instance(
            "quick_reference", {"quick_re_title": "new"}
        )
        assert inst.instance_id == "3263"


