"""The VeOps CMDB adapter: implements the core :class:`CMDBAdapter`
protocol against the verified OneOps/VeOps 2.6.2 contract.

This is the orchestration layer.  It calls :mod:`.client` for the raw
HTTP work (including per-request key/secret signing), :mod:`:`\
:mod:`.mapper` for payload translation, and the shared
:func:`cmdb_mcp_adapter.observability.retry.retry` helper for the bounded
retry policy.

Retry policy (per the design boundary):

* **Read** operations (``list_models``, ``get_instances``,
  ``search_instances``, ``get_relations``) are retried with
  ``is_idempotent=True`` and ``max_retries`` from the inbound settings.
  Only transient categories (timeout / rate limit / 5xx) are retried.
* **Write** operations (``create_instance``, ``update_instance``,
  ``delete_instance``) are **never** retried (``is_idempotent=False``).

Contract notes (verified 2026-08, see ``docs/veops-adapter.md``):

* **``get_instances`` returns the whole type in one result page.**
  The upstream ``GET /ci/type/<type_id>`` *is* paginated (fixed 50
  per page; ``page`` honoured, ``page_size`` ignored; ``numfound``/
  ``total`` are per-page counts, not a total), so the adapter pages
  through ``page=1,2,...`` until a short or empty page arrives and
  returns the full list with ``total`` = the full count.
  ``next_cursor`` is always ``None`` and a non-``None`` cursor
  argument is rejected with a validation error.
* **``search_instances`` is page-paginated** (``page`` / ``page_size``
  over ``GET /ci/search?q=&ci_type=&page=&page_size=``); the next-page
  number is encoded into the core cursor.  The upstream ``fl`` search
  parameter controls *returned fields*, not filtering, so the core
  ``filters`` argument is applied **client-side** (exact equality) on
  the returned pages.
* **``get_relations``** returns **all** *outbound* edge targets of an
  instance (``GET /ci_relations/<id>/second_cis``), with their relation
  type.  That endpoint is paginated at a fixed 50 rows (``numfound`` is
  the total over the edge set, unlike the ci list endpoint), so the
  adapter sends ``count=all`` and refuses the result when the row count
  does not match ``numfound`` instead of passing on a truncated list
  (defect P-13).  The response carries no ``relation_type_id``, so the
  type is recovered by re-querying once per *live* relation type name
  from ``GET /relation_types`` (cached, 60 s TTL), consuming rows out of
  the unfiltered set; anything left over is flagged in ``metadata``
  rather than silently left empty (defect P-12).  The raw target payload
  is preserved in each relation's ``metadata``.
* **Write calls** sign their scalar JSON-body values the same way GETs
  sign their query parameters.
* **``list_models``** maps ``GET /ci_types`` (a list-wrapped response).
  The ci_types entries carry **no attribute list**, so per-model
  ``fields`` are resolved from the type's attribute *binding list*:
  ``GET /ci_types/<type_id>/attributes`` (one request per model, cached
  per model with a 60 s TTL).  That endpoint is also the only source of
  the per-field ``required`` flag and of the resolved enumeration
  (``choice_value``).  Inferring the mapping from the attribute *naming*
  convention was tried first and is **wrong on real deployments** —
  see :meth:`VeopsAdapter._fields_for_type`.  ``list_models`` propagates
  a binding-lookup failure instead of guessing; instance reads degrade
  to ``fields=()`` (harmless: their ``data`` stays complete).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from cmdb_mcp_adapter.adapters.veops import mapper
from cmdb_mcp_adapter.adapters.veops.client import VeopsClient
from cmdb_mcp_adapter.adapters.veops.intent import VeopsIntentOps
from cmdb_mcp_adapter.core.adapter import Filters
from cmdb_mcp_adapter.core.errors import (
    AuthorizationError,
    CoreError,
    NotFoundError,
    UnsupportedOperationError,
    UpstreamServerError,
    ValidationError,
)
from cmdb_mcp_adapter.core.models import (
    CMDBInstance,
    CMDBModel,
    CMDBRelation,
    ModelField,
    Page,
)
from cmdb_mcp_adapter.observability.retry import retry

#: Endpoint path templates (relative to the API prefix ``/api/v0.1``).
#: Verified against the 2.6.2 deployment; a deployment that mounts
#: endpoints elsewhere only changes these strings and the prefix.
_PATH_CI_TYPES = "/ci_types"
_PATH_TYPE_INSTANCES = "/ci/type/{type_id}"
_PATH_CI = "/ci"
_PATH_INSTANCE = "/ci/{instance_id}"
_PATH_SEARCH = "/ci/search"
_PATH_RELATION_SECOND = "/ci_relations/{instance_id}/second_cis"
_PATH_RELATION_TYPES = "/relation_types"
#: The type→attribute binding list (see :meth:`VeopsAdapter._fields_for_type`
#: for why this is the only trustworthy field source).  A type *name* is
#: accepted in the same slot upstream; the adapter always sends the numeric id.
_PATH_TYPE_ATTRIBUTES = "/ci_types/{type_id}/attributes"

#: TTL (seconds) for the ci_types / attributes caches.  The upstream has
#: no change notification, so a short TTL balances freshness against
#: request volume (one upstream call per entry refresh).
_CACHE_TTL_SECONDS = 60.0

#: Upstream page size of ``GET /ci/type/<type_id>``: the endpoint
#: serves a *fixed* page of 50 entries — ``page`` is honoured but
#: ``page_size`` is ignored, and ``numfound``/``total`` in the
#: response are *per-page* counts, not a total over the type
#: (verified 2026-08-29 on the intranet deployment, where e.g.
#: ``data_check`` holds 772 CIs across 16 pages).  The end-of-data
#: signal is therefore a short page (< 50); a deployment that served
#: a *smaller* page would make the first page look short and stop
#: the fetch early (none observed — 115 is fixed at 50), while a
#: larger page still terminates correctly on the empty page.
_INSTANCE_PAGE_SIZE = 50

#: Fetch-everything parameter for the *relation* endpoints.  ``count``
#: feeds ``get_page_size()``, which returns the string ``"all"``
#: untouched, and ``get_second_cis()`` then skips ``offset``/``limit``
#: altogether (verified in ``api/lib/cmdb/ci.py:1141-1155`` +
#: ``api/lib/utils.py:42-50`` on the 2.6.2 deployment).
_RELATION_COUNT_ALL = "all"

_logger = logging.getLogger("cmdb_mcp_adapter.adapters.veops")


def _as_int(value: Any) -> int | None:
    """Coerce an upstream count (``int`` or digit string) to ``int``.

    ``None`` for anything else — the caller decides whether a missing
    count is fatal (it is for a completeness check).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class VeopsAdapter:
    """A :class:`~cmdb_mcp_adapter.core.adapter.CMDBAdapter` backed by
    VeOps 2.6.2.

    When constructed with ``readonly=True`` (the ``VEOPS_READONLY``
    switch) the write operations — the three primitives
    (``create_instance`` / ``update_instance`` / ``delete_instance``) and
    the three intent-level writes (``cmdb_create_card`` /
    ``cmdb_update_card`` / ``cmdb_link_relations``) —
    raise :class:`UnsupportedOperationError` before any upstream call;
    all read operations (including the zero-write ``cmdb_preflight_write``)
    are unaffected.
    """

    def __init__(self, client: VeopsClient, *, max_retries: int, readonly: bool = False) -> None:
        self._client = client
        self._max_retries = max_retries
        self._readonly = readonly
        # name -> (numeric ci_type id, fetched_at)
        self._type_ids: dict[str, tuple[int, float]] = {}
        # name -> raw ci_types entry (keeps alias/icon/etc. for display)
        self._type_payloads: dict[str, dict[str, Any]] = {}
        self._type_ids_fetched_at: float | None = None
        # model name -> (resolved ModelFields, fetched_at); one entry per
        # ci_type, filled by :meth:`_fields_for_type` (60 s TTL).
        self._model_fields: dict[str, tuple[tuple[ModelField, ...], float]] = {}
        # live relation type names, filled by :meth:`_relation_type_names`
        # (same 60 s TTL); ``None`` = never fetched.
        self._relation_names: tuple[str, ...] | None = None
        self._relation_names_fetched_at: float | None = None
        # lazily built intent-ops facade (layer-3 tools); holds its own
        # liveness cache for the lifetime of this adapter instance.
        self._intent_ops: VeopsIntentOps | None = None

    # -- internals ---------------------------------------------------------
    def _read(self, fn: Callable[[], Any], *, operation: str) -> Any:
        """Run an idempotent (read) operation under the bounded retry
        policy."""
        return retry(
            fn,
            is_idempotent=True,
            max_retries=self._max_retries,
            operation_name=operation,
        )

    def _now(self) -> float:
        # Indirect so tests can freeze time deterministically.
        return time.monotonic()

    def _check_writable(self, operation: str) -> None:
        """Refuse a write when the adapter runs in read-only mode."""
        if self._readonly:
            raise UnsupportedOperationError(
                f"veops adapter is in read-only mode (VEOPS_READONLY=1); "
                f"write operation '{operation}' is disabled"
            )

    def _ci_types_fresh(self) -> bool:
        return (
            self._type_ids_fetched_at is not None
            and self._now() - self._type_ids_fetched_at < _CACHE_TTL_SECONDS
        )

    def _refresh_ci_types(self) -> None:
        """Fetch the ci_types dictionary (retried) and refresh the
        name→id cache plus the per-model field resolution."""
        payload = self._client.request("GET", _PATH_CI_TYPES)
        now = self._now()
        self._type_ids = {
            str(t.get("name", "")): (int(t["id"]), now)
            for t in (payload.get("ci_types") or [])
            if isinstance(t, dict)
            and t.get("name") is not None
            and isinstance(t.get("id"), int)
        }
        # Keep the raw entries (alias etc.) for display mapping.
        self._type_payloads = {
            str(t.get("name", "")): t
            for t in (payload.get("ci_types") or [])
            if isinstance(t, dict) and t.get("name") is not None
        }
        self._type_ids_fetched_at = now
        # Drop the field cache of types this deployment no longer has;
        # surviving types keep their own entry until its TTL expires.
        self._model_fields = {
            name: entry for name, entry in self._model_fields.items() if name in self._type_ids
        }

    def _resolve_type_id(self, model_name: str) -> int:
        """Resolve a ci_type *name* to its numeric id (cached, 60 s TTL).

        Raises:
            ValidationError: when the name is unknown to this deployment
                (checked against a fresh ci_types list).
        """
        if not self._ci_types_fresh():
            self._refresh_ci_types()
        hit = self._type_ids.get(model_name)
        if hit is None:
            raise ValidationError(
                f"unknown model '{model_name}': the veops deployment has no "
                "ci_type with that name (see get_models)"
            )
        return hit[0]

    def _fields_for_type(self, type_name: str) -> tuple[ModelField, ...]:
        """Resolve one ci_type's declared fields from its attribute
        **binding list** (``GET /ci_types/<type_id>/attributes``).

        This is the only authoritative type→attribute mapping the upstream
        offers.  ``GET /ci_types`` answers no attribute list, and the
        global ``GET /attributes/s`` dictionary carries no binding at all
        — an earlier release inferred the binding from the deployment's
        attribute-naming convention (``credential`` → ``credenti_*``) plus
        a hand-maintained type→prefix alias table.  That heuristic was
        falsified on the intranet deployment (2026-09-21): attributes
        are **not namespaced per type**, so ``business_system`` (2 bound
        attributes) was reported with 29 fields borrowed from
        ``business_application`` while ``runtime_platform`` (31 bound) and
        ``monitoring_component`` (27, all ``middlewa_*``) were reported
        wrong or empty.  There is deliberately **no fallback** to the
        naming convention here: a wrong field list is worse than an error.

        The binding list also carries what the global dictionary cannot:
        the per-binding ``is_required`` (``required`` is a property of the
        *binding*, not of the attribute — ``c_attributes`` has no such
        column) and the server-resolved ``choice_value`` enumeration, so
        ``required`` and ``enum_values`` become real metadata instead of
        defaults.  Inherited attributes are included and the whole list
        arrives in the upstream's ``(order, id)`` sequence, which is kept.

        Results are cached per model name under :data:`_CACHE_TTL_SECONDS`.

        Raises:
            ValidationError: when ``type_name`` is unknown to the
                deployment (from :meth:`_resolve_type_id`).
            CoreError: any upstream failure of the binding call, propagated
                unchanged so ``list_models`` surfaces it.
        """
        hit = self._model_fields.get(type_name)
        if hit is not None and self._now() - hit[1] < _CACHE_TTL_SECONDS:
            return hit[0]
        type_id = self._resolve_type_id(type_name)
        payload = self._client.request("GET", _PATH_TYPE_ATTRIBUTES.format(type_id=type_id))
        fields = tuple(mapper.map_type_attributes(payload))
        self._model_fields[type_name] = (fields, self._now())
        return fields

    def _best_effort_fields(self, type_name: str) -> tuple[ModelField, ...]:
        """Field list for *instance* mapping, degrading to ``()`` when the
        binding lookup fails.

        Empty is safe on this path: :func:`.mapper.map_ci` only *restricts*
        ``data`` to the declared fields when it has a non-empty list, so an
        unavailable binding never drops instance values — it only stops
        the vendor-key quarantine.  ``list_models`` is the opposite case
        (the fields *are* the product) and must not swallow the error.
        """
        try:
            return self._fields_for_type(type_name)
        except CoreError:
            return ()

    def _known_fields(self, model_name: str) -> tuple[str, ...]:
        """Field names for a model (best effort, cached per model 60 s).

        One binding-list request per model per TTL, so instance/search
        reads pay for the model they touch — not for the whole
        dictionary.
        """
        return tuple(f.name for f in self._best_effort_fields(model_name))

    def _relation_type_names(self) -> tuple[str, ...]:
        """The deployment's *live* relation type names (cached, 60 s TTL).

        Labeling an edge requires this list: the ``second_cis`` endpoint
        exposes the relation type only as a **query filter addressed by
        name**, and it answers ``400 Invalid relation type`` for anything
        outside the current dictionary — including a soft-deleted name
        (both verified on the intranet deployment, 2026-09-22).  A
        hard-coded table would therefore break the day an operator adds
        or renames a type, so the names are read from
        ``GET /relation_types`` when the cache expires.

        Blank names are dropped: upstream treats an empty ``relation_type``
        parameter as *no filter at all*, which would re-fetch the whole
        edge set and label every edge with nothing.

        Returns an empty tuple when the dictionary endpoint is *not
        usable as specified* — denied (``403``), absent (``404``) or a
        shape the contract does not allow (``ValidationError``, e.g. the
        usual list wrapper instead of the bare array): the edges come
        from another endpoint and are already proven complete by the
        ``numfound`` self-check, so they are still worth returning, each
        flagged unresolved.  Degradation is visible, never silent, and is
        logged once per attempt (the result is deliberately *not*
        cached).  Transient failures (timeout / 5xx) propagate into the
        retry policy instead.
        """
        now = self._now()
        if (
            self._relation_names is not None
            and self._relation_names_fetched_at is not None
            and now - self._relation_names_fetched_at < _CACHE_TTL_SECONDS
        ):
            return self._relation_names
        try:
            payload = self._client.request_array(_PATH_RELATION_TYPES)
        except (AuthorizationError, NotFoundError, ValidationError) as exc:
            # Not cached on purpose: the next call tries again.
            _logger.warning(
                "veops.relation_types.unavailable",
                extra={
                    "event": "veops.relation_types.unavailable",
                    "category": exc.category.value,
                },
            )
            return ()
        by_id = mapper.map_relation_types(payload)
        names = tuple(
            stripped for stripped in (by_id[i].strip() for i in sorted(by_id)) if stripped
        )
        self._relation_names = names
        self._relation_names_fetched_at = self._now()
        return names

    # -- the seven protocol operations ------------------------------------
    def list_models(self) -> list[CMDBModel]:
        def _one() -> list[CMDBModel]:
            self._refresh_ci_types()
            # One binding-list request per model (12 on the probed
            # deployment, ~1 s warm).  Failures propagate on purpose: a
            # silently wrong field list would have agents write garbage.
            fields_by_name = {name: self._fields_for_type(name) for name in self._type_ids}
            return mapper.map_ci_types(
                {"ci_types": self._type_ids_names()}, fields_by_name
            )

        result = self._read(_one, operation="list_models")
        return cast("list[CMDBModel]", result)

    def _type_ids_names(self) -> list[dict[str, Any]]:
        """The cached ci_types payload entries (full dict per name)."""
        return [self._type_payloads[name] for name in self._type_payloads]

    def get_instances(
        self,
        model_name: str,
        *,
        page_size: int,
        cursor: str | None,
        filters: Filters,
    ) -> Page[CMDBInstance]:
        """Fetch *all* instances of the type in one result page.

        ``GET /ci/type/<type_id>`` is paginated upstream with a fixed
        page of 50 entries (``page`` honoured, ``page_size`` ignored;
        ``numfound``/``total`` are per-page counts, not a total), so
        a single unpaginated call silently truncates any type with
        more than 50 CIs (verified 2026-08-29 on the intranet VeOps host
        deployment).  The loop pages through ``page=1,2,...`` until a
        short or empty page arrives — a page that adds no new ``_id``
        also terminates it (a guard against an upstream that repeats
        a page instead of advancing) — and returns the merged list,
        so ``total`` is the full count and ``next_cursor`` is always
        ``None``.
        """
        if cursor is not None:
            # The adapter already hides the upstream pagination: the
            # whole type comes back in one result page, so there is
            # no "next page" concept to cursor across.
            raise ValidationError(
                "veops get_instances returns the full instance list in "
                "a single result page: the upstream /ci/type/<id> "
                "endpoint is paginated (fixed 50 per page) and the "
                "adapter pages through all of it internally, so no "
                "cursor is issued or accepted"
            )
        type_id = self._resolve_type_id(model_name)

        def _one() -> dict[str, Any]:
            raws: list[Any] = []
            seen_ids: set[Any] = set()
            page = 1
            while True:
                payload = self._client.request(
                    "GET",
                    _PATH_TYPE_INSTANCES.format(type_id=type_id),
                    params={"page": page},
                )
                page_cis = [
                    c for c in (payload.get("cis") or []) if isinstance(c, dict)
                ]
                fresh: list[Any] = []
                for c in page_cis:
                    # Key on the raw _id; entries lacking it fall back
                    # to object identity so they are never merged away.
                    marker = c.get("_id")
                    if marker is None:
                        marker = id(c)
                    if marker in seen_ids:
                        continue
                    seen_ids.add(marker)
                    fresh.append(c)
                raws.extend(fresh)
                if len(page_cis) < _INSTANCE_PAGE_SIZE or not fresh:
                    break
                page += 1
            return {"cis": raws}

        payload = self._read(_one, operation="get_instances")
        items = mapper.map_cis(
            payload, model_name=model_name, known_fields=self._known_fields(model_name)
        )
        items = _filter_instances(items, filters)
        total = len(items)
        return Page(
            items=items,
            page=1,
            page_size=page_size,
            total=total,
            next_cursor=None,
        )

    def create_instance(self, model_name: str, data: dict[str, Any]) -> CMDBInstance:
        self._check_writable("create_instance")
        type_id = self._resolve_type_id(model_name)
        body: dict[str, Any] = {"ci_type": type_id}
        body.update(data)
        payload = self._client.request("POST", _PATH_CI, json_body=body)
        return self._instance_from_write(payload, model_name, data)

    def update_instance(
        self,
        model_name: str,
        instance_id: str,
        data: dict[str, Any],
    ) -> CMDBInstance:
        _ = model_name  # the upstream PUT is addressed by numeric ci id only
        self._check_writable("update_instance")
        payload = self._client.request(
            "PUT", _PATH_INSTANCE.format(instance_id=instance_id), json_body=dict(data)
        )
        return self._instance_from_write(payload, model_name, data)

    def delete_instance(self, model_name: str, instance_id: str) -> None:
        _ = model_name
        self._check_writable("delete_instance")
        try:
            self._client.request("DELETE", _PATH_INSTANCE.format(instance_id=instance_id))
        except ValidationError as exc:
            # The real endpoint exists (verified in the deployment's
            # source); a 405 would mean the deployment disabled the
            # method — surface the dedicated category.
            if "HTTP 405" in exc.message:
                raise UnsupportedOperationError(
                    "veops does not support delete for this instance "
                    "(upstream returned HTTP 405)",
                    upstream_detail=exc.upstream_detail,
                ) from exc
            raise

    def search_instances(
        self,
        model_name: str,
        query: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> Page[CMDBInstance]:
        try:
            page = mapper.decode_search_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        type_id = self._resolve_type_id(model_name)
        params: dict[str, Any] = {
            "q": query,
            "ci_type": type_id,
            "page": page,
            "page_size": page_size,
        }

        def _one() -> dict[str, Any]:
            return self._client.request("GET", _PATH_SEARCH, params=params)

        payload = self._read(_one, operation="search_instances")
        items = mapper.map_search_result(
            payload, model_name=model_name, known_fields=self._known_fields(model_name)
        )
        total_raw = payload.get("total")
        if isinstance(total_raw, (int, float)):
            total: int | None = int(total_raw)
        elif isinstance(total_raw, str) and total_raw.isdigit():
            total = int(total_raw)
        else:
            total = None
        has_more = (
            page * page_size < total if total is not None else len(items) >= page_size
        )
        next_cursor = mapper.encode_search_cursor(page + 1) if has_more else None
        return Page(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            next_cursor=next_cursor,
        )

    def get_relations(
        self,
        model_name: str,
        instance_id: str,
    ) -> list[CMDBRelation]:
        """List the **outbound** edges of an instance — complete and typed.

        Two verified upstream behaviours shape this call (intranet VeOps host,
        2026-09-22; see ``docs/veops-adapter.md``):

        * ``GET /ci_relations/<id>/second_cis`` serves a fixed page of 50
          rows and ignores ``page_size``, while ``numfound`` is the total
          over the *whole* edge set (the opposite of the ci list
          endpoint, where it is per-page).  A bare call therefore returned
          50 of 100 edges **without any signal** — defect **P-13**, which
          hid 2,207 of 5,026 edges (43.9%) deployment-wide.  So the
          adapter sends ``count=all`` and *checks* the row count against
          ``numfound`` instead of trusting it; a mismatch raises.  Page
          looping is **not** a fallback: the query has no ``ORDER BY``, so
          pages can repeat or drop rows.
        * the payload carries no ``relation_type_id``, so the type is
          recovered by re-querying the same endpoint once per live
          relation type (defect **P-12**).  Each typed row *consumes* one
          row of the unfiltered set (keyed by target id), which makes the
          per-type counts provably add up to the whole set: a typed row
          that the unfiltered set does not contain is a contract
          violation (raise), and an unfiltered row no type claimed is
          returned flagged ``adapter.relation_type_unresolved`` — the
          usual cause being an edge that still points at a
          **soft-deleted** relation type.

        Cost: 1 dictionary call per 60 s + 1 unfiltered call + one call
        per relation type *until every edge is attributed* (1 for a
        single-typed hub, 8 at worst).  Rows keep the upstream order.
        """
        _ = model_name  # relations are addressed by numeric ci id only
        path = _PATH_RELATION_SECOND.format(instance_id=instance_id)

        def _rows(params: dict[str, Any], *, what: str) -> list[dict[str, Any]]:
            """One ``count=all`` read, verified against ``numfound``."""
            payload = self._client.request("GET", path, params=params)
            rows = [r for r in (payload.get("second_cis") or []) if isinstance(r, dict)]
            numfound = _as_int(payload.get("numfound"))
            if numfound is None:
                if rows:
                    raise UpstreamServerError(
                        f"veops returned {len(rows)} {what} for instance "
                        f"{instance_id} without a numfound to verify them "
                        "against; refusing to pass on an unverified edge set"
                    )
            elif numfound != len(rows):
                raise UpstreamServerError(
                    f"veops returned {len(rows)} of {numfound} {what} for "
                    f"instance {instance_id} although count=all was sent; "
                    "refusing to pass on a truncated relation list (the "
                    "deployment's default page is 50 rows)"
                )
            return rows

        def _one() -> list[CMDBRelation]:
            rows = _rows({"count": _RELATION_COUNT_ALL}, what="outbound edges")
            # target id -> indexes of ``rows`` still waiting for a type
            pending: dict[str, list[int]] = {}
            for index, row in enumerate(rows):
                pending.setdefault(str(row.get("_id", "")), []).append(index)
            labels: list[str | None] = [None] * len(rows)
            # With no rows there is nothing to type: skip the dictionary
            # read altogether (the common case for a leaf CI).
            if pending:
                for name in self._relation_type_names():
                    if not pending:
                        break  # every edge attributed: stop querying
                    for row in _rows(
                        {"count": _RELATION_COUNT_ALL, "relation_type": name},
                        what=f"edges of relation type {name}",
                    ):
                        key = str(row.get("_id", ""))
                        waiting = pending.get(key)
                        if not waiting:
                            raise UpstreamServerError(
                                f"veops relation type {name!r} returned target "
                                f"{key} for instance {instance_id}, which is "
                                "not in the unfiltered edge set; refusing to "
                                "guess which edge to label"
                            )
                        labels[waiting.pop(0)] = name
                        if not waiting:
                            del pending[key]
            return mapper.map_second_ci_rows(
                rows, source_instance_id=instance_id, relation_types=labels
            )

        result = self._read(_one, operation="get_relations")
        return cast("list[CMDBRelation]", result)

    def _instance_from_write(
        self,
        payload: dict[str, Any],
        model_name: str,
        sent_data: dict[str, Any],
    ) -> CMDBInstance:
        """Map a create/update response onto a :class:`CMDBInstance`.

        The write response shape on 2.6.2 is not contractually fixed
        (it may echo the flat instance, a ``{"cis": [...]}`` /
        ``{"result": [...]}`` list, a ``{"id": ...}`` stub, or
        ``{}``); this mapper handles all three without I/O.
        """
        for list_key in ("cis", "result"):
            if isinstance(payload.get(list_key), list) and payload[list_key]:
                return mapper.map_ci(
                    payload[list_key][0],
                    model_name=model_name,
                    known_fields=self._known_fields(model_name),
                )
        # The live 2.6.2 write response is a ``{"ci_id": <int>}`` stub
        # (verified against the local sandbox, 2026-08); the read
        # ``GET /ci/<id>`` instead wraps the flat instance in
        # ``{"ci": {...}}``.  ``_id``/``id`` cover the remaining shapes.
        ci_payload: Any = payload.get("ci")
        unwrapped: dict[str, Any] = (
            ci_payload if isinstance(ci_payload, dict) else payload
        )
        for id_key in ("_id", "ci_id", "id"):
            if unwrapped.get(id_key) is not None:
                return mapper.map_ci(
                    unwrapped,
                    model_name=model_name,
                    known_fields=self._known_fields(model_name),
                )
        # Degenerate: no id echoed at all.  Keep the sent fields; empty id.
        return CMDBInstance(
            model_name=model_name,
            instance_id="",
            data=dict(sent_data),
            metadata=None,
        )

    # -- intent-level operations (layer-3, rules-gated) ---------------------
    # These back the cmdb_preflight_write / cmdb_create_card /
    # cmdb_update_card / cmdb_link_relations tools.  The gate logic lives
    # in :mod:`cmdb_mcp_adapter.adapters.veops.intent` and
    # :mod:`cmdb_mcp_adapter.rules_gate`; the primitives above are NOT
    # touched by them (backward compatibility of the 3 direct write tools
    # is a hard requirement).

    def _intent(self) -> VeopsIntentOps:
        if self._intent_ops is None:
            self._intent_ops = VeopsIntentOps(self._client, self)
        return self._intent_ops

    def cmdb_preflight_write(
        self,
        model_name: str,
        fields: dict[str, Any],
        ticket: str,
        ci_id: str | None = None,
    ) -> dict[str, Any]:
        """Zero-write verdict over the rules gates + current-state reads."""
        return self._intent().preflight_write(model_name, fields, ticket, ci_id=ci_id)

    def cmdb_create_card(
        self,
        model_name: str,
        ticket: str,
        fields: dict[str, Any],
        relations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Rules-gated create with readback rulers and optional edges."""
        return self._intent().create_card(model_name, ticket, fields, relations=relations)

    def cmdb_update_card(
        self,
        model_name: str,
        ci_id: str,
        ticket: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Rules-gated update with the three post-write rulers."""
        return self._intent().update_card(model_name, ci_id, ticket, fields)

    def cmdb_link_relations(self, ticket: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
        """Verify endpoints and relation types, then create edges (never delete)."""
        return self._intent().link_relations(ticket, edges)


def _filter_instances(
    items: list[CMDBInstance], filters: Filters
) -> list[CMDBInstance]:
    """Client-side equality filtering.  The upstream search ``fl``
    parameter controls *returned fields*, not filtering (verified
    2026-08), so filters are applied here.  ``None`` filter values match
    instances lacking that key; any other value requires an exact
    match."""
    if not filters:
        return items
    return [
        item
        for item in items
        if all(_filter_matches(item.data, key, value) for key, value in filters.items())
    ]


def _filter_matches(data: dict[str, Any], key: str, value: Any) -> bool:
    if value is None:
        return key not in data
    got: Any = data.get(key)
    return bool(got == value)


__all__ = ["VeopsAdapter"]
