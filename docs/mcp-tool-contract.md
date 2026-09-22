# MCP Tool Contract

This document is the **public API contract** for the seven CMDB tools
exposed by the `cmdb-mcp-adapter` MCP server.  Tool names, parameter names,
and normalized field names are stable and must not change when new vendor
adapters are added.

## Stability rules

1. **Tool names are frozen.**  The seven names below are public API.  Adding
   a new CMDB adapter (for example the veops adapter) must **not** rename,
   remove, or re-purpose any of them.  If a new capability is needed, add a
   new tool; do not modify the existing seven.
2. **Vendor fields go in `metadata`.**  Every normalized model carries an
   optional `metadata` dictionary (JSON-serializable).  Any vendor-specific
   value that does not map onto a normalized field **must** be placed in
   `metadata` under an explicit key.  Vendor values must never replace or
   rename a required normalized field.
3. **Error categories are stable.**  The category names in the
   [Error categories](#error-categories) table are part of the contract.
   New categories may be added, but existing ones must not change meaning.

## Tools

### `get_models`

List the CMDB models this adapter exposes.

**Input:** none (empty object).

**Output (JSON):** a list of model objects:

```json
[
  {
    "name": "host",
    "display_name": "Host",
    "description": "A compute host.",
    "fields": [
      {
        "name": "hostname",
        "type": "string",
        "required": true,
        "description": "FQDN.",
        "enum_values": null
      }
    ]
  }
]
```

### `get_instances`

List one page of instances of a model (cursor-paginated).

**Input:**

| Name          | Type   | Required | Description                              |
|---------------|--------|----------|------------------------------------------|
| `model_name`  | string | yes      | The model to list.                       |
| `page_size`   | int    | no       | Items per page (default 50, max 1000).   |
| `cursor`      | string \| null | no | Opaque cursor from a previous page. |
| `filters`     | object | no       | Field-name → value equality filters.     |

**Output (JSON):** a page object:

```json
{
  "items": [
    {
      "model_name": "host",
      "instance_id": "host-1",
      "data": {"hostname": "web-1.example.com", "ip": "10.0.0.1"},
      "metadata": null
    }
  ],
  "page": 1,
  "page_size": 3,
  "total": 8,
  "next_cursor": "2",
  "has_next": true
}
```

### `create_instance`

Create a new instance of a model.

**Input:**

| Name         | Type   | Required | Description              |
|--------------|--------|----------|--------------------------|
| `model_name` | string | yes      | The model to create.     |
| `data`       | object | yes      | Field values to store.   |

**Output (JSON):** the created instance (same shape as an `items` element
above).

### `update_instance`

Update an existing instance of a model.

**Input:**

| Name         | Type   | Required | Description                  |
|--------------|--------|----------|------------------------------|
| `model_name` | string | yes      | The model.                   |
| `instance_id`| string | yes      | The instance to update.      |
| `data`       | object | yes      | Fields to merge into the instance. |

**Output (JSON):** the updated instance.

### `delete_instance`

Delete an existing instance of a model.

**Input:**

| Name         | Type   | Required | Description                  |
|--------------|--------|----------|------------------------------|
| `model_name` | string | yes      | The model.                   |
| `instance_id`| string | yes      | The instance to delete.      |

**Output (JSON):** the string `"deleted"`.

### `search_instances`

Free-text search over a model's instances (cursor-paginated).

**Input:**

| Name         | Type   | Required | Description                  |
|--------------|--------|----------|------------------------------|
| `model_name` | string | yes      | The model to search.         |
| `query`      | string | yes      | The search string.           |
| `page_size`  | int    | no       | Items per page (default 50). |
| `cursor`     | string \| null | no | Opaque cursor.          |

**Output (JSON):** a page object (same shape as `get_instances`).

### `get_relations`

List **every** relation originating from an instance (outbound edges).

The result is never partial: there is no `page`/`cursor` argument because
the adapter returns the complete edge set.  If the upstream cannot prove
completeness, the call **fails** with `upstream_server` instead of returning
a short list — callers may rely on the count.

**Input:**

| Name         | Type   | Required | Description                  |
|--------------|--------|----------|------------------------------|
| `model_name` | string | yes      | The model.                   |
| `instance_id`| string | yes      | The source instance.         |

**Output (JSON):** a list of relation objects:

```json
[
  {
    "source_instance_id": "host-1",
    "target_instance_id": "svc-orders",
    "relation_type": "runs",
    "metadata": null
  }
]
```

`relation_type` is the upstream type name (`承载`, `runs_on`, …).  An empty
string means the adapter could not attribute that one edge; such a row also
carries `"metadata": {"adapter.relation_type_unresolved": true, ...}` so the
gap cannot be mistaken for "this edge has no type".  See
[veops-adapter.md — Relations](veops-adapter.md) for when that happens.

## Pagination semantics

All paginated tools (`get_instances`, `search_instances`) use a **cursor**
style:

* The **first** page is requested with `cursor=null` (or omitted).
* Each page response includes `next_cursor` (a string) and `has_next`
  (boolean).  When `has_next` is `false` (or `next_cursor` is `null`), no
  further pages exist.
* `next_cursor` is an **opaque** token.  Clients must not parse it; they
  simply echo it back in the next request.
* `total` is the best-effort size of the whole result set.  It may be
  `null` when the adapter cannot count.  `has_next` / `next_cursor` are the
  authoritative "more data?" signals.
* A cursor past the end of the result set yields an empty page
  (`items=[]`, `has_next=false`), **not** an error.
* `page_size` is clamped to `[1, 1000]`.  Requesting a larger value is not
  an error; the server clamps it.

## Error categories

When a tool call fails, the MCP `CallToolResult` has `isError=true` and its
text content is `"<category>: <message>"`.  The raw upstream detail (if any)
is **never** included in the client-visible text.

| Category              | Meaning                                            |
|-----------------------|----------------------------------------------------|
| `validation`          | Input failed contract validation.                  |
| `configuration`       | Adapter or server configuration is missing/invalid.|
| `authentication`      | Upstream rejected the adapter's credentials (401). |
| `authorization`       | Upstream denied the action (403).                  |
| `not_found`           | The requested resource does not exist (404).       |
| `timeout`             | Upstream did not respond in time.                  |
| `rate_limit`          | Upstream throttled the request (429).              |
| `upstream_server`     | Upstream returned a 5xx or internal error.         |
| `unsupported_operation`| The adapter does not support this operation.     |
| `internal`            | An unexpected, uncategorized failure.              |

## Running the server

This contract is transport-agnostic: the same seven tools are served over
both the stdio transport (host-launched local process) and the Streamable
HTTP transport (long-running server).  See
[`docs/local-stdio.md`](local-stdio.md) for how to run the stdio path
(install, MCP host configuration, mock mode, Inspector debugging) and
[`docs/remote-http.md`](remote-http.md) for the HTTP path.

## Adding a new CMDB adapter

To add a new vendor adapter:

1. Implement the `CMDBAdapter` protocol (see
   `src/cmdb_mcp_adapter/core/adapter.py`) with your vendor's HTTP calls.
2. Translate vendor errors into the `CoreError` subclasses in
   `src/cmdb_mcp_adapter/core/errors.py`.
3. Register your adapter factory in
   `cmdb_mcp_adapter.server.app.register_adapter("<name>", factory)`.
4. **Do not** rename, remove, or re-purpose any of the seven tool names.
5. **Do not** add new required fields to the normalized models; put
   vendor-specific values in `metadata`.
