"""Normalized, vendor-neutral CMDB data models.

These dataclasses are the public contract shared by every CMDB adapter and
every MCP tool.  They deliberately describe CMDB *concepts* (models, fields,
instances, relations, pages) and never reference a specific vendor.  Vendor-
specific values must be carried in the ``metadata`` extension dictionaries,
never by adding new required normalized fields (see the compatibility rules
in ``docs/mcp-tool-contract.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")

#: The maximum number of items a single page may carry.  Callers requesting a
#: larger ``page_size`` are clamped to this value by the core service so that
#: one response can never become unbounded, regardless of the adapter.
MAX_PAGE_SIZE = 1000


@dataclass(frozen=True)
class ModelField:
    """A single field (attribute) of a CMDB model.

    Attributes:
        name: Stable machine name of the field.  This is the key used in an
            instance's ``data`` mapping.
        type: Vendor-neutral type hint (for example ``"string"``, ``"int"``,
            ``"bool"``, ``"datetime"``, ``"ref"``).  Adapters map their native
            field types onto these strings.
        required: Whether a value for this field is required to create an
            instance.
        description: Human-readable documentation for the field.
        enum_values: Optional list of permitted values when the field is an
            enumerated (fixed-choice) type.
    """

    name: str
    type: str
    required: bool
    description: str = ""
    enum_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CMDBModel:
    """Metadata describing a single CMDB model (entity type).

    Attributes:
        name: Stable machine name of the model.  This is the value passed as
            ``model_name`` to the instance tools and must be unique per
            adapter.
        display_name: Human-readable name.
        description: Human-readable documentation.
        fields: The model's fields in a stable order.
    """

    name: str
    display_name: str
    description: str = ""
    fields: tuple[ModelField, ...] = ()


@dataclass(frozen=True)
class CMDBInstance:
    """A single instance (record) of a CMDB model.

    Attributes:
        model_name: The model this instance belongs to (matches
            :attr:`CMDBModel.name`).
        instance_id: Stable identifier of the instance within the adapter.
            Used to update or delete the instance.
        data: Normalized field values keyed by :attr:`ModelField.name`.  Only
            normalized fields may appear here.
        metadata: Optional vendor-specific extension.  Any value that does not
            map onto a normalized field *must* be placed here, keyed by an
            explicit prefix, so that normalized fields are never displaced.
    """

    model_name: str
    instance_id: str
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class CMDBRelation:
    """A directed relationship between two instances.

    Attributes:
        source_instance_id: The instance the relation originates from.
        target_instance_id: The instance the relation points to.
        relation_type: Vendor-neutral name of the relationship (for example
            ``"runs_on"``, ``"owns"``).
        metadata: Optional vendor-specific extension.
    """

    source_instance_id: str
    target_instance_id: str
    relation_type: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class Page(Generic[T]):
    """A single page of a cursor-paginated result set.

    Pagination semantics (cursor style):
        * ``cursor`` is an opaque, adapter-specific token.  Clients must never
          parse it; they simply echo it back to :func:`get_instances` /
          :func:`search_instances` to fetch the next page.
        * The first page is requested with ``cursor=None``.
        * A page is *empty* when ``items`` is empty.  When the last item of the
          current page was the final item of the result set, ``next_cursor`` is
          ``None`` and ``has_next`` is ``False``.
        * A cursor that has gone past the end (for example one reused after the
          last page) yields an empty page with ``next_cursor=None``; it is not
          an error.
        * ``total`` is the best-effort size of the whole result set.  It is
          ``None`` when the adapter cannot count it (streaming or filtered
          upstreams).  ``has_next``/``next_cursor`` are the authoritative
          "more data?" signals and must be correct even when ``total`` is
          unknown.
    """

    items: list[T]
    page: int
    page_size: int
    total: int | None
    next_cursor: str | None = None

    @property
    def has_next(self) -> bool:
        """Whether a further page is available after this one."""
        return self.next_cursor is not None
