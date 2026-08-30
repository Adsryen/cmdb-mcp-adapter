# VeOps CMDB Adapter

This document describes the `veops` adapter: how it maps the normalized
core contract onto the **verified** OneOps/VeOps 2.6.2 HTTP API, how to
configure it, how upstream errors are mapped onto stable core
categories, and how to test the adapter without a real CMDB.

It is the reference for the `MCP_ADAPTER=veops` deployment.  The adapter
lives entirely under `src/cmdb_mcp_adapter/adapters/veops/`; the core
package and the MCP tool layer contain no veops-specific logic.

## Verified API contract

The endpoints, paths, and JSON shapes below are **confirmed against a
live OneOps/VeOps 2.6.2 deployment** (veops.example.com:8000, probed
2026-08-25).  The API is mounted at `http://<host>:8000` (the nginx
front proxy in front of `cmdb-api:5000`); `VEOPS_BASE_URL` should point
at that address.

### Authentication (key/secret signing)

VeOps 2.6.2 authenticates service-to-service calls with a per-user
`key`/`secret` pair (the `users.key` / `users.secret` columns in the
VeOps database; the VeOps *user management* page exposes the same
values).  There is **no** `Authorization` header.  Every request —
`GET`/`POST`/`PUT`/`DELETE` — carries two credential parameters,
`_key` and `_secret`, whose **location and signature base depend on
whether the request has a JSON body** (verified against the local
sandbox 2026-08):

* `_key` — the user's key (an identity, not a secret);
* `_secret` — the request signature, computed as:

```
_secret = sha1_hex( path + secret + "".join(str(v) for k, v in sorted(values)) )
```

where `path` is the raw request path (no query string, e.g.
`/api/v0.1/ci_types`) and `values` is the set of scalar request
parameters, excluding `_key`/`_secret` itself, sorted by parameter name
with the *values* concatenated.  The upstream's `auth_required` wrapper
rewrites `request.values` to the **parsed JSON body** when a body is
present (otherwise to the query string) and signs over that set, so:

* **read calls** (no body): `values` is the query string; the
  credentials ride as **query parameters**;
* **write calls** (`POST`/`PUT` with a JSON body): `values` is the body;
  the credentials ride **inside the JSON body** and the signature covers
  the body's scalar values.  (Sending them in the query for a body-carrying
  request 401s — this was the 0.2.0/0.2.1 write bug.)

The signature is implemented as the pure function
`signing.sign(path, secret, values)` in
`src/cmdb_mcp_adapter/adapters/veops/signing.py`; the same function is
imported by the test stubs so a signing regression fails immediately.

Verified live behaviors:

| Request                                  | Upstream response |
|------------------------------------------|-------------------|
| no `_key` parameter                      | `401` `{"message": "401 Unauthorized: unauthorized"}` |
| `_key` present, wrong signature          | `401` (same body) |
| valid signature                          | `200` with data   |

So a bad credential pair surfaces as `authentication`
(`AuthenticationError`), never as `200` with a flag.

### Endpoints

All paths are relative to `VEOPS_BASE_URL` plus `API_PREFIX`
(`"/api/v0.1"` — note: `/api/v1` does **not** exist on 2.6.2 and
404s).

| MCP operation      | VeOps call                                        | Notes |
|--------------------|---------------------------------------------------|-------|
| `get_models`       | `GET /ci_types`                                   | `{"ci_types": [...], "numfound": N}` — list wrapper |
| — (field lookup)   | `GET /ci_types/<name>` + `GET /attributes/s?page=&page_size=` | see "Model fields" below |
| `get_instances`    | `GET /ci/type/<type_id>?page=`                    | `{"cis": [...], numfound, page, total, type_id}` — paginated upstream: fixed 50/page, `page_size` ignored, `numfound`/`total` are per-page counts; the adapter pages through all of them (see "Traps observed") |
| `search_instances` | `GET /ci/search?q=&ci_type=&page=&page_size=`     | `{numfound, total, page, result: [...]}` |
| `get_relations`    | `GET /ci_relations/<instance_id>/second_cis`      | `{numfound, total, page, second_cis: [...]}` (outbound edges; inbound is the symmetric `/first_cis`) |
| `create_instance`  | `POST /ci`                                        | body `{"ci_type": <type_id>, ...fields, _key, _secret}` — creds in body; answers `{"ci_id": <int>}` (live-verified) |
| `update_instance`  | `PUT /ci/<instance_id>`                           | body is the flat field dict + `_key`/`_secret` in body; answers `{"ci_id": <int>}` (live-verified) |
| `delete_instance`  | `DELETE /ci/<instance_id>`                        | no body (creds in query); answers `{"message": "ok"}`; `405` maps to `unsupported_operation` (live-verified) |

**Traps observed in the deployment:**

* `GET /ci` with no id is a **hard 500** in the upstream
  (`get() missing 1 required positional argument: 'ci_id'` — a
  flask_restful route collision between `/ci` and `/ci/<int:ci_id>`).
  Instance listing must go through `/ci/type/<type_id>`; the adapter
  never issues a bare `GET /ci`.
* `GET /ci/type/<type_id>` is **paginated with a fixed page of 50**
  (verified 2026-08-29 on the intranet deployment): `page` is
  honoured (from 1), `page_size` is *ignored*, and `numfound`/`total`
  in the response are **per-page** counts — the response carries no
  total over the type, so a short page (< 50) is the only
  end-of-data signal.  Earlier probes only exercised types with ≤ 50
  CIs, which made the endpoint look unpaginated.  The adapter pages
  through `page=1,2,...` until a short or empty page arrives (a page
  that adds no new `_id` also terminates the loop) and returns the
  whole list in one result page: `total` is the full count and
  `next_cursor` is always `None` (a non-`None` cursor is a
  validation error).
* The `fl` search parameter controls **returned fields, not
  filtering**: passing `fl=<attr>=<value>` 400s
  (`Attribute attr=value does not exist`).  Attribute filtering is
  therefore done **client-side** by the adapter (exact equality over
  the returned pages); `None` filter values match instances lacking
  that key.  The upstream search has no server-side attribute filter.

### Model fields (best effort)

`GET /ci_types` returns one entry per CI type with `id`/`name`/`alias`/
`unique_key`/`enabled`/`icon`/`parent_ids` but **no attribute list**
(the single-type lookup `GET /ci_types/<name>` is list-wrapped too and
adds only `unique_name`).  The adapter therefore resolves each
model's `fields` from the global attribute dictionary
(`GET /attributes/s`, `{id, name, alias, value_type, is_choice,
choice, ...}`).

**`GET /attributes/s` is paginated.**  The upstream serves a default
page of 50 entries and honours `page` / `page_size` (verified
2026-08-28 on the intranet deployment: 210 attributes in total,
`page_size=500` returns them all in one page).  A single unpaginated
call therefore truncates the dictionary to its first page and silently
leaves most model types with `fields=()`.  The adapter pages through
the endpoint (`page=1,2,...`, requesting `page_size=500`) until a
short or empty page arrives — the response carries no total count, so
a short page is the end-of-data signal (a deployment that capped
`page_size` below the requested value would truncate the fetch at the
first page; no such cap was observed in the 2026-08 probe).  A total
that is an exact multiple of the page size costs one extra request
(the terminating empty page), and a page that adds no new attribute
also terminates the loop (a guard against an upstream that repeats a
page).

Matching uses the deployment's naming convention: an attribute
belongs to a type when its name equals the type name, starts with
`<type>_`, or starts with an 8-character prefix of the type name
(`credential` → `credenti_*`, matching VeOps's attribute table).
Some types **reuse another type's attribute naming**: on the
intranet deployment (2026-08-28), `monitoring_component` CIs
carry `middlewa_*` attributes (e.g. `middlewa_project`,
`middlewa_lifecycle_status`) because the type reuses the middleware
attribute set — the naming convention can never match those.  The
matcher therefore also consults a small, explicit type→prefix alias
table (`_ATTR_PREFIX_ALIASES` in `adapter.py`); add an entry there
when a deployment reuses another type's prefix for yet another type.

Types with no match get `fields=()`; both dictionaries are cached with
a 60 s TTL.  This is a documented degradation — instance *values*
still flow through `data` regardless of the `fields` metadata.

### Instance payloads

Instances are returned as **flat** dicts: `_id`, `_type`,
`_updated_at`, `_updated_by`, `ci_type`, `ci_type_alias`, `unique`,
plus one key per attribute value.  The adapter splits them:

* `_id` → `instance_id` (stringified);
* the remaining vendor keys (plus any other `_*` key) →
  `CMDBInstance.metadata` under the `veops.` prefix (e.g.
  `{"veops.ci_type": "credential", "veops.id": "515", ...}`);
* attribute values → `data`, restricted to the model's resolved
  `fields` when available (unmatched keys are quarantined into
  `metadata` so a normalized field is never displaced).

### Relations

`GET /ci_relations/<id>/second_cis` returns flat *target* instance
dicts without a `relation_type_id`, so `relation_type` is empty and the
raw target payload is preserved in each relation's `metadata`
(vendor-prefixed).  This is a documented limitation of the 2.6.2
API; the generic `GET /ci_relations/s` endpoint 404s without full
request context and is not used.

## Configuration

The veops adapter reads three *upstream* environment variables and two
*shared* runtime variables.  There are no configuration files or
secret managers; values come from the process environment and are
rotated by changing the environment and restarting the process.

| Variable               | Required | Purpose                                                    |
|------------------------|----------|------------------------------------------------------------|
| `VEOPS_BASE_URL`       | **yes**  | The CMDB endpoint.  Must be `http(s)`.  Trailing slash is normalized away. |
| `VEOPS_KEY`            | **yes**  | The VeOps user key (identity), sent as the `_key` query parameter. |
| `VEOPS_SECRET`         | **yes**  | The VeOps user secret (signing material), used to compute `_secret`; never logged. |
| `MCP_HTTP_TIMEOUT`     | no (default 30) | Per-request timeout (seconds) applied to every veops call (the readiness probe uses its own fixed 5-second timeout). |
| `MCP_HTTP_MAX_RETRIES` | no (default 2)  | Retry budget for *read* operations only (see below).       |
| `VEOPS_READONLY`       | no (off)   | Read-only switch. When enabled the three mutation tools are disabled (see [Read-only mode](#read-only-mode)). |

**0.2.0 migration.**  The pre-0.2.0 single-token variable
`VEOPS_TOKEN` is no longer used: one token cannot express a key/secret
pair.  It is still *read* purely for diagnostics — if `VEOPS_TOKEN` is
set while `VEOPS_KEY`/`VEOPS_SECRET` are missing, startup fails with a
`configuration` error that names both new variables, so an old
deployment is a one-line environment fix rather than a silent
mis-authentication.  `VEOPS_TOKEN` never authenticates.

**Fail-closed policy (stricter than the mock adapter).**  The veops
adapter requires **all three** of `VEOPS_BASE_URL`, `VEOPS_KEY`, and
`VEOPS_SECRET` at startup.  If any is missing, the process refuses to
start with a `configuration` error before any protocol activity.
This is deliberate: the real CMDB requires authentication, so running
without credentials would only fail on the first request; failing at
startup is the safer behavior.  (The generic upstream loader in
`config.py` treats these as optional so the *independent* inbound /
upstream domains can be represented without a specific adapter; the
veops-specific tightening lives in `adapters/veops/config.py`.)

**Where do the credentials come from?**  The VeOps *user management*
page, or the `key`/`secret` columns of the VeOps `users` table
(plain user attributes, not a password).  Production deployments
should use a dedicated service account.  The values are treated as
secrets: the key and secret are registered with the shared redaction
filter, so neither — nor the derived `_secret` signature — can appear
in log lines, readiness bodies, or error messages.

`MCP_HTTP_TIMEOUT` and `MCP_HTTP_MAX_RETRIES` are consumed by the veops
client: the timeout bounds every HTTP call, and the retry budget applies
only to idempotent reads.

## Read-only mode

Since 0.2.3 the veops adapter supports a **read-only mode** via the
`VEOPS_READONLY` environment variable.

**Why it exists.**  The adapter authenticates to VeOps with a
*per-user* `key`/`secret` pair (`VEOPS_KEY` / `VEOPS_SECRET`).  In many
deployments that credential is an **admin-level** account, which means
the adapter's write tools (`create_instance` / `update_instance` /
`delete_instance`) could mutate real CMDB data.  For a production
endpoint you usually want the adapter to *read* the CMDB for MCP
clients but never write to it.  `VEOPS_READONLY=1` makes that the
enforced contract rather than a convention.

**Behaviour when `VEOPS_READONLY` is set to `1`/`true`/`yes`/`on`
(case-insensitive):**

* The three write operations raise an `unsupported_operation` error
  **before any upstream HTTP request is made** — no partial write is
  ever possible, and no request touches the CMDB at all.
* The MCP server omits the three mutation tools from `list_tools` and
  does not register their dispatch handlers, so a well-behaved MCP
  client never even sees them.  (A client that nevertheless calls
  `create_instance` by name receives an `unknown tool` error.)
* The four read tools — `get_models`, `get_instances`,
  `search_instances`, `get_relations` — are unaffected and keep their
  full behaviour.

**When it does *not* apply.**  `VEOPS_READONLY` is a *veops-domain*
switch: it only takes effect when `MCP_ADAPTER=veops`.  The `mock`
adapter (used for contract tests and local development) always
exposes the full tool set regardless of the env value, so the
write-path contract tests keep exercising the complete surface.

**Default is write-enabled.**  The flag defaults to *off*, so local and
sandbox deployments that intentionally test the write contract keep
working unchanged.  To deploy read-only you must set the variable
explicitly (see the [deployment doc](docker-deployment.md)).

Any unrecognized value (e.g. a typo like `yep`) degrades to the
write-enabled default rather than failing startup — the switch is
fail-open-toward-writes by design, because the safer *deployment*
choice is to simply not set the flag at all when you want writes.

## Error mapping

The veops client maps upstream HTTP outcomes onto stable core
categories.  The MCP tool layer emits only the category + a clean
message; the raw upstream body is retained in `upstream_detail`
(protected logs only, never sent to MCP clients) and redacted before
logging.

| veops outcome                     | Core category          | Tool error text (prefix)   |
|-----------------------------------|------------------------|----------------------------|
| timeout / connect / network error | `timeout`              | `timeout: …`               |
| `401` (missing **or bad** signature) | `authentication`  | `authentication: …`        |
| `403 Forbidden`                   | `authorization`        | `authorization: …`         |
| `404 Not Found`                   | `not_found`            | `not_found: …`             |
| `429 Too Many Requests`           | `rate_limit`           | `rate_limit: …`            |
| `405` on DELETE                   | `unsupported_operation`| `unsupported_operation: …` |
| `400` / other `4xx`               | `validation`           | `validation: …`            |
| `5xx` (incl. the bare `GET /ci` 500 trap) | `upstream_server` | `upstream_server: …`     |
| non-JSON success body             | `validation`           | `validation: …`            |
| a malformed client-supplied cursor| `validation`           | `validation: …`            |

## Retry behavior

Only **idempotent read** operations (`get_models`/`list_models`,
`get_instances`, `search_instances`, `get_relations`) are retried,
using the shared `observability.retry.retry` helper with
`is_idempotent=True` and `max_retries = MCP_HTTP_MAX_RETRIES`.  Only
transient categories (`timeout`, `rate_limit`, `upstream_server`) are
retried, with bounded exponential backoff + jitter; on exhaustion the
*original* error (and its category) is re-raised.

**Write** operations (`create_instance`, `update_instance`,
`delete_instance`) are **never** retried — retrying a non-idempotent
call risks duplicate side effects, so the first failure is returned
as-is.

## Readiness probe

The HTTP transport wires a *real* (cheap, read-only) reachability
probe into `/readyz` when `MCP_ADAPTER=veops`.  The probe issues a
single **unauthenticated** `GET /ci_types` (deliberately without
`_key`/`_secret`: a 401 "unauthenticated" answer is itself proof the
upstream is up) with a fixed 5-second timeout and classifies:

* `2xx`/`3xx`, `401`, `403` → **reachable** (the server answered; a
  401/403 merely means it needs auth).
* `5xx`, connect / timeout / network error → **unavailable**, with a
  redacted `reason`.

The probe is read-only and bounded, so it is safe to run on every
readiness check.  When the adapter is `mock`, the shared no-op prober
is used instead (no extra I/O).

## Deployment limitations (2.6.2)

* **`get_instances` is not paginated upstream.**  `GET /ci/type/
  <type_id>` returns the whole type in one response (up to a few
  thousand rows on real deployments); the adapter returns it as a
  single page (`next_cursor=None`) and rejects a non-empty cursor
  argument with a `validation` error.  Use `search_instances` for
  large datasets — it is page-paginated (`page`/`page_size`, with
  `next_cursor` carrying the next 1-based page).
* **No server-side attribute filtering.**  The search `fl` parameter
  selects returned fields only; `filters` are applied client-side
  (exact equality; `None` matches missing keys).
* **Single-replica API.**  The probe and every call hit the one
  `cmdb-api` process behind nginx; there is no connection pooling or
  failover in the adapter.
* **Write responses are verified but thin.**  `POST /ci` and
  `PUT /ci/<id>` answer a `{"ci_id": <int>}` stub (the new/updated
  native id only — no field echo), and `DELETE /ci/<id>` answers
  `{"message": "ok"}`; all three were exercised live against the local
  2.6.2 sandbox (2026-08) with a full create → update → delete →
  search-verify round-trip.  The adapter's write mapping therefore
  resolves the new/updated `instance_id` from `ci_id` (or `_id`/`id`/
  a `{"ci": {...}}` wrapper) and, on a stub response, reports the sent
  fields as `data` while the native id is authoritative.  Business-rule
  rejections (e.g. a required choice attribute such as
  `data_che_check_type` on `data_check`) surface as `validation`
  (`400`) with the upstream message in `upstream_detail`.  A `405`
  from `DELETE` maps to `unsupported_operation`.

## Testing without a real CMDB

Two independent no-network paths exist:

1. **The mock adapter** (`MCP_ADAPTER=mock`) implements the full core
   contract in-process with a deterministic dataset — use it to
   exercise the MCP tool layer and the core service end-to-end without
   any CMDB.
2. **This adapter's signature-verifying stub.**
   `tests/adapters/` drive `VeopsClient` / `VeopsAdapter` through an
   `httpx.MockTransport` that emulates the real `/api/v0.1` routes
   *and verifies the `_key`/`_secret` pair with the same production
   `signing.sign()` function* (imported, never copied): a request
   arriving with a bad or missing signature gets the live deployment's
   exact `401` body.  This covers the full status→category table,
   pagination across pages, the read-vs-write retry policy, the
   unsupported-delete mapping, and the prober semantics — all with
   zero network access and no real credentials.

Run them with:
```bash
uv run --no-sync pytest tests/adapters -q
```

### Opt-in live check

`tests/integration/test_veops_live.py` is an **opt-in, read-only**
check against a real deployment.  It is disabled by default
(`@pytest.mark.optin` + `skipif MCP_VEOPS_LIVE != "1"`) and issues
`GET` requests only (`get_models`, `get_instances`, `search_instances`):

```bash
MCP_VEOPS_LIVE=1 \
VEOPS_BASE_URL=http://<host>:8000 \
VEOPS_KEY=<your-veops-key> \
VEOPS_SECRET=<your-veops-secret> \
uv run --no-sync pytest -q tests/integration/test_veops_live.py
```
