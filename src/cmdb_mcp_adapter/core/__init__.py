"""Vendor-neutral unified CMDB core.

This package owns the normalized data models, the stable error categories,
the adapter protocol, and the :class:`CoreService` that MCP tools call.  It
must not import any vendor adapter; adapters implement the protocol here.

Public API (import from :mod:`cmdb_mcp_adapter.core`):

    Models:     :class:`ModelField`, :class:`CMDBModel`, :class:`CMDBInstance`,
                :class:`CMDBRelation`, :class:`Page`
    Errors:     :class:`CoreError`, :class:`ErrorCategory`, and the concrete
                subclasses, plus :func:`normalize`
    Adapter:    :class:`CMDBAdapter`
    Service:    :class:`CoreService`

The seven stable MCP tool names are exported as constants so that the tool
layer and the documentation reference a single source of truth.  These names
are public API and must not change.
"""

from __future__ import annotations

from cmdb_mcp_adapter.core.adapter import CMDBAdapter, Filters
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

#: The seven stable MCP tool names.  These are part of the public contract:
#: adding a new CMDB adapter must never change them.
TOOL_GET_MODELS = "get_models"
TOOL_GET_INSTANCES = "get_instances"
TOOL_CREATE_INSTANCE = "create_instance"
TOOL_UPDATE_INSTANCE = "update_instance"
TOOL_DELETE_INSTANCE = "delete_instance"
TOOL_SEARCH_INSTANCES = "search_instances"
TOOL_GET_RELATIONS = "get_relations"

ALL_TOOL_NAMES: tuple[str, ...] = (
    TOOL_GET_MODELS,
    TOOL_GET_INSTANCES,
    TOOL_CREATE_INSTANCE,
    TOOL_UPDATE_INSTANCE,
    TOOL_DELETE_INSTANCE,
    TOOL_SEARCH_INSTANCES,
    TOOL_GET_RELATIONS,
)

__all__ = [
    "ALL_TOOL_NAMES",
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
    "InternalError",
    "MAX_PAGE_SIZE",
    "ModelField",
    "NotFoundError",
    "Page",
    "RateLimitError",
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
    "normalize",
]
