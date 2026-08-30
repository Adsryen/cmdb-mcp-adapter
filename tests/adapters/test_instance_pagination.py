"""``GET /ci/type/<type_id>`` pagination (0.2.5).

The upstream instance endpoint is paginated with a *fixed* page of
50 entries: ``page`` is honoured (from 1), ``page_size`` is ignored,
and ``numfound``/``total`` in the response are *per-page* counts —
there is no total over the type (verified 2026-08-29 on the
intranet deployment, where e.g. ``data_check`` holds 772 CIs).
The end-of-data signal is a short page (< 50).  A single unpaginated
GET therefore silently truncated every type with more than 50 CIs
(server 62, business_application 368, middleware 374, credential 445,
operation_runbook 236, network 113, data_check 772 on that
deployment); the adapter now pages through ``page=1,2,...`` until a
short or empty page arrives — a page that adds no new ``_id`` also
terminates the loop — and returns the whole list in one result page.

Reuses the signature-verifying ``_RecordingTransport`` from
``_fakes.py`` (the production :func:`sign` is enforced in the stub) —
no network, no real credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cmdb_mcp_adapter.core.errors import ValidationError
from tests.adapters._fakes import _RecordingTransport, build_adapter

API = "/api/v0.1"

#: The fixed upstream page size (the deployment ignores page_size).
UPSTREAM_PAGE_SIZE = 50


def _ci_type_payload() -> dict[str, Any]:
    return {
        "numfound": 1,
        "ci_types": [{"id": 1, "name": "quick_reference", "alias": "快速查询"}],
    }


def _raw_ci(num: int) -> dict[str, Any]:
    return {
        "_id": num,
        "_type": 1,
        "_updated_at": "2026-08-29 12:00:00",
        "_updated_by": "admin",
        "ci_type": "quick_reference",
        "ci_type_alias": "快速查询",
        "quick_re_title": f"CI-{num:04d}",
    }


def _instance_pages_handler(
    master: list[dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Emulates the real ``GET /ci/type/<type_id>`` contract: a fixed
    page of 50, ``page`` honoured, ``page_size`` ignored, and
    ``numfound``/``total`` as per-page counts (the live response
    shape)."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * UPSTREAM_PAGE_SIZE
        page_cis = master[start : start + UPSTREAM_PAGE_SIZE]
        return httpx.Response(
            200,
            json={
                "cis": page_cis,
                "numfound": len(page_cis),
                "page": page,
                "total": len(page_cis),
                "type_id": 1,
            },
        )

    return handler


class TestInstancePagination:
    """The instance list must be fetched *in full* across pages, with
    the terminating edges (short page, exact page multiple, empty
    type, repeated page) all bounded."""

    def _transport(self, master: list[dict[str, Any]]) -> _RecordingTransport:
        return _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): _ci_type_payload(),
                ("GET", f"{API}/attributes/s"): {"attributes": []},
                ("GET", f"{API}/ci/type/1"): _instance_pages_handler(master),
            },
            {},
        )

    def _instance_requests(self, transport: _RecordingTransport) -> list[httpx.Request]:
        return [r for r in transport.requests if r.url.path.endswith("/ci/type/1")]

    def test_short_page_terminates_and_full_list_returned(self) -> None:
        # 120 CIs = 50/50/20: the third (short) page terminates the
        # loop and all 120 come back in one result page, in order.
        transport = self._transport([_raw_ci(n) for n in range(1, 121)])
        adapter, _ = build_adapter(transport)
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        requests = self._instance_requests(transport)
        assert [r.url.params.get("page") for r in requests] == ["1", "2", "3"]
        # page_size must not be sent: the upstream ignores it anyway.
        assert all("page_size" not in r.url.params for r in requests)
        assert [i.instance_id for i in page.items] == [str(n) for n in range(1, 121)]
        assert page.total == 120
        assert page.page == 1
        assert page.next_cursor is None
        assert page.has_next is False

    def test_exact_page_multiple_terminates_on_empty_page(self) -> None:
        # 100 CIs = exactly two full pages: the loop issues a third
        # request, receives the empty page, and stops there (no
        # fourth; no lost or duplicated CIs).
        transport = self._transport([_raw_ci(n) for n in range(1, 101)])
        adapter, _ = build_adapter(transport)
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        assert [
            r.url.params.get("page") for r in self._instance_requests(transport)
        ] == ["1", "2", "3"]
        assert len(page.items) == 100
        assert len({i.instance_id for i in page.items}) == 100
        assert page.total == 100
        assert page.next_cursor is None

    def test_empty_type_single_request(self) -> None:
        # A type with zero CIs is a single empty page — exactly one
        # upstream request.
        transport = self._transport([])
        adapter, _ = build_adapter(transport)
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        assert [
            r.url.params.get("page") for r in self._instance_requests(transport)
        ] == ["1"]
        assert len(page.items) == 0
        assert page.total == 0
        assert page.next_cursor is None

    def test_repeated_page_terminates_without_duplicates(self) -> None:
        # Guard against an upstream that repeats a page instead of
        # advancing: a page that adds no new _id stops the loop (and
        # the repeated entries are not duplicated into the list).
        repeated = [_raw_ci(n) for n in range(1, 51)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "cis": repeated,
                    "numfound": len(repeated),
                    "page": 1,
                    "total": len(repeated),
                    "type_id": 1,
                },
            )

        transport = _RecordingTransport(
            {
                ("GET", f"{API}/ci_types"): _ci_type_payload(),
                ("GET", f"{API}/attributes/s"): {"attributes": []},
                ("GET", f"{API}/ci/type/1"): handler,
            },
            {},
        )
        adapter, _ = build_adapter(transport)
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        assert [
            r.url.params.get("page") for r in self._instance_requests(transport)
        ] == ["1", "2"]
        assert len(page.items) == 50
        assert len({i.instance_id for i in page.items}) == 50
        assert page.total == 50

    def test_small_model_single_request(self) -> None:
        # A type with fewer than 50 CIs is a single (short) page —
        # exactly one upstream request (the pre-0.2.5 behaviour for
        # small models is preserved).
        transport = self._transport([_raw_ci(n) for n in range(1, 31)])
        adapter, _ = build_adapter(transport)
        page = adapter.get_instances(
            "quick_reference", page_size=10, cursor=None, filters={}
        )
        assert [
            r.url.params.get("page") for r in self._instance_requests(transport)
        ] == ["1"]
        assert len(page.items) == 30
        assert page.total == 30
        assert page.next_cursor is None

    def test_cursor_still_rejected(self) -> None:
        # The adapter hides the upstream pagination (whole type in one
        # result page), so a non-None cursor remains a validation
        # error — raised before any instance request.
        transport = self._transport([_raw_ci(n) for n in range(1, 61)])
        adapter, _ = build_adapter(transport)
        with pytest.raises(ValidationError) as exc:
            adapter.get_instances(
                "quick_reference",
                page_size=10,
                cursor="veops-instance-page-2",
                filters={},
            )
        assert "cursor" in exc.value.message
        assert self._instance_requests(transport) == []
