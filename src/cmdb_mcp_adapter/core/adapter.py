"""The vendor-agnostic CMDB adapter protocol.

A :class:`CMDBAdapter` is the boundary the core service talks to.  Each
concrete adapter (for example a vendor adapter, or the mock adapter
used for tests) translates the normalized core models onto a specific CMDB's
native API and translates vendor failures onto :class:`~cmdb_mcp_adapter.core.errors.CoreError`.

All methods are synchronous.  The MVP targets CMDB backends that are reached
over plain HTTP; adapters that later need async I/O can wrap their own
event loops internally as long as this surface stays synchronous.

An adapter may implement a subset of the operations.  Any operation it cannot
perform must raise :class:`~cmdb_mcp_adapter.core.errors.UnsupportedOperationError`
rather than failing with a generic error, so that clients can tell "not
supported" apart from "the backend is down".
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from cmdb_mcp_adapter.core.models import (
    CMDBInstance,
    CMDBModel,
    CMDBRelation,
    Page,
)

#: A free-form structured filter applied to ``get_instances``.  Keys are
#: normalized field names; values are scalar values or lists of values for
#: multi-value equality.  Adapters interpret what they can and ignore (or map)
#: the rest.
Filters = dict[str, Any]


class CMDBAdapter(Protocol):
    """A vendor CMDB backend, expressed in normalized core terms.

    Implementations must be constructible without performing network I/O at
    construction time; any connection or credential handshake belongs in a
    separate step or is lazy, so that a bad configuration is detected by
    :func:`~cmdb_mcp_adapter.server.app.create_app` rather than by an adapter
    constructor side effect.
    """

    def list_models(self) -> list[CMDBModel]:
        """Return the metadata for every model this adapter exposes."""

    def get_instances(
        self,
        model_name: str,
        *,
        page_size: int,
        cursor: str | None,
        filters: Filters,
    ) -> Page[CMDBInstance]:
        """Return one page of instances of ``model_name``.

        ``cursor`` is ``None`` for the first page; otherwise it is the opaque
        token returned in a previous :class:`Page`'s ``next_cursor``.
        """

    def create_instance(self, model_name: str, data: dict[str, Any]) -> CMDBInstance:
        """Create a new instance and return the stored record."""

    def update_instance(
        self,
        model_name: str,
        instance_id: str,
        data: dict[str, Any],
    ) -> CMDBInstance:
        """Update an existing instance and return the updated record."""

    def delete_instance(self, model_name: str, instance_id: str) -> None:
        """Delete an existing instance."""

    def search_instances(
        self,
        model_name: str,
        query: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> Page[CMDBInstance]:
        """Free-text search over ``model_name``; cursor-paginated like get_instances."""

    def get_relations(
        self,
        model_name: str,
        instance_id: str,
    ) -> list[CMDBRelation]:
        """Return the relations that originate from the given instance."""


@runtime_checkable
class IntentAdapter(Protocol):
    """Optional capability: intent-level, rules-gated CMDB write tools.

    Adapters that provide the layer-3 tools (:func:`cmdb_preflight_write`
    etc.) implement this protocol; the core service degrades those tools to
    :class:`~cmdb_mcp_adapter.core.errors.UnsupportedOperationError` for
    adapters that do not.  Kept separate from :class:`CMDBAdapter` so the
    original seven-tool contract surface stays frozen.
    """

    def cmdb_preflight_write(
        self,
        model_name: str,
        fields: dict[str, Any],
        ticket: str,
        ci_id: str | None = None,
    ) -> dict[str, Any]:
        """Zero-write verdict over the rules gates plus current-state reads."""

    def cmdb_create_card(
        self,
        model_name: str,
        ticket: str,
        fields: dict[str, Any],
        relations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Rules-gated create with readback rulers and optional edges."""

    def cmdb_update_card(
        self,
        model_name: str,
        ci_id: str,
        ticket: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Rules-gated update with the three post-write rulers."""

    def cmdb_link_relations(self, ticket: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
        """Create verified relation edges (never delete)."""
