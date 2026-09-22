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
| — (field lookup)   | `GET /ci_types/<type_id>/attributes`                | per-type **binding list**: the only source of `fields`, `required`, `enum_values` (see "Model fields" below) |
| `get_instances`    | `GET /ci/type/<type_id>?page=`                    | `{"cis": [...], numfound, page, total, type_id}` — paginated upstream: fixed 50/page, `page_size` ignored, `numfound`/`total` are per-page counts; the adapter pages through all of them (see "Traps observed") |
| `search_instances` | `GET /ci/search?q=&ci_type=&page=&page_size=`     | `{numfound, total, page, result: [...]}` |
| `get_relations`    | `GET /ci_relations/<instance_id>/second_cis?count=all[&relation_type=<name>]` | `{numfound, total, page, second_cis: [...]}` — **outbound** edges only; `count=all` is mandatory to see them all and `relation_type` is the only way to learn an edge's type (see "Relations" below) |
| — (edge typing)    | `GET /relation_types`                               | **bare JSON array**, no list wrapper: `[{id, name, deleted, ...}, ...]`; live types only — a soft-deleted name is *absent here and rejected* by the filter above with `400` |
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
* **The relation endpoints are paginated too — and their `numfound` means
  something else.**  Verified on the intranet deployment
  (2026-09-22): a bare `GET /ci_relations/<id>/second_cis` for a CI with
  100 edges answered `len(second_cis)=50`, `total=50`, `numfound=100`.  On
  the ci *list* endpoint `numfound` is the **page** count; on the relation
  endpoints it is the **whole edge set**.  A bare call is therefore silently
  truncated at the upstream's fixed 50-row page with no "more data" signal.
  `count=all` (→ `get_page_size()`, `api/lib/utils.py:42`) suppresses
  `offset`/`limit` (`api/lib/cmdb/ci.py:1141-1155`) and makes
  `len(rows) == numfound` hold, so the adapter sends it and *checks* it.
  Looping `page=2,3,...` is **not** an alternative: that query carries no
  `ORDER BY` (and no `distinct`), so pages can repeat or drop rows; `page`
  was also verified to be ignored once `count=all` is present.
* `second_cis?relation_type=<name>` is the only way to obtain an edge's
  type, and the name must come from `GET /relation_types`: an unknown or
  soft-deleted name answers `400 Invalid relation type` (verified with
  `hosts`, which exists in the table with `deleted=1`).  An **empty** value
  is treated as "no filter", so a blank name must never be sent — it would
  relabel the entire unfiltered set.
* `GET /relation_types` answers a top-level array, not the usual
  `{...: [...]}` wrapper, so it needs its own client call
  (`VeopsClient.request_array`); every other endpoint keeps the
  object-shape requirement.
* **Inbound edges cannot be typed.**  `GET /ci_relations/<id>/first_cis`
  honours `page`/`count` but *ignores* `relation_type` (verified 2026-09-22:
  the same rows come back for every type name), so the per-type technique
  above only works outbound.  `/first_cis` backs no MCP tool today.

### Model fields (from the type's binding list)

`GET /ci_types` returns one entry per CI type with `id`/`name`/`alias`/
`unique_key`/`enabled`/`icon`/`parent_ids` but **no attribute list**
(the single-type lookup `GET /ci_types/<name>` is list-wrapped too and
adds only `unique_name`).  The authoritative type→attribute mapping
lives in the binding table (`c_ci_type_attributes`), and the upstream
exposes it per type:

`GET /ci_types/<type_id>/attributes` →
`{"attributes": [...], "type_id": n, "unique_id": n, "unique": "<name>"}`

* One request per model; upstream also accepts the type *name* in the
  `<type_id>` slot, the adapter always sends the numeric id.
* **Not paginated** — the whole list arrives at once, already sorted by
  `(order, id)`, and it includes attributes inherited from parent types
  (`inherited` / `inherited_from`).
* Each entry is the global attribute record (`name` / `alias` /
  `value_type` / `is_choice` / `is_unique` / ...) enriched with the
  *per-binding* flags `is_required` / `order` / `default_show`.  This is
  therefore the only place the `required` marker can come from: the
  attribute table itself has no such column, because required-ness in
  VeOps belongs to the type×attribute row, not to the attribute.
* For choice attributes the upstream resolves the enumeration
  server-side and answers `choice_value` as `[value, option]` pairs
  (verified 2026-09-21: `[["生产", {"label": "生产"}], ["UAT", {...}]]`).
  The adapter projects them onto the *value* — the option label is
  display text, not a storable value — and maps a missing or empty list
  to `enum_values=None` ("no constraint known"), never an empty
  enumeration: an unreachable webhook also answers `[]`.

**Do not rebuild this mapping from attribute names.**  Up to 0.2.6 the
adapter read the *global* dictionary `GET /attributes/s` and assigned
attributes to types by the deployment's naming convention (`<type>_`, an
8-character prefix of the type name, plus a hand-maintained alias
table).  Measured on the intranet deployment (2026-09-21; 12 types,
265 binding rows) that inference mis-resolved 3 models:

| model | real binding rows | naming heuristic returned |
|-------|-------------------|---------------------------|
| `business_system` | 2 | 29 — the whole `business_*` set of `business_application` |
| `runtime_platform` | 31 | 0 — it binds `middlewa_*` rows, no prefix match |
| `monitoring_component` | 27 | 30 — the whole `middlewa_*` set of `middleware` |

The names genuinely do overlap across types (`monitoring_component`
reuses 6 `middlewa_*` attributes, `runtime_platform` reuses 6 more), so
no prefix rule or static alias table can express which subset belongs to
which type.  The per-type binding endpoint has existed the whole time;
reading the dictionary instead of it was simply the wrong tool.

`GET /attributes/s` is paginated (default page 50, `page` / `page_size`
honoured, no total in the payload) and its entries carry **no binding
information at all**.  The adapter stopped calling it in 0.2.7.

Cost and failure handling: the 12 calls run sequentially and cost
~0.7–1.0 s warm on the probe deployment (37–161 ms each).  A cold
upstream can stall one of them — a single `> 30 s` read timeout was
observed once (8th type of the first probe of 2026-09-21, normal on
retry) — so the lookups go through the client's retry policy and
configured timeout.  On failure `list_models` **propagates** the error:
the field list *is* its product, so a guessed or empty catalogue is
worse than an explicit `upstream_*` error.  The instance-read paths
(`get_instances` / `search_instances` / `get_relations`) instead degrade
to `fields=()`, which is harmless: `map_ci` only *restricts* `data` to
the declared fields when the list is non-empty, so instance values never
disappear together with the metadata.  Each model's list is cached under
the same 60 s TTL as the ci_types lookup.

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

`GET /ci_relations/<id>/second_cis` returns flat *target* instance dicts
and — unlike the generic `GET /ci_relations/s` endpoint, which 404s without
full request context and is not used — **no `relation_type_id`**.  Since
0.2.8 `get_relations` returns the whole edge set *with* its types anyway, in
two steps:

1. **Completeness** — one `count=all` call, then refuse the answer unless
   `len(second_cis) == numfound`.  A mismatch, or rows with no `numfound` to
   check them against, raises `upstream_server` naming both counts (e.g.
   `returned 50 of 120 outbound edges`); a truncated edge list is never
   passed on as if it were complete.
2. **Typing** — one call per *live* relation type name (dictionary cached
   60 s), each typed row **consuming** one row of the unfiltered set keyed by
   target id.  Iteration stops as soon as every edge is attributed: a
   single-typed hub costs 2 requests (unfiltered + that type), a leaf CI
   costs exactly 1 (no rows ⇒ the dictionary is not even read), worst case
   `1 + len(live types)`.

Measured on the intranet deployment (2026-09-22, 44 sampled CIs, 3,223
edges, every out-degree band and every live relation type represented):
**243 upstream calls, 13.0 MB, 4.6 s, zero errors** — 1 dictionary call + 44
`count=all` calls + 198 typed probes.  The two extremes are instructive:

* `data_check` **#2987** (292 edges, all `待核验`): 8 calls, 1,254,542 bytes.
  The unfiltered set is 627,070 bytes and the `待核验` probe returns that same
  payload again, because the typed query has no way to project fewer fields.
  Six intermediate probes returned 67 bytes (the empty envelope).
* A **1-edge CI whose type sorts last** in the dictionary (#1154, #2336,
  #2874, #2925, #2929, #2963): 8 calls for 5,244 bytes ≈ 90 ms.  Request
  count, not bytes, is what hurts at this end — and types that carry no edges
  at all (`调用`, `监测`) still get probed while any edge is unattributed.

Ordering the probes by observed hit rate would collapse both cases toward
`1 + types actually present`; it changes no guarantee (attribution is keyed by
target id and the loop only stops when nothing is pending), so it is a
candidate for 0.2.9.

Consistency is enforced in both directions: a typed row whose target is not
in the unfiltered set raises `upstream_server` (the adapter refuses to guess
which edge to label), and an unclaimed edge is returned with
`relation_type=""` **plus**
`metadata["adapter.relation_type_unresolved"] = true`.  That flag is
namespaced `adapter.` rather than `veops.` on purpose: it is the adapter's
own statement, not a copied vendor value.  The usual cause is an edge still
pointing at a soft-deleted relation type — a real data problem inside the
CMDB, which is exactly why it is surfaced instead of hidden.  If the
dictionary endpoint is unusable (`403`/`404`/wrong shape) the adapter logs
one warning, returns the (still proven complete) edges with every type
unresolved, and does **not** cache the failure; transient failures follow
the normal retry policy.

Edges keep the upstream row order, and the raw target payload stays in each
relation's `metadata` under the `veops.` prefix.  Sum rule verified on the
probe deployment: the per-type row counts add up to `numfound` exactly
(100 = 100 `承载` + 0 for each of the other 7 live types).

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

* **`get_instances` answers in a single MCP page.**  `GET /ci/type/
  <type_id>` *is* paginated upstream (fixed 50 rows, `page_size` ignored —
  see "Traps observed"); the adapter walks those pages itself and returns
  the whole type with `next_cursor=None`, and rejects a non-empty cursor
  argument with a `validation` error.  `search_instances` is the tool that
  pages for the caller (`page`/`page_size`, `next_cursor` carrying the next
  1-based page).  *(This bullet used to claim the endpoint was "not
  paginated upstream" — measured wrong on 2026-08-29 and fixed in 0.2.5, the
  wording had never been brought in line with the trap above.)*
* **`get_relations` is outbound-only and costs one request per relation
  type.**  An instance with edges over `t` live relation types costs `1 + t`
  upstream calls (bounded by `1 + 8` on the probe deployment; the dictionary
  is cached for 60 s and zero-edge CIs cost a single call), and inbound
  edges (`/first_cis`) cannot be typed at all on 2.6.2, so no tool exposes
  them.
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
