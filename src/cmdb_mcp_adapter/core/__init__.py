"""Vendor-neutral unified CMDB core.

This package owns the normalized data models, the stable error categories,
the adapter protocol, and the :class:`CoreService` that MCP tools call.  It
must not import any vendor adapter; adapters implement the protocol here.

Public API (import from :mod:`cmdb_mcp_adapter.core`):

    Models:     :class:`ModelField`, :class:`CMDBModel`, :class:`CMDBInstance`,
                :class:`CMDBRelation`, :class:`Page`
    Errors:     :class:`CoreError`, :class:`ErrorCategory`, and the concrete
                subclasses, plus :func:`normalize`
    Adapter:    :class:`CMDBAdapter`, :class:`IntentAdapter`
    Service:    :class:`CoreService`

The eleven stable MCP tool names (seven primitives + four intent-level
``cmdb_*`` tools) are exported as constants so that the tool layer and the
documentation reference a single source of truth.  The original seven names
are public API and must not change; the four intent tools are additive.
"""

from __future__ import annotations

from cmdb_mcp_adapter.core.adapter import CMDBAdapter, Filters, IntentAdapter
from cmdb_mcp_adapter.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    CoreError,
    ErrorCategory,
    InternalError,
    NotFoundError,
    RateLimitError,
    TimeoutError,
    UnsupportedOperationError,
    UpstreamServerError,
    ValidationError,
    normalize,
)
from cmdb_mcp_adapter.core.models import (
    MAX_PAGE_SIZE,
    CMDBInstance,
    CMDBModel,
    CMDBRelation,
    ModelField,
    Page,
)
from cmdb_mcp_adapter.core.service import CoreService

#: The stable MCP tool names.  The first seven are the original public
#: contract; adding a new CMDB adapter must never change them.  The four
#: ``cmdb_*`` intent tools (layer-3) extend the surface additively.
TOOL_GET_MODELS = "get_models"
TOOL_GET_INSTANCES = "get_instances"
TOOL_CREATE_INSTANCE = "create_instance"
TOOL_UPDATE_INSTANCE = "update_instance"
TOOL_DELETE_INSTANCE = "delete_instance"
TOOL_SEARCH_INSTANCES = "search_instances"
TOOL_GET_RELATIONS = "get_relations"
TOOL_CMDB_PREFLIGHT_WRITE = "cmdb_preflight_write"
TOOL_CMDB_CREATE_CARD = "cmdb_create_card"
TOOL_CMDB_UPDATE_CARD = "cmdb_update_card"
TOOL_CMDB_LINK_RELATIONS = "cmdb_link_relations"

ALL_TOOL_NAMES: tuple[str, ...] = (
    TOOL_GET_MODELS,
    TOOL_GET_INSTANCES,
    TOOL_CREATE_INSTANCE,
    TOOL_UPDATE_INSTANCE,
    TOOL_DELETE_INSTANCE,
    TOOL_SEARCH_INSTANCES,
    TOOL_GET_RELATIONS,
    TOOL_CMDB_PREFLIGHT_WRITE,
    TOOL_CMDB_CREATE_CARD,
    TOOL_CMDB_UPDATE_CARD,
    TOOL_CMDB_LINK_RELATIONS,
)

#: The subset of :data:`ALL_TOOL_NAMES` that mutates CMDB state.  A
#: read-only adapter exposes all tools *except* these three.
WRITE_TOOL_NAMES: tuple[str, ...] = (
    TOOL_CREATE_INSTANCE,
    TOOL_UPDATE_INSTANCE,
    TOOL_DELETE_INSTANCE,
)

#: The intent-level tools that mutate CMDB state.  ``cmdb_preflight_write``
#: is deliberately NOT here: it performs zero writes and stays visible in
#: read-only mode as a dry-run aid.
INTENT_WRITE_TOOL_NAMES: tuple[str, ...] = (
    TOOL_CMDB_CREATE_CARD,
    TOOL_CMDB_UPDATE_CARD,
    TOOL_CMDB_LINK_RELATIONS,
)

#: The read-only tool set: everything in :data:`ALL_TOOL_NAMES` except the
#: mutating primitives and the intent writes.  Order is preserved from
#: ``ALL_TOOL_NAMES``.
READONLY_TOOL_NAMES: tuple[str, ...] = tuple(
    name
    for name in ALL_TOOL_NAMES
    if name not in WRITE_TOOL_NAMES and name not in INTENT_WRITE_TOOL_NAMES
)

__all__ = [
    "ALL_TOOL_NAMES",
    "INTENT_WRITE_TOOL_NAMES",
    "AuthenticationError",
    "AuthorizationError",
    "CMDBAdapter",
    "CMDBInstance",
    "CMDBModel",
    "CMDBRelation",
    "ConfigurationError",
    "CoreError",
    "CoreService",
    "ErrorCategory",
    "Filters",
    "IntentAdapter",
    "InternalError",
    "MAX_PAGE_SIZE",
    "ModelField",
    "NotFoundError",
    "Page",
    "RateLimitError",
    "READONLY_TOOL_NAMES",
    "TOOL_CMDB_CREATE_CARD",
    "TOOL_CMDB_LINK_RELATIONS",
    "TOOL_CMDB_PREFLIGHT_WRITE",
    "TOOL_CMDB_UPDATE_CARD",
    "TOOL_CREATE_INSTANCE",
    "TOOL_DELETE_INSTANCE",
    "TOOL_GET_INSTANCES",
    "TOOL_GET_MODELS",
    "TOOL_GET_RELATIONS",
    "TOOL_SEARCH_INSTANCES",
    "TOOL_UPDATE_INSTANCE",
    "TimeoutError",
    "UpstreamServerError",
    "UnsupportedOperationError",
    "ValidationError",
    "WRITE_TOOL_NAMES",
    "normalize",
]
