"""``GET /ci_relations/<id>/second_cis`` completeness + typing (0.2.8).

Covers defects **P-13** (a bare call returns the upstream's fixed page of
50 rows while ``numfound`` is the total over the whole edge set, so 2,207
of 5,026 deployment-wide edges — 43.9% — were invisible to the tool) and
**P-12** (``relation_type`` was always empty because the payload carries
no ``relation_type_id``).

The stub reproduces the contract that was **measured on the
intranet deployment on 2026-09-22**, not an imagined one:

* ``numfound`` = total over the edge set, ``total`` = rows returned;
* ``count=all`` removes the limit (``get_page_size("all")`` returns
  ``"all"``, and ``get_second_cis`` then skips ``offset``/``limit``), so
  ``len(rows) == numfound`` is a sound completeness check;
* the edge query has **no ``ORDER BY``** — paging is not a safe fallback;
* ``GET /relation_types`` answers a **bare array** and omits soft-deleted
  entries (8 live names: 承载, 关联凭据, 运维手册, 调用, 监测, 依赖,
  待核验, 归属项目);
* ``relation_type=<name>`` filters by *name* and a name outside the live
  dictionary — including a deleted one — is answered ``400 Invalid
  relation type``.

Reuses the signature-verifying ``_RecordingTransport`` from ``_fakes.py``
— no network, no real credentials.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import httpx
import pytest

from cmdb_mcp_adapter.core.errors import UpstreamServerError, ValidationError
from tests.adapters._fakes import _RecordingTransport, build_adapter

API = "/api/v0.1"
REL_PATH = f"{API}/ci_relations/48/second_cis"
DICT_PATH = f"{API}/relation_types"

#: The live dictionary, in upstream (id) order.  归属项目 is deliberately
#: last so "worst case" request counts are measurable.
LIVE_TYPES: tuple[str, ...] = (
    "承载",
    "关联凭据",
    "运维手册",
    "调用",
    "监测",
    "依赖",
    "待核验",
    "归属项目",
)

UNRESOLVED = "adapter.relation_type_unresolved"


def _ci(target: int) -> dict[str, Any]:
    """A flat *target* instance dict, as the endpoint returns them."""
    return {
        "_id": target,
        "_type": 4,
        "ci_type": "business_application",
        "ci_type_alias": "业务应用",
        "business_app_name": f"APP-{target:04d}",
    }


def _second_cis_handler(
    spec: list[tuple[int, str | None]],
    *,
    live_names: tuple[str, ...] = LIVE_TYPES,
    truncate_to: int | None = None,
    hide_numfound: bool = False,
    phantom: tuple[str, int] | None = None,
    reject: str | None = None,
) -> Any:
    """Emulate ``GET /ci_relations/48/second_cis`` faithfully.

    ``spec`` is the edge set as ``(target_id, relation_type_name_or_None)``
    pairs in upstream order; a ``None`` type means "an edge whose
    relation type is not in the live dictionary" (soft-deleted type).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("relation_type")
        if name is not None:
            if name == reject:
                return httpx.Response(
                    400, json={"message": f"Invalid relation type: {name}"}
                )
            if name not in live_names:
                # The adapter must never get here; kept as a guard so a
                # regression shows up as a hard failure, not a 200.
                return httpx.Response(
                    400, json={"message": f"Invalid relation type: {name}"}
                )
        selected = [t for t, ty in spec if name is None or ty == name]
        if phantom is not None and name == phantom[0]:
            # A row the *filtered* call answers but the unfiltered one did
            # not contain — with its own numfound counting it, so the
            # completeness check passes and the consumption guard is the
            # thing that has to catch it.
            selected = [*selected, phantom[1]]
        numfound = len(selected)
        rows = [_ci(t) for t in selected]
        if truncate_to is not None:
            rows = rows[:truncate_to]
        body: dict[str, Any] = {"second_cis": rows, "total": len(rows), "page": 1}
        if not hide_numfound:
            body["numfound"] = numfound
        return httpx.Response(200, json=body)

    return handler


def _dict_handler(
    names: tuple[str, ...] = LIVE_TYPES,
    *,
    deleted: tuple[str, ...] = (),
    response: Any | None = None,
) -> Any:
    """The bare-array ``GET /relation_types`` (verified shape)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if response is not None:
            if isinstance(response, tuple):  # an (status, body) pair
                status, body = response
                return httpx.Response(status, json=body)
            return response
        entries = [
            {
                "id": index,
                "name": name,
                "deleted": name in deleted,
                "created_at": "2026-09-22 10:00:00",
                "updated_at": "2026-09-22 10:00:00",
                "deleted_at": None,
            }
            for index, name in enumerate(names, start=1)
        ]
        return httpx.Response(200, json=entries)

    return handler


class _Fixture:
    """Transport + adapter for one edge spec, with request bookkeeping."""

    def __init__(self, spec: list[tuple[int, str | None]], **handler_kwargs: Any) -> None:
        self.transport = _RecordingTransport(
            {
                ("GET", REL_PATH): _second_cis_handler(spec, **handler_kwargs),
                ("GET", DICT_PATH): _dict_handler(),
            },
            {},
        )
        self.adapter, _ = build_adapter(self.transport)

    def relations(self) -> Any:
        return self.adapter.get_relations("server", "48")

    @property
    def edge_requests(self) -> list[httpx.Request]:
        return [r for r in self.transport.requests if r.url.path == REL_PATH]

    @property
    def dict_requests(self) -> list[httpx.Request]:
        return [r for r in self.transport.requests if r.url.path == DICT_PATH]

    @property
    def queried_types(self) -> list[str | None]:
        return [r.url.params.get("relation_type") for r in self.edge_requests]


class TestCompleteness:
    """P-13: the edge set must come back whole, or the call must fail."""

    def test_count_all_fetches_every_edge(self) -> None:
        # 120 edges of one type: a bare call would return 50 of them.
        spec = [(1000 + n, "承载") for n in range(120)]
        fx = _Fixture(spec)
        rels = fx.relations()
        assert len(rels) == 120
        assert [r.target_instance_id for r in rels] == [str(t) for t, _ in spec]

    def test_count_all_is_sent_and_page_is_never_used(self) -> None:
        spec = [(1000 + n, "承载") for n in range(80)]
        fx = _Fixture(spec)
        assert len(fx.relations()) == 80
        assert fx.edge_requests, "no edge request was made"
        for request in fx.edge_requests:
            assert request.url.params.get("count") == "all"
            # The query has no ORDER BY upstream: paging cannot be used as
            # a fallback, so the adapter must not even try it.
            assert "page" not in request.url.params

    def test_upstream_that_still_truncates_raises_instead_of_passing_it_on(self) -> None:
        # A deployment that ignored count=all (claims 120, serves 50): the
        # tool must be loud, never quietly short.
        spec = [(1000 + n, "承载") for n in range(120)]
        fx = _Fixture(spec, truncate_to=50)
        with pytest.raises(UpstreamServerError) as exc:
            fx.relations()
        assert "50 of 120" in exc.value.message
        assert "truncated" in exc.value.message

    def test_missing_numfound_with_rows_raises(self) -> None:
        spec = [(1000 + n, "承载") for n in range(5)]
        fx = _Fixture(spec, hide_numfound=True)
        with pytest.raises(UpstreamServerError) as exc:
            fx.relations()
        assert "numfound" in exc.value.message

    def test_empty_edge_set_is_one_request_and_no_dictionary_read(self) -> None:
        fx = _Fixture([])
        assert fx.relations() == []
        assert len(fx.edge_requests) == 1
        assert fx.dict_requests == []


class TestTyping:
    """P-12: every edge carries its relation type, or says it cannot."""

    def test_each_edge_is_labelled_with_its_relation_type(self) -> None:
        spec = [
            (201, "承载"),
            (202, "依赖"),
            (203, "归属项目"),
            (204, "承载"),
            (205, "待核验"),
        ]
        fx = _Fixture(spec)
        rels = fx.relations()
        assert [(r.target_instance_id, r.relation_type) for r in rels] == [
            ("201", "承载"),
            ("202", "依赖"),
            ("203", "归属项目"),
            ("204", "承载"),
            ("205", "待核验"),
        ]
        assert all(r.source_instance_id == "48" for r in rels)
        assert all(UNRESOLVED not in (r.metadata or {}) for r in rels)
        # the raw target payload is still preserved (pre-0.2.8 behaviour)
        assert rels[0].metadata["veops.ci_type"] == "business_application"

    def test_multiset_of_edges_survives_the_typing_pass(self) -> None:
        spec = [(t, ty) for t, ty in [
            (9, "承载"), (9, "依赖"), (10, None), (11, "待核验"), (12, "承载"),
        ]]
        fx = _Fixture(spec)
        rels = fx.relations()
        assert Counter((r.target_instance_id, r.relation_type) for r in rels) == Counter(
            (str(t), ty or "") for t, ty in spec
        )

    def test_two_edges_to_the_same_target_are_two_relations(self) -> None:
        # Upstream does not SELECT DISTINCT: one target reached by two
        # relation types must show up twice, once per type.
        spec = [(7, "承载"), (7, "依赖")]
        fx = _Fixture(spec)
        rels = fx.relations()
        assert [r.target_instance_id for r in rels] == ["7", "7"]
        assert sorted(r.relation_type for r in rels) == sorted(["承载", "依赖"])

    def test_edge_of_a_deleted_relation_type_is_flagged_not_silent(self) -> None:
        spec = [(8, "承载"), (9, None)]
        fx = _Fixture(spec)
        rels = fx.relations()
        assert len(rels) == 2
        resolved = [r for r in rels if UNRESOLVED not in (r.metadata or {})]
        unresolved = [r for r in rels if UNRESOLVED in (r.metadata or {})]
        assert [(r.target_instance_id, r.relation_type) for r in resolved] == [("8", "承载")]
        assert [(r.target_instance_id, r.relation_type) for r in unresolved] == [("9", "")]
        assert unresolved[0].metadata[UNRESOLVED] is True
        # an unattributable edge defeats the early stop: every name probed
        assert fx.queried_types == [None, *LIVE_TYPES]

    def test_typed_rows_must_exist_in_the_unfiltered_set(self) -> None:
        # A per-type query answering a target the unfiltered call did not
        # return means the two responses disagree; guessing which edge to
        # label would corrupt the result, so it raises.
        spec = [(201, "承载"), (202, "依赖")]
        fx = _Fixture(spec, phantom=("依赖", 999))
        with pytest.raises(UpstreamServerError) as exc:
            fx.relations()
        assert "not in the unfiltered edge set" in exc.value.message

    def test_a_type_name_the_deployment_rejects_is_not_swallowed(self) -> None:
        # The dictionary said the name is live, the edge endpoint 400s on
        # it (a stale cache or a server-side drift): surfacing "0 edges of
        # that type" would silently drop edges, so the error propagates.
        # 监测 is the 5th name in dictionary order, and the loop stops as
        # soon as every edge is attributed — so keep one edge that can
        # never be attributed to make the probe actually reach it.
        spec = [(201, "承载"), (202, None)]
        transport = _RecordingTransport(
            {
                ("GET", REL_PATH): _second_cis_handler(spec, reject="监测"),
                ("GET", DICT_PATH): _dict_handler(),
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        with pytest.raises(ValidationError):
            adapter.get_relations("server", "48")


class TestRequestCost:
    """The breakdown stops as soon as every edge is attributed."""

    def test_early_stop_after_the_first_covering_type(self) -> None:
        spec = [(1000 + n, "承载") for n in range(60)]
        fx = _Fixture(spec)
        assert len(fx.relations()) == 60
        # unfiltered + 承载 only: the other 7 names are never queried.
        assert fx.queried_types == [None, "承载"]

    def test_worst_case_is_one_call_per_live_type(self) -> None:
        spec = [(203, "归属项目"), (204, "归属项目")]
        fx = _Fixture(spec)
        assert len(fx.relations()) == 2
        assert fx.queried_types == [None, *LIVE_TYPES]

    def test_dictionary_is_cached_for_60_seconds(self) -> None:
        spec = [(1, "承载")]
        fx = _Fixture(spec)
        fx.relations()
        fx.relations()
        assert len(fx.dict_requests) == 1

    def test_dictionary_refetched_once_the_ttl_expires(self) -> None:
        spec = [(1, "承载")]
        fx = _Fixture(spec)
        clock = {"t": 1_000.0}
        # freeze the clock before the first fetch, then step past the TTL
        fx.adapter._now = lambda: clock["t"]  # type: ignore[method-assign]
        fx.relations()
        assert len(fx.dict_requests) == 1
        clock["t"] = 1_061.0
        fx.relations()
        assert len(fx.dict_requests) == 2


class TestDictionaryDegradation:
    """The label dictionary is not allowed to break the edge list."""

    def _fixture_with_dict(self, response: Any) -> _Fixture:
        spec = [(201, "承载"), (202, "依赖")]
        transport = _RecordingTransport(
            {
                ("GET", REL_PATH): _second_cis_handler(spec),
                ("GET", DICT_PATH): _dict_handler(response=response),
            },
            {},
        )
        fx = _Fixture.__new__(_Fixture)
        fx.transport = transport
        fx.adapter, _ = build_adapter(transport)
        return fx

    @pytest.mark.parametrize("status", [403, 404])
    def test_denied_or_absent_dictionary_still_returns_edges(self, status: int) -> None:
        fx = self._fixture_with_dict((status, {"message": "nope"}))
        rels = fx.relations()
        assert [r.target_instance_id for r in rels] == ["201", "202"]
        assert all(r.relation_type == "" for r in rels)
        assert all(r.metadata[UNRESOLVED] is True for r in rels)
        # edges themselves were fetched unfiltered + nothing else
        assert fx.queried_types == [None]

    def test_bare_array_contract_is_enforced(self) -> None:
        # A deployment answering the usual *list wrapper* is not the
        # verified contract: it must not be mistaken for an empty
        # dictionary, it must be refused (and the edges then degrade).
        fx = self._fixture_with_dict(None)
        fx.transport.handlers[("GET", DICT_PATH)] = _dict_handler(
            response=(200, {"relation_types": [{"id": 1, "name": "承载"}]})
        )
        rels = fx.relations()
        assert len(rels) == 2
        assert all(r.metadata[UNRESOLVED] is True for r in rels)

    def test_soft_deleted_dictionary_entries_are_never_queried(self) -> None:
        transport = _RecordingTransport(
            {
                ("GET", REL_PATH): _second_cis_handler([(201, "承载")]),
                ("GET", DICT_PATH): _dict_handler(deleted=("依赖",)),
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        rels = adapter.get_relations("server", "48")
        assert [(r.target_instance_id, r.relation_type) for r in rels] == [("201", "承载")]
        types = [
            r.url.params.get("relation_type")
            for r in transport.requests
            if r.url.path == REL_PATH
        ]
        assert "依赖" not in types  # would be a 400 upstream

    def test_blank_relation_type_name_is_never_sent(self) -> None:
        # Upstream reads an *empty* relation_type parameter as "no filter
        # at all", which would re-fetch the whole set under a blank label.
        transport = _RecordingTransport(
            {
                ("GET", REL_PATH): _second_cis_handler(
                    [(201, "承载")], live_names=("", "承载")
                ),
                ("GET", DICT_PATH): _dict_handler(names=("", "承载")),
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        rels = adapter.get_relations("server", "48")
        assert [(r.target_instance_id, r.relation_type) for r in rels] == [("201", "承载")]
        types = [
            r.url.params.get("relation_type")
            for r in transport.requests
            if r.url.path == REL_PATH
        ]
        assert "" not in types
        assert types == [None, "承载"]
