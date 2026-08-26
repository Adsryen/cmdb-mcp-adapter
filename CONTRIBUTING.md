# Contributing to cmdb-mcp-adapter

This guide covers how to work in this repository: the project layout, the
local development loop, and — the main focus — how to add a new CMDB
adapter without touching the frozen MCP tool contract.

Read the [README](README.md) first for what the project is and how to
run it; this file is about *building* it.

---

## 1. Project layout

```text
src/cmdb_mcp_adapter/
├── core/                     # Vendor-neutral core (NEVER vendor-specific)
│   ├── models.py             #   CMDBModel, ModelField, CMDBInstance, CMDBRelation, Page
│   ├── errors.py             #   CoreError + stable categories (ValidationError,
│   │                         #   ConfigurationError, AuthenticationError,
│   │                         #   AuthorizationError, NotFoundError, TimeoutError,
│   │                         #   RateLimitError, UpstreamServerError,
│   │                         #   UnsupportedOperationError, InternalError)
│   ├── adapter.py            #   CMDBAdapter Protocol — the 7-method boundary
│   └── service.py            #   CoreService — input validation, page-size clamping,
│                             #   structured logging boundary
├── server/                   # Transports + composition root
│   ├── app.py                #   create_app() — the single composition point;
│   │                         #   register_adapter(name, factory) registry
│   ├── mcp_server.py         #   FastMCP / low-level dispatcher: the 7 MCP tools
│   ├── http.py               #   Streamable HTTP launcher (uvicorn)
│   ├── asgi.py               #   ASGI app: /mcp, /healthz, /readyz, AuthMiddleware
│   ├── stdio.py              #   stdio launcher (host-launched child process)
│   ├── auth.py               #   Inbound bearer-token authorization
│   ├── health.py             #   Liveness/readiness + UpstreamProber contract
│   └── lifecycle.py          #   Signal handling, shutdown, stderr flush
├── adapters/
│   ├── mock.py               #   MockAdapter — in-process, credential-free
│   └── veops/                #   The reference vendor adapter
│       ├── config.py         #   VeopsConfig, build_veops_config (fail-closed)
│       ├── client.py         #   VeopsClient — the only HTTP code; injectable
│       │                     #   transport (httpx)
│       ├── mapper.py         #   Pure vendor-payload → normalized-model mapping
│       ├── adapter.py        #   VeopsAdapter — implements CMDBAdapter; retry,
│       │                     #   page↔cursor bridging
│       └── prober.py         #   Readiness probe (auth failure = reachable)
├── config.py                 #   load_settings(), UpstreamSettings, ALL_ENV_VARS,
│                             #   validate_settings (fail-closed)
├── cli.py                    #   cmdb-mcp entry point (http / stdio subcommands)
└── observability/            #   correlation IDs, structured logging, redaction,
                              #   retry() bounded-retry helper (stdlib only)

tests/
├── unit/                     #   config, app, auth, cli, health, observability
├── contract/                 #   CoreService + MCP tool contract (adapter-agnostic)
├── integration/              #   CLI drift, transports, fail-closed, docs checks,
│                             #   secret scan
└── adapters/                 #   Per-adapter tests (veops/*); opt-in live smoke

scripts/mcp_dev_server.py     #   mcp dev / Inspector entry point (module `server`)
docs/                         #   User-facing documentation (see README table)
```

**Boundary rules** (enforced by review, and by structure):

* `core/` imports nothing from `adapters/` — the dependency points one way
  only: `adapters → core`.
* `server/` never calls a vendor endpoint directly; it only talks to
  `CoreService`.
* Vendor knowledge lives exclusively under `adapters/<name>/`.

---

## 2. Local development setup

Prerequisites: Python 3.10–3.12 (see
[Python Support and Dependency Policy](docs/python-support-policy.md)) and
[`uv`](https://docs.astral.sh/uv/).

```bash
# From the repository root
uv sync                          # uses the committed uv.lock
# if your network blocks pypi.org (the lock records the Alibaba mirror):
uv sync --index-url https://mirrors.aliyun.com/pypi/simple/
```

After the environment is synced, always run tools through
`uv run --no-sync` so you never trigger a resolution or accidentally
upgrade against a different index:

```bash
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest -q
```

**What "green" means:**

| Check | Green output |
|-------|--------------|
| `ruff check .` | `All checks passed!` (0 findings; `.trellis`, `.claude`, `.codex`, `.agents` are excluded) |
| `mypy src` | `Success: no issues found in N source files` (strict mode, `python_version = 3.10`) |
| `pytest -q` | `294 passed, 1 skipped` as of the current tree (the 1 skip is the opt-in live-CMDB smoke test; see §3.4). Adding tests is fine; a *new failure* is not. |

Run a single test:

```bash
uv run --no-sync pytest -q tests/adapters/test_mapper.py::TestModelMapping::test_model_with_fields
uv run --no-sync pytest -q -k "name_fragment"      # filter by name
```

Useful no-network CLI checks while iterating:

```bash
uv run --no-sync cmdb-mcp --version
uv run --no-sync cmdb-mcp http --help
MCP_ADAPTER=mock uv run --no-sync cmdb-mcp stdio --help
```

---

## 3. Adding a new CMDB adapter

The composition root is designed so that **one new adapter package is all
the production change required** — no core code, no tool-contract change, no
`server/` change (except the one-line registration). The `veops` adapter is
the reference implementation; mirror its structure.

### 3.1 Create the package `src/cmdb_mcp_adapter/adapters/<name>/`

Model the layout on `adapters/veops/`:

* **`config.py`** — typed upstream settings for this vendor.
  * Read the vendor's credentials from the *upstream* environment domain
    (the generic loader is `load_upstream_settings()` in
    `cmdb_mcp_adapter/config.py`; today that means `VEOPS_*`, so a new
    vendor either gets its own `ENV_*` constants there or reads
    `os.environ` directly — see note in §3.3).
  * **Fail closed**: if a credential the adapter is known to require is
    missing, raise `ConfigurationError` at factory time — before any
    protocol activity — exactly like
    `build_veops_config` / `validate_veops_upstream`. A missing mandatory
    token must never surface as a 401 on the first real request.
  * Normalize the base URL once (e.g. strip the trailing slash) so the
    client can concatenate paths.
* **`client.py`** — the *only* module that knows how to talk to the vendor
  API. Mirror `VeopsClient`: wrap a synchronous `httpx.Client` with a
  **constructor-injectable `transport`** (so tests can pass
  `httpx.MockTransport`), apply the per-request timeout from the inbound
  `Settings`, send the vendor auth header (never logged), and map upstream
  outcomes onto the stable `CoreError` categories in `core/errors.py`:

  | Upstream outcome | Core error |
  |------------------|-----------|
  | timeout / connection error | `TimeoutError` (core) |
  | 401 | `AuthenticationError` |
  | 403 | `AuthorizationError` |
  | 404 | `NotFoundError` |
  | 429 | `RateLimitError` |
  | 5xx | `UpstreamServerError` |
  | other 4xx | `ValidationError` |

* **`mapper.py`** — *pure* functions (dict in → core dataclass out, no I/O)
  that translate vendor payloads into the normalized models from
  `core/models.py` (`CMDBModel`, `CMDBInstance`, `CMDBRelation`).
  **Vendor-only values never become normalized fields**: any value that
  does not map onto a declared `ModelField` is collected into
  `CMDBInstance.metadata` under an explicit vendor prefix — the veops
  mapper uses the constant `VENDOR_METADATA_PREFIX = "veops:"`; use
  `"<name>:"` for your adapter. This is the core contract rule
  "vendor fields go in `metadata`" (see
  [MCP Tool Contract: Stability rules](docs/mcp-tool-contract.md#stability-rules)).
* **`adapter.py`** — implement the `CMDBAdapter` protocol
  (`core/adapter.py`) with **all seven methods** and exactly these
  signatures (the MCP tool layer and contract tests depend on them):

  ```python
  def list_models(self) -> list[CMDBModel]: ...
  def get_instances(self, model_name: str, *, page_size: int,
                    cursor: str | None, filters: Filters) -> Page[CMDBInstance]: ...
  def create_instance(self, model_name: str, data: dict[str, Any]) -> CMDBInstance: ...
  def update_instance(self, model_name: str, instance_id: str,
                      data: dict[str, Any]) -> CMDBInstance: ...
  def delete_instance(self, model_name: str, instance_id: str) -> None: ...
  def search_instances(self, model_name: str, query: str, *, page_size: int,
                       cursor: str | None) -> Page[CMDBInstance]: ...
  def get_relations(self, model_name: str, instance_id: str) -> list[CMDBRelation]: ...
  ```

  Rules that apply inside the adapter:

  * **Retry policy** (use the shared helper
    `cmdb_mcp_adapter.observability.retry.retry`):
    * **Reads** — `list_models`, `get_instances`, `search_instances`,
      `get_relations` — retry with `is_idempotent=True` and
      `max_retries` from the inbound `Settings`; only transient
      categories (timeout / 429 / 5xx) are retried; the helper enforces
      that.
    * **Writes** — `create_instance`, `update_instance`,
      `delete_instance` — call **exactly once** (`is_idempotent=False`);
      writes are never retried.
  * **Pagination bridge**: the core contract is *cursor-based* (opaque
    `cursor` in, `Page.next_cursor` / `has_next` out; first page is
    `cursor=None`). The vendor is usually page-number-based; encode/decode
    the vendor page number into the opaque cursor (veops does this in
    `mapper.encode_cursor` / `decode_cursor`). `has_next` and
    `next_cursor` are the authoritative "more data?" signals and must be
    correct even when `total` is unknown (`None`).
  * **Unsupported operations** must raise
    `UnsupportedOperationError` — never a generic error — so clients can
    distinguish "not supported" from "the backend is down".
  * **No network I/O at construction time**: the constructor (and the
    factory) must not dial the vendor; `create_app` performs startup
    validation, and the first upstream contact happens on the first tool
    call.
* **`prober.py`** — a reachability probe matching the
  `UpstreamProber` contract in `server/health.py`: a callable taking an
  `UpstreamSettings | None` and returning
  `{"status": "ok"}` or
  `{"status": "unavailable", "reason": ...}`. Mirror the veops
  `make_veops_prober` semantics:
  * 2xx/3xx → `ok`;
  * **401/403 (auth failure) → `ok`** — the service *is* up (it answered);
    auth is not availability;
  * 404/other-4xx → `ok` (the server answered; this exact path may not
    exist);
  * connect/timeout/network errors and 5xx → `unavailable` with a reason
    (the reason is scrubbed of the token by the shared redaction filter
    before it reaches `/readyz`).
  * Keep it read-only and bounded by a short timeout (veops uses a
    fixed 5 s, 0 retries, on a single `GET` to a status endpoint).
  * The probe must never raise — return `unavailable` on any exception.
* **`__init__.py`** — export the factory
  `create_<name>_adapter(settings: Settings) -> CMDBAdapter` (mirror
  `create_veops_adapter`): read the upstream environment, build the
  vendor-specific config (fail closed on missing credentials), construct
  the client/adapter with the inbound timeout/retry budget. No network
  I/O.

### 3.2 Register the adapter (one line)

Adapters self-register *lazily* inside `create_app` in
`server/app.py`. Add a branch that mirrors the veops pattern — the import
is deferred inside the branch so importing
`cmdb_mcp_adapter.server.app` stays free of your vendor's dependencies:

```python
if "<name>" not in _ADAPTER_REGISTRY:
    from cmdb_mcp_adapter.adapters.<name> import create_<name>_adapter

    register_adapter("<name>", create_<name>_adapter)
```

That is the only change to shared code. The factory receives the composed
`Settings` and returns an adapter satisfying `CMDBAdapter`;
`create_app` then wraps it in a `CoreService` and builds the MCP server.
An unknown `MCP_ADAPTER` value fails closed at startup with
`ConfigurationError` listing the registered names.

### 3.3 Configuration

* Inbound/runtime settings (bind, port, auth token, timeout, retries, log
  level) come from the existing `MCP_*` variables — **do not invent a
  second inbound namespace**.
* Upstream credentials: today the shared upstream domain in `config.py`
  is the `VEOPS_*` pair. If your vendor needs its own environment
  variables (e.g. `BLUEKING_BASE_URL` / `BLUEKING_TOKEN`), add their
  `ENV_*` constants to `config.py` and **add them to `ALL_ENV_VARS`**
  (it is the single source of truth used by tests and the secret scan);
  alternatively read `os.environ` directly inside your adapter's
  `config.py`. Either way: document them in `.env.example`
  (placeholder values only) and in
  [Configuration, Security, and Observability](docs/configuration-security-observability.md).
* Adapter selection is **environment-only** (`MCP_ADAPTER=<name>`); there
  is no and must not be a `--adapter` CLI flag.

### 3.4 Tests

* **Mapper tests** (`tests/adapters/test_<name>_mapper.py`) — pure
  mapping: vendor payload shapes → core models, vendor-field quarantine
  into `metadata`, empty/malformed inputs, cursor encode/decode round
  trip. Stdlib + `pytest` only; no network.
* **Client + adapter tests** — drive your client through an
  `httpx.MockTransport` exactly like
  [`tests/adapters/_fakes.py`](tests/adapters/_fakes.py) (recording,
  scriptable fake transport; see the `test_adapter.py` /
  `test_client.py` / `test_prober.py` split in that package). Cover: the
  full status→category table, header/timeout plumbing, pagination across
  pages, the read-vs-write retry policy (and that writes are *never*
  retried), the `UnsupportedOperationError` path, and the prober
  semantics (401 = `ok`, 5xx/timeout = `unavailable`). **No real network,
  no real credentials, ever** in the default suite.
* **Opt-in live smoke** (only if a real endpoint exists to test against)
  — see [`tests/integration/test_veops_live.py`](tests/integration/test_veops_live.py):
  a `skipif` on a vendor-specific env flag (`MCP_VEOPS_LIVE != "1"`) makes
  it skip cleanly in CI and normal runs; real credentials come from the
  environment only and are never committed. New adapters follow the same
  gating pattern.
* The existing `tests/contract/` suite is adapter-agnostic and must
  continue to pass without modification — that is the proof your adapter
  satisfies the contract.

### 3.5 Documentation

* Add `docs/<name>-adapter.md` modeled on
  [docs/veops-adapter.md](docs/veops-adapter.md): assumed/actual API
  contract (endpoints, request/response shapes), configuration table
  (variable name, required?, purpose) with the fail-closed policy stated,
  the error-mapping table, retry behavior, readiness-probe semantics, and
  testing without a real CMDB.
* Add a row to the **Documentation** table in the [README](README.md) and
  a mention in **Supported scope** if the adapter is user-facing.
* Every command, environment variable name, endpoint path, and tool name
  in the new doc is part of the compatibility surface (§5) — the docs
  secret/link scan runs on all `docs/*.md` files automatically.

### 3.6 Pull request checklist

Before requesting review, confirm all of the following:

* [ ] **7-method conformance**: the adapter implements every
      `CMDBAdapter` method with the exact signatures from §3.1;
      `tests/contract/` passes unmodified.
* [ ] **Frozen contract untouched**: no rename/removal/re-purposing of
      the seven MCP tool names (`get_models`, `get_instances`,
      `create_instance`, `update_instance`, `delete_instance`,
      `search_instances`, `get_relations`); no new *required* normalized
      field (vendor values live in `metadata` under the `<name>:`
      prefix); no change to `/mcp`, `/healthz`, `/readyz`, or to the
      `MCP_*` / `VEOPS_*` environment names; error-category meanings
      unchanged. If a change to any of these is *truly* required, it
      needs a migration note in [CHANGELOG.md](CHANGELOG.md) and updates
      to the executable docs checks
      ([`tests/integration/test_docs_examples.py`](tests/integration/test_docs_examples.py))
      in the same PR.
* [ ] **Retry policy**: reads retried via `observability.retry.retry`
      (`is_idempotent=True`), writes never retried.
* [ ] **Fail-closed startup**: mandatory credentials validated at factory
      time with `ConfigurationError`; no network I/O in constructors.
* [ ] **Prober** follows the auth-failure-is-reachable semantics.
* [ ] **All three gates green**: `uv run --no-sync ruff check .`,
      `uv run --no-sync mypy src`, `uv run --no-sync pytest -q`.
* [ ] **No secrets anywhere**: no real or realistic-looking credentials in
      code, docs, examples, or logs. Docs use `REPLACE_ME` /
      `<placeholder>` values; `.env.example` is the only file allowed to
      contain the locked fake token (guarded by
      [`tests/integration/test_secret_scan.py`](tests/integration/test_secret_scan.py)).
* [ ] **Line endings**: text files committed as LF (see §6); binary files
      untouched.
* [ ] **CHANGELOG**: an entry under `[Unreleased]` describing the new
      adapter and any configuration additions.

---

## 4. Future-adapter boundary

**BlueKing, WeCMDB, and iTop are explicitly out of the MVP scope.** Nothing
in the MVP assumes anything about them. This document is the template for
when work on any of them starts: create `adapters/<name>/` per §3.1–§3.6,
keep the core and the MCP tool contract byte-for-byte stable, and do not
touch existing adapters in the same change unless the PR is explicitly
about that.

---

## 5. Frozen compatibility surfaces

These are public API; a change to any of them is a **breaking change**
requiring a migration note, a version bump per
[CHANGELOG.md](CHANGELOG.md#release-process--rollback), and updates to the
executable documentation checks:

* The seven MCP tool names and their parameter/result shapes
  ([MCP Tool Contract](docs/mcp-tool-contract.md)).
* The normalized model fields in `core/models.py` (adding *optional*
  `metadata` content is fine; adding required fields is not).
* The `ErrorCategory` names and their meanings.
* The HTTP endpoints `/mcp`, `/healthz`, `/readyz` and the
  `{"status": ...}` bodies.
* Environment variable names in `config.py` (`ALL_ENV_VARS`) and the
  precedence order (CLI flags > environment > defaults).
* The cursor-paging semantics (`Page.next_cursor` / `has_next`
  authoritative; first page `cursor=None`).

---

## 6. Line endings, `.gitattributes`, `.gitignore`

* **`.gitattributes`** normalizes every text file to **LF**
  (`* text=auto eol=lf`), so Windows and Linux checkouts do not produce
  full-file diffs when only line endings changed. An explicit list of
  binary extensions (`*.zip`, `*.pdf`, `*.png`, …) is never rewritten as
  text. Trellis developer journals
  (`.trellis/workspace/*/journal-*.md`) use `merge=union` because they are
  append-only.
* Verify a file's effective attributes with:

  ```bash
  git check-attr -a <path>        # e.g. git check-attr -a README.md → eol=lf, text=auto
  ```

  If a new file type is binary, add it to the list in `.gitattributes` in
  the same commit that adds the file.
* **`.gitignore`** already covers Python caches (`__pycache__/`,
  `*.py[cod]`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`),
  environments (`.venv/`, `venv/`), packaging output (`dist/`, `build/`,
  `*.egg-info/`), credentials (`.env`, `.env.*` with `!.env.example`,
  `*.pem`, `*.key`, `*.p12`), logs, and OS/editor noise. Never commit a
  `.env`, a key, or a wheel built from unmerged work; commit only the
  source tree plus `.env.example` and `uv.lock`.
