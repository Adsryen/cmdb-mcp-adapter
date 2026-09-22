"""Pure mapping between VeOps 2.6.2 payloads and normalized core models.

This module is *pure*: every function takes plain data (dicts / strings)
and returns core dataclasses or plain values, with no I/O and no hidden
state.  That makes it trivially unit-testable and keeps the HTTP concerns
in :mod:`.client` and the orchestration (retry, caching, pagination) in
:mod:`.adapter`.

The shapes below are *verified* against a live OneOps/VeOps 2.6.2
deployment (2026-08; see ``docs/veops-adapter.md``):

* ``GET /ci_types`` returns ``{"ci_types": [ ... ], "numfound": N}`` —
  a **list wrapper** even for the single-type lookup
  ``GET /ci_types/<id|name>`` (which returns one entry in the list).
  A ci_type entry carries ``id``/``name``/``alias``/``unique_key``/
  ``enabled``/``icon``/``parent_ids`` but **no** attribute list.
* ``GET /ci/type/<type_id>`` returns ``{"cis": [ ... ]}`` — the *full*
  instance set for that type in a single response (no pagination
  parameters are honoured).
* ``GET /ci/search`` returns ``{numfound, total, page, result: [...]}``
  with flat instance dicts.
* ``GET /ci_relations/<id>/second_cis`` (and ``/first_cis``) return
  ``{numfound, total, page, second_cis: [...]}`` — flat instance dicts,
  **without** a ``relation_type_id``.  ``numfound`` is the *total over
  the whole edge set* (unlike the ci list endpoint) and ``total`` is the
  number of rows actually returned, so ``count=all`` makes
  ``len(rows) == numfound`` a sound completeness check.  The relation
  type is therefore supplied by the adapter (per-type re-query, see
  :func:`map_second_cis`) and an edge whose type could not be resolved
  is labelled explicitly instead of silently left empty.
* ``GET /ci_types/<type_id>/attributes`` returns
  ``{"attributes": [ ... ], "type_id": n, "unique_id": n, "unique": n}``
  — the type's **attribute binding list**, i.e. the authoritative
  type→attribute mapping.  Each entry is the global attribute record
  (``name`` / ``alias`` / ``value_type`` / ``is_choice`` / ``is_unique``
  / ...) enriched with the *per-binding* ``is_required`` / ``order`` /
  ``default_show`` / ``inherited`` flags and, for choice attributes, the
  server-resolved ``choice_value`` pairs (``[[value, {"label": …}], …]``).
  The list is already sorted by ``(order, id)`` and includes inherited
  attributes; it is not paginated upstream.
* ``GET /attributes/s`` returns the *global* attribute dictionary, with
  no type binding on any entry — useless for field resolution (see
  ``docs/veops-adapter.md``, "Model fields"); the adapter has not called
  it since 0.2.7.
* ``GET /relation_types`` returns a **bare array** ``[{id, name, ...}]``.

Vendor-specific values are never placed into normalized fields: VeOps
metadata keys (``_id``/``_type``/``_updated_at``/``_updated_by``/
``ci_type``/``ci_type_alias``/``unique``/``unique_alias``) are carried in
``CMDBInstance.metadata`` under an explicit prefix (``veops.``),
satisfying the core rule that normalized fields are never displaced.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cmdb_mcp_adapter.core.models import (
    CMDBInstance,
    CMDBModel,
    CMDBRelation,
    ModelField,
)

#: Key (in ``CMDBRelation.metadata``) that marks an edge whose relation
#: type the adapter could **not** resolve.  Namespaced apart from
#: :data:`VENDOR_METADATA_PREFIX` on purpose: the flag is the adapter's
#: own statement, not a vendor value copied out of the payload.  Absent
#: when the type *was* resolved.
RELATION_TYPE_UNRESOLVED_KEY = "adapter.relation_type_unresolved"

#: Prefix for vendor-specific values carried in ``metadata``.  The core
#: contract requires vendor extensions to be explicitly namespaced so they
#: can never collide with a normalized field name.
VENDOR_METADATA_PREFIX = "veops."

#: Instance-dict keys that are VeOps *metadata* (not attribute values).
#: ``_id`` becomes ``instance_id``; the rest are preserved in
#: ``metadata`` under the vendor prefix.  Any other ``_*`` key found in
#: the future is quarantined the same way (defensive).
_INSTANCE_META_KEYS: frozenset[str] = frozenset(
    {"_id", "_type", "_updated_at", "_updated_by", "ci_type", "ci_type_alias"}
)


def _is_metadata_key(key: str) -> bool:
    return key in _INSTANCE_META_KEYS or (key.startswith("_") and key != "_id")


# ---------------------------------------------------------------------------
# Models (ci_types)
# ---------------------------------------------------------------------------
def _value_type_to_core(value_type: Any) -> str:
    """Map the VeOps ``value_type`` code onto a neutral type hint.

    The 2.6.2 attribute dictionary uses numeric codes; the known set is
    small and stable (2 = text/string, 3 = int, 4 = float, 5 = bool,
    6 = datetime, 7 = json).  Unknown codes degrade to ``"string"``
    rather than failing: the type hint is advisory.
    """
    code = str(value_type).strip()
    return {
        "1": "int",
        "2": "string",
        "3": "float",
        "4": "bool",
        "5": "datetime",
        "6": "json",
    }.get(code, "string")


def map_ci_type(
    raw: dict[str, Any],
    fields: tuple[ModelField, ...] = (),
) -> CMDBModel:
    """Map one VeOps ``ci_types[]`` entry onto a :class:`CMDBModel`.

    ``fields`` are the model's declared attributes, resolved by the
    adapter from the type's binding list (see :func:`map_type_attribute`):
    the ci_type payload itself carries no attribute list (verified: the
    single-type lookup does not add one).  A model whose binding list
    could not be fetched degrades to ``fields=()``.
    """
    alias = raw.get("alias")
    return CMDBModel(
        name=str(raw.get("name", "")),
        display_name=str(alias) if alias else str(raw.get("name", "")),
        description=str(alias or "") and f"VeOps CI type id={raw.get('id')}" or "",
        fields=fields,
    )


def map_ci_types(
    payload: dict[str, Any],
    fields_by_name: dict[str, tuple[ModelField, ...]] | None = None,
) -> list[CMDBModel]:
    """Map a ``GET /ci_types`` payload (``{"ci_types": [...]}``) onto
    models.

    ``fields_by_name`` optionally supplies, per ci_type *name*, the
    attributes declared for it (from the type's binding list).  Entries
    with no match get ``fields=()``.
    """
    raws = payload.get("ci_types") or []
    fields_by_name = fields_by_name or {}
    models: list[CMDBModel] = []
    for raw in raws:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", ""))
        models.append(map_ci_type(raw, fields_by_name.get(name, ())))
    return models


def _choice_value_to_enum(raw: Any) -> tuple[str, ...] | None:
    """Normalise the upstream ``choice_value`` list into enum members.

    ``/ci_types/<type_id>/attributes`` resolves enumerations server-side
    (choice table, webhook or CI-backed source) and answers a list of
    ``[value, option]`` pairs — verified 2026-09-21 on the
    intranet deployment: ``[["生产", {"label": "生产"}],
    ["UAT", {…}], …]``.
    Only ``value`` is a *storable* value; the ``option`` label is display
    text, so the pairs are projected onto their first element.

    Returns ``None`` — never an empty tuple — when nothing is known:
    a non-choice attribute carries no ``choice_value`` key at all, and a
    choice attribute whose webhook is currently unreachable answers
    ``[]``.  Both mean "no constraint is known", which is *not* the same
    claim as "no value is allowed".
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    values: list[str] = []
    for item in raw:
        candidate: Any = item[0] if isinstance(item, (list, tuple)) and item else item
        if candidate is None or isinstance(candidate, (dict, list, tuple)):
            continue
        text = str(candidate).strip()
        if text and text not in values:
            values.append(text)
    return tuple(values) or None


def map_type_attribute(raw: dict[str, Any]) -> ModelField | None:
    """Map one ``/ci_types/<type_id>/attributes`` entry onto a
    :class:`ModelField`.

    ``required`` comes from the **binding** row's ``is_required`` (the
    global attribute dictionary has no such column: required-ness in
    VeOps is per type×attribute, not per attribute).  ``enum_values``
    comes from the resolved ``choice_value`` list (see
    :func:`_choice_value_to_enum`).  Returns ``None`` for entries without
    a usable name.
    """
    name = str(raw.get("name") or "")
    if not name:
        return None
    alias = raw.get("alias")
    return ModelField(
        name=name,
        type=_value_type_to_core(raw.get("value_type")),
        required=bool(raw.get("is_required")),
        description=str(alias or ""),
        enum_values=_choice_value_to_enum(raw.get("choice_value")),
    )


def map_type_attributes(payload: dict[str, Any]) -> list[ModelField]:
    """Map a ``GET /ci_types/<type_id>/attributes`` payload onto a field
    list.  The upstream's ``(order, id)`` sequence is preserved."""
    raws = payload.get("attributes") or []
    fields: list[ModelField] = []
    for raw in raws:
        if not isinstance(raw, dict):
            continue
        field = map_type_attribute(raw)
        if field is not None:
            fields.append(field)
    return fields


# ---------------------------------------------------------------------------
# Instances (flat dicts)
# ---------------------------------------------------------------------------
def map_ci(
    raw: dict[str, Any],
    *,
    model_name: str,
    known_fields: tuple[str, ...] = (),
) -> CMDBInstance:
    """Map one VeOps *flat* instance dict (a ``cis[]`` / ``result[]`` /
    ``second_cis[]`` entry) onto a :class:`CMDBInstance`.

    VeOps instance dicts are flat: ``_id``/``_type``/``_updated_at``/
    ``_updated_by``/``ci_type``/``ci_type_alias`` plus one key per
    attribute value (e.g. ``credenti_environment``).  The split:

    * ``_id`` -> ``instance_id`` (stringified);
    * the other metadata keys (plus any other ``_*`` key and
      ``unique``/``unique_alias``) -> ``metadata`` under ``veops.``;
    * attribute values -> ``data``, restricted to ``known_fields`` when
      supplied (anything else is quarantined into ``metadata`` so a
      normalized field is never displaced);
    * ``metadata["veops.id"]`` always preserves the native id.

    The native id is read from ``_id`` for read-shaped payloads, or
    ``ci_id`` for the write-stub shape (``POST``/``PUT`` on 2.6.2 answer
    ``{"ci_id": <int>}`` — verified against the local sandbox, 2026-08).
    A ``{"ci": {...}}`` wrapper (the ``GET /ci/<id>`` read shape) is
    unwrapped first.
    """
    ci_payload: Any = raw.get("ci")
    raw = ci_payload if isinstance(ci_payload, dict) else raw
    if raw.get("_id") is not None:
        instance_id = str(raw["_id"])
    elif raw.get("ci_id") is not None:
        instance_id = str(raw["ci_id"])
    else:
        instance_id = ""
    data_raw = raw

    metadata: dict[str, Any] = {}
    for key in (
        "_type",
        "_updated_at",
        "_updated_by",
        "ci_type",
        "ci_type_alias",
        "unique",
        "unique_alias",
    ):
        if key in raw:
            metadata[f"{VENDOR_METADATA_PREFIX}{key}"] = raw[key]
    # Defensive: any other underscore-prefixed key is vendor metadata too.
    for key in data_raw:
        if key != "_id" and key.startswith("_") and key not in metadata:
            metadata[f"{VENDOR_METADATA_PREFIX}{key}"] = data_raw[key]
    metadata[f"{VENDOR_METADATA_PREFIX}id"] = instance_id

    values = {
        k: v
        for k, v in data_raw.items()
        if k not in ("_id", "ci_id") and not _is_metadata_key(k)
        and k not in ("unique", "unique_alias")
    }
    if known_fields:
        known = set(known_fields)
        normalized: dict[str, Any] = {k: v for k, v in values.items() if k in known}
        for k, v in values.items():
            if k not in known:
                metadata[f"{VENDOR_METADATA_PREFIX}{k}"] = v
    else:
        normalized = values

    return CMDBInstance(
        model_name=model_name,
        instance_id=instance_id,
        data=normalized,
        metadata=metadata or None,
    )


def map_cis(
    payload: dict[str, Any],
    *,
    model_name: str,
    known_fields: tuple[str, ...] = (),
) -> list[CMDBInstance]:
    """Map a ``{"cis": [...]}`` payload onto a list of instances."""
    items = payload.get("cis") or []
    return [
        map_ci(item, model_name=model_name, known_fields=known_fields)
        for item in items
        if isinstance(item, dict)
    ]


def map_search_result(
    payload: dict[str, Any],
    *,
    model_name: str,
    known_fields: tuple[str, ...] = (),
) -> list[CMDBInstance]:
    """Map a ``{"result": [...]}`` search payload onto instances."""
    items = payload.get("result") or []
    return [
        map_ci(item, model_name=model_name, known_fields=known_fields)
        for item in items
        if isinstance(item, dict)
    ]


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------
def map_second_cis(
    payload: dict[str, Any],
    *,
    source_instance_id: str,
    relation_type: str = "",
) -> list[CMDBRelation]:
    """Map a ``GET /ci_relations/<id>/second_cis`` payload.

    The response carries flat *target* instance dicts (``second_cis[]``)
    — the source id and the relation type are **not** included in it
    (verified: no ``relation_type_id`` field).  ``relation_type`` is
    supplied by the adapter from the ``/relation_types`` dictionary when
    it can resolve one; an edge it cannot resolve is mapped with
    ``relation_type=""`` **and** flagged
    (``metadata["adapter.relation_type_unresolved"]``) so the gap is
    visible instead of silent.  The raw target payload is preserved in
    each relation's ``metadata`` (vendor-prefixed) so nothing is lost.
    """
    rows = [i for i in (payload.get("second_cis") or []) if isinstance(i, dict)]
    return map_second_ci_rows(
        rows,
        source_instance_id=source_instance_id,
        relation_types=[relation_type] * len(rows),
    )


def map_second_ci_rows(
    rows: list[dict[str, Any]],
    *,
    source_instance_id: str,
    relation_types: Sequence[str | None],
) -> list[CMDBRelation]:
    """Map already-extracted target rows, one relation per row.

    ``relation_types`` is **parallel to** ``rows`` (same order, same
    length): the resolved type name, or ``None`` for an edge whose type
    could not be resolved.  An unresolved edge keeps the empty normalized
    field (the core contract forbids inventing a vendor-neutral name) and
    is flagged in ``metadata`` instead.  Kept pure — the *selection* of
    which row carries which type is the adapter's job (see
    ``VeopsAdapter.get_relations``).

    Raises:
        ValueError: when the two sequences are not the same length (a
            label could then not be trusted to belong to its edge).
    """
    if len(rows) != len(relation_types):
        raise ValueError(
            f"{len(rows)} edge rows but {len(relation_types)} relation-type labels"
        )
    relations: list[CMDBRelation] = []
    for item, relation_type in zip(rows, relation_types, strict=True):
        metadata: dict[str, Any] = {}
        for key, value in item.items():
            if key != "_id":
                metadata[f"{VENDOR_METADATA_PREFIX}{key}"] = value
        if relation_type is None:
            metadata[RELATION_TYPE_UNRESOLVED_KEY] = True
        relations.append(
            CMDBRelation(
                source_instance_id=source_instance_id,
                target_instance_id=str(item.get("_id", "")),
                relation_type=relation_type or "",
                metadata=metadata or None,
            )
        )
    return relations


def map_first_cis(
    payload: dict[str, Any],
    *,
    target_instance_id: str,
    relation_type: str = "",
) -> list[CMDBRelation]:
    """Symmetric mapping for ``GET /ci_relations/<id>/first_cis``
    (inbound edges: the listed instances are the *sources*)."""
    items = payload.get("first_cis") or []
    relations: list[CMDBRelation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata: dict[str, Any] = {}
        for key, value in item.items():
            if key != "_id":
                metadata[f"{VENDOR_METADATA_PREFIX}{key}"] = value
        relations.append(
            CMDBRelation(
                source_instance_id=str(item.get("_id", "")),
                target_instance_id=target_instance_id,
                relation_type=relation_type,
                metadata=metadata or None,
            )
        )
    return relations


def map_relation_types(payload: Any) -> dict[int, str]:
    """Map the ``GET /relation_types`` **bare array** into an
    ``id -> name`` dictionary for resolving relation type names.

    Soft-deleted entries are dropped: upstream ``get_all()`` already
    filters them (verified 2026-09-22 — 11 rows exist in the table, 8
    live names are returned, the deleted ``hosts``/``4``/``5``/``6`` are
    absent), and a deleted name would be *rejected* by the ``second_cis``
    ``relation_type`` filter with HTTP 400, so it must never be queried.
    """
    if not isinstance(payload, list):
        return {}
    out: dict[int, str] = {}
    for raw in payload:
        if not isinstance(raw, dict) or raw.get("deleted"):
            continue
        attr_id = raw.get("id")
        if not isinstance(attr_id, int) and attr_id is not None:
            try:
                attr_id = int(attr_id)
            except (TypeError, ValueError):
                continue
        if attr_id is None:
            continue
        out[attr_id] = str(raw.get("name", ""))
    return out


# ---------------------------------------------------------------------------
# Pagination (search is page-based; instances are a single full page)
# ---------------------------------------------------------------------------
def encode_search_cursor(page: int) -> str:
    """Encode a 1-based *search* page number into a core opaque cursor.

    Only the search operation is page-paginated upstream; instance
    listing returns one full page (no cursor ever issued).  The cursor
    is opaque to MCP clients and only ever parsed by this adapter via
    :func:`decode_search_cursor`.
    """
    return f"veops-search-page-{int(page)}"


def decode_search_cursor(cursor: str | None) -> int:
    """Decode a search cursor back to the 1-based upstream page.

    ``None`` (first page) maps to page 1.  A cursor this adapter did not
    produce raises ``ValueError`` — the caller converts that into a
    :class:`ValidationError`.
    """
    if cursor is None:
        return 1
    prefix = "veops-search-page-"
    if not cursor.startswith(prefix):
        raise ValueError(f"unrecognized cursor {cursor!r}")
    try:
        page = int(cursor[len(prefix):])
    except ValueError as exc:
        raise ValueError(f"unrecognized cursor {cursor!r}") from exc
    if page < 1:
        raise ValueError(f"cursor page must be >= 1, got {page}")
    return page


__all__ = [
    "RELATION_TYPE_UNRESOLVED_KEY",
    "VENDOR_METADATA_PREFIX",
    "decode_search_cursor",
    "encode_search_cursor",
    "map_type_attribute",
    "map_type_attributes",
    "map_ci",
    "map_ci_type",
    "map_ci_types",
    "map_cis",
    "map_first_cis",
    "map_relation_types",
    "map_second_ci_rows",
    "map_second_cis",
    "map_search_result",
]
