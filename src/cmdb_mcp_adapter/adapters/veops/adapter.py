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

* **``get_instances`` returns one full page.**  The upstream
  ``GET /ci/type/<type_id>`` has no pagination and returns *all*
  instances of the type (up to a few thousand on real deployments).
  ``next_cursor`` is always ``None`` and a non-``None`` cursor argument
  is rejected with a validation error.
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
  The per-type endpoint returns no attribute list, so per-model
  ``fields`` are resolved from the global ``GET /attributes/s``
  dictionary by the deployment's naming convention (``credential`` →
  ``credenti_*``); models with no match get ``fields=()`` (a documented
  degradation — instance values still flow through ``data``).
  The ci_types/attributes lookups are cached with a 60 s TTL.
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
_PATH_ATTRIBUTES = "/attributes/s"

#: TTL (seconds) for the ci_types / attributes caches.  The upstream has
#: no change notification, so a short TTL balances freshness against
#: request volume (one upstream call per entry refresh).
_CACHE_TTL_SECONDS = 60.0


class VeopsAdapter:
    """A :class:`~cmdb_mcp_adapter.core.adapter.CMDBAdapter` backed by
    VeOps 2.6.2."""

    def __init__(self, client: VeopsClient, *, max_retries: int) -> None:
        self._client = client
        self._max_retries = max_retries
        # name -> (numeric ci_type id, fetched_at)
        self._type_ids: dict[str, tuple[int, float]] = {}
        # name -> raw ci_types entry (keeps alias/icon/etc. for display)
        self._type_payloads: dict[str, dict[str, Any]] = {}
        self._type_ids_fetched_at: float | None = None
        # name -> (resolved ModelFields, fetched_at)
        self._model_fields: dict[str, tuple[tuple[ModelField, ...], float]] = {}
        # Raw global attribute dictionary (list of ModelField).
        self._attribute_fields: tuple[ModelField, ...] | None = None
        self._attributes_fetched_at: float | None = None

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
        self._assign_attributes()

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

    def _refresh_attributes(self) -> None:
        """Fetch the global attribute dictionary (retried, 60 s TTL)."""
        payload = self._client.request("GET", _PATH_ATTRIBUTES)
        self._attribute_fields = tuple(mapper.map_attributes(payload))
        self._attributes_fetched_at = self._now()

    def _attributes_available(self) -> bool:
        if self._attributes_fetched_at is None:
            return False
        return self._now() - self._attributes_fetched_at < _CACHE_TTL_SECONDS

    def _ensure_attributes(self) -> None:
        if not self._attributes_available():
            try:
                self._refresh_attributes()
            except CoreError:
                # The attribute dictionary is best-effort: if it is
                # unavailable, models keep ``fields=()`` rather than
                # failing the whole call.
                self._attribute_fields = ()
                self._attributes_fetched_at = self._now()

    def _assign_attributes(self) -> None:
        """Populate :attr:`_model_fields` from the (lazily refreshed)
        ci_types + global attribute caches.  Matching is by the
        deployment's naming convention: an attribute belongs to a type
        when its name starts with ``<type>_`` or with an 8-character
        prefix of the type name (``credential`` → ``credenti_*``).
        Types with no match get ``fields=()``."""
        self._ensure_attributes()
        now = self._now()
        out: dict[str, tuple[tuple[ModelField, ...], float]] = {}
        attrs = self._attribute_fields or ()
        for name in self._type_ids:
            candidates = tuple(f for f in attrs if self._attr_matches_type(f.name, name))
            out[name] = (candidates, now)
        self._model_fields = out

    @staticmethod
    def _attr_matches_type(attr_name: str, type_name: str) -> bool:
        if not attr_name or not type_name:
            return False
        if attr_name == type_name or attr_name.startswith(f"{type_name}_"):
            return True
        short = type_name[:8]
        return len(short) >= 6 and attr_name.startswith(f"{short}_")

    def _known_fields(self, model_name: str) -> tuple[str, ...]:
        """Field names for a model (best effort, cached 60 s)."""
        hit = self._model_fields.get(model_name)
        if (
            hit is not None
            and self._attributes_fetched_at is not None
            and self._now() - self._attributes_fetched_at < _CACHE_TTL_SECONDS
        ):
            return tuple(f.name for f in hit[0])
        self._refresh_ci_types()
        hit = self._model_fields.get(model_name)
        return tuple(f.name for f in hit[0]) if hit else ()

    # -- the seven protocol operations ------------------------------------
    def list_models(self) -> list[CMDBModel]:
        def _one() -> list[CMDBModel]:
            self._refresh_ci_types()
            return mapper.map_ci_types(
                {"ci_types": self._type_ids_names()},
                {name: f for name, (f, _at) in self._model_fields.items()},
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
        if cursor is not None:
            # The upstream instance endpoint is *not* paginated: it
            # returns the whole type in one response, so there is no
            # "next page" concept to cursor across.
            raise ValidationError(
                "veops instance listing is not paginated: the upstream "
                "returns all instances of a type in a single response, so "
                "no cursor is issued or accepted"
            )
        type_id = self._resolve_type_id(model_name)

        def _one() -> dict[str, Any]:
            return self._client.request(
                "GET", _PATH_TYPE_INSTANCES.format(type_id=type_id)
            )

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
        payload = self._client.request(
            "PUT", _PATH_INSTANCE.format(instance_id=instance_id), json_body=dict(data)
        )
        return self._instance_from_write(payload, model_name, data)

    def delete_instance(self, model_name: str, instance_id: str) -> None:
        _ = model_name
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
