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
* **``get_relations``** returns the *outbound* edge targets of an
  instance (``GET /ci_relations/<id>/second_cis``).  The response does
  not carry a relation type id, so ``relation_type`` is empty
  (documented limitation); the raw target payload is preserved in each
  relation's ``metadata``.
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

import time
from collections.abc import Callable
from typing import Any, cast

from cmdb_mcp_adapter.adapters.veops import mapper
from cmdb_mcp_adapter.adapters.veops.client import VeopsClient
from cmdb_mcp_adapter.core.adapter import Filters
from cmdb_mcp_adapter.core.errors import (
    CoreError,
    UnsupportedOperationError,
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


class VeopsAdapter:
    """A :class:`~cmdb_mcp_adapter.core.adapter.CMDBAdapter` backed by
    VeOps 2.6.2.

    When constructed with ``readonly=True`` (the ``VEOPS_READONLY``
    switch) the three write operations
    (``create_instance`` / ``update_instance`` / ``delete_instance``)
    raise :class:`UnsupportedOperationError` before any upstream call;
    all read operations are unaffected.
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
        _ = model_name  # relations are addressed by numeric ci id only

        def _one() -> list[CMDBRelation]:
            payload = self._client.request(
                "GET", _PATH_RELATION_SECOND.format(instance_id=instance_id)
            )
            # The response carries no relation_type_id (verified
            # 2026-08), so the type stays empty (documented limitation).
            return mapper.map_second_cis(
                payload,
                source_instance_id=instance_id,
                relation_type="",
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
