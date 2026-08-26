# Changelog

All notable changes to `cmdb-mcp-adapter` are documented in this file,
in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
The project aims for [Semantic Versioning](https://semver.org/)
(major.minor.patch).

## [0.2.2] - 2026-08-26

### Changed

* **Write-response id mapping** — the live 2.6.2 write responses were
  verified against the local sandbox: `POST /ci` and `PUT /ci/<id>`
  answer a `{"ci_id": <int>}` stub (native id only, no field echo) and
  `DELETE /ci/<id>` answers `{"message": "ok"}`.  `map_ci` and
  `_instance_from_write` now resolve the new/updated `instance_id`
  from `ci_id` (and unwrap the `{"ci": {...}}` read wrapper), so
  `create_instance` / `update_instance` return the authoritative
  native id instead of an empty one.  A full
  create → update → delete → search-verify round-trip passed live
  against the isolated sandbox (2026-08-26).

## [0.2.1] - 2026-08-26

### Fixed

* **VeOps write calls 401'd** — the 2.6.2 `auth_required` wrapper
  rewrites `request.values` to the parsed JSON body when a body is
  present, so it looks for `_key`/`_secret` *inside the body* and signs
  over the body's scalar values.  0.2.0 sent the credentials in the
  query for every call, which 401s on `POST`/`PUT` (a body-carrying
  request).  The client now branches on the presence of a JSON body:
  **read** calls sign the query and put the credentials in the query;
  **write** calls put `_key`/`_secret` in the JSON body and sign over
  the body's scalar values.  Verified live: body-auth `POST`/`PUT`
  pass authentication and reach business logic; query-auth `POST`
  401s.  `signing` / `client` docstrings corrected to match.

## [0.2.0] - 2026-08-25

### Added

* **Opt-in live test** — `tests/integration/test_veops_live.py`
  (`MCP_VEOPS_LIVE=1`, `GET`-only) verifies the adapter end-to-end
  against a real OneOps/VeOps 2.6.2 deployment: `get_models`
  (asserting the deployment's `quick_reference` and `credential` CI
  types), `get_instances("quick_reference")` (Chinese content fields),
  and `search_instances("credential", "ACC-0166")`.
* **Contract-suite coverage for the veops adapter** — the MCP tool
  layer's `tools/list` output (names **and** JSON input schemas) is now
  asserted identical to the mock adapter's, so the frozen tool contract
  is protected across adapters.

### Changed

* **VeOps adapter targets the real OneOps/VeOps 2.6.2 contract** —
  verified against a live deployment (2026-08):
  * API prefix `/api/v1` → `/api/v0.1`; reachability probe is now an
    unauthenticated `GET /ci_types` (`{200, 401, 403}` = reachable).
  * `get_models` ← `GET /ci_types` (list-wrapped response); model
    `fields` are resolved best-effort from the global
    `GET /attributes/s` dictionary (60 s TTL).
  * `get_instances` ← `GET /ci/type/<type_id>` (the upstream returns
    the **whole** type in one response and is not paginated): one full
    page, `next_cursor=None`, non-empty cursor input rejected.
  * `search_instances` ← `GET /ci/search` (page-paginated; `fl` selects
    *returned fields*, so the core `filters` argument is applied
    client-side — exact equality, `None` matches missing keys).
  * `get_relations` ← `GET /ci_relations/<id>/second_cis` (outbound
    edges; the 2.6.2 response carries no `relation_type_id`, so
    `relation_type` is empty and the raw target payload is preserved in
    `metadata` — documented limitation).
  * Instance payloads are flat dicts: `_id` → `instance_id`,
    `_type`/`_updated_at`/`_updated_by`/`ci_type`/`ci_type_alias`/
    `unique` → `metadata` under the `veops.` prefix, attribute values
    → `data`.
  * `create`/`update`/`delete` ← `POST /ci`, `PUT /ci/<id>`,
    `DELETE /ci/<id>` (write responses are not contractually fixed in
    2.6.2; the mapping tolerates an echoed instance, a
    `cis`/`result` list, an `id` stub, or an empty body; `405` on
    delete maps to `unsupported_operation`).

### Breaking

* **`VEOPS_TOKEN` is removed as a credential** (0.2.0 migration).
  The VeOps CMDB authenticates with a per-user **key/secret pair**:
  every request carries `_key` and a `_secret` signature
  (`sha1(path + secret + <sorted scalar param values>)`, see
  [VeOps Adapter — Authentication](docs/veops-adapter.md#authentication-keysecret-signing)).
  Set `VEOPS_KEY` and `VEOPS_SECRET` (from the VeOps user-management
  page or the `users.key`/`users.secret` columns) instead.
  `VEOPS_TOKEN` is still read for diagnostics only: if it is set while
  the new pair is missing, startup fails with a `configuration` error
  naming the two new variables.  `VEOPS_BASE_URL` should now point at
  the VeOps nginx front (e.g. `http://<host>:8000`); the API is
  mounted at `/api/v0.1` under it.

### Notes

* The seven MCP tool names, parameter/result shapes, error categories,
  and cursor-paging semantics are **unchanged** (`tools/list` output is
  field-for-field identical to 0.1.0; verified by the contract suite).
* The veops integration test stubs now emulate the real contract and
  verify the `_key`/`_secret` pair with the same production
  `signing.sign()` function (imported, never copied).

## [0.1.0] - 2026-08-25

### Added

MVP release:

* **Seven frozen MCP tools** (`get_models`, `get_instances`,
  `create_instance`, `update_instance`, `delete_instance`,
  `search_instances`, `get_relations`) with a vendor-neutral
  normalized contract and stable error categories
  ([MCP Tool Contract](docs/mcp-tool-contract.md)).
* **`veops` adapter** — first vendor adapter, mapping the core
  contract onto the (assumed) VeOps CMDB REST API, with bounded
  timeout, idempotent-read-only retries, error-category mapping, and
  a real read-only `/readyz` probe
  ([VeOps Adapter](docs/veops-adapter.md)).
* **`mock` adapter** — deterministic in-process dataset for
  credential-free development, demos, and contract testing.
* **Streamable HTTP transport** (primary) — single-process
  `cmdb-mcp http` with `/mcp`, `/healthz`, `/readyz`; fail-closed
  Bearer-token auth for non-loopback binds; in-memory sessions
  ([Remote HTTP](docs/remote-http.md)).
* **Local stdio transport** (secondary) — host-launched
  `cmdb-mcp stdio` with protocol-only stdout and the documented
  exit-code/signal contract
  ([Local stdio](docs/local-stdio.md)).
* **Configuration** — env-only (typed settings, CLI > env > default
  precedence, fail-closed validation); no config files, no secret
  manager ([Configuration, Security, and
  Observability](docs/configuration-security-observability.md)).
* **Observability** — one-JSON-line-per-line structured logs on
  stderr, correlation IDs, secret redaction, liveness/readiness
  probes.
* **Release scaffolding** — this changelog,
  [Python Support Policy](docs/python-support-policy.md)
  (3.10/3.11/3.12), local-wheel/private-index installation
  (no public PyPI publish), and CI validation of the documentation
  (`tests/integration/test_docs_examples.py` plus the CI docs job).

### Notes

* Docker images, systemd units, TLS/reverse proxy, OAuth,
  TypeScript client, Kubernetes, and Windows service installers are
  **out of scope** for 0.1.0 (see
  [MVP boundaries](README.md#mvp-boundaries)).

## Release process / rollback

**Versioning.** The release version is declared in exactly two
places and they must match: the `[project] version` in
`pyproject.toml` and `__version__` in
`src/cmdb_mcp_adapter/__init__.py` (the latter is what
`cmdb-mcp --version` prints).  CI (the `Docs` job in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)) asserts the two agree.  Bumps
follow SemVer: tool-name/contract changes are major; new adapters or
tools are minor; fixes are patch.  Every release updates this
changelog first.

**Artifacts.** A release produces a **local wheel and source
archive** (`uv build`, hatchling backend — no network required) plus
the tagged source tree.  The wheel is mirrored to the
organization's **private index** for `pip`/`pipx` installs; there is
**no public PyPI publish**.  No container image is produced in the
MVP.

**Rollback.** Every release records the **previous known-good
version** (see the entries above).  To roll back: stop the running
process (`SIGINT`/`SIGTERM`; it flushes stderr and exits), then start
the previous version's wheel or private-index revision with the same
environment — no data migration is needed (the adapter holds no
persistent state; MCP clients simply re-initialize).  Roll back
dependency pins the same way (restore the previous `pyproject.toml`
pin and `uv.lock`, then `uv sync --locked`); see
[Python Support and Dependency
Policy](docs/python-support-policy.md#rollback-policy).

**Compatibility and deprecation.** MCP tool names, normalized core
fields, CLI command names, documented endpoint paths, and required
environment variable names are compatibility surfaces.  Changes to
them require a deprecation note in the changelog plus migration
instructions, and migration guidance is kept in the changelog even
after a command or configuration key is removed.
