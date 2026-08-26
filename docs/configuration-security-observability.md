# Configuration, Security, and Observability

This document is the source of truth for the MVP's environment
configuration, secret handling, authentication policy, logging /
redaction, health probes, retry behavior, timeout, and graceful-shutdown
contract.  It is the reference the `remote-http` and `veops-adapter`
tasks build against.

The MVP is an **internal, single-process** HTTP/stdio adapter.  Public
exposure, TLS, reverse proxies, OAuth, and full distributed tracing are
explicitly **out of scope** (see [Prohibited](#prohibited)).

## Configuration sources and precedence

Configuration is parsed **once at startup** into typed settings
(:class:`cmdb_mcp_adapter.config.Settings` and
:class:`~cmdb_mcp_adapter.config.UpstreamSettings`).  The deterministic
precedence, highest first, is:

1. **CLI flags** — only the `http` subcommand exposes flags, and only
   `--host` / `--port` (which override the bind address and port).  The
   `stdio` subcommand has no such flags.
2. **Environment variables** — the primary source for every other value.
3. **Safe defaults** — loopback bind, no auth token, conservative
   timeouts.

The MVP **does not read configuration files or a secret manager**.  Every
value is an environment variable or a CLI flag.  The `examples/`
directory (if one is added) documents variable *names* only and is never
consulted by the loader; nothing in `src/` opens a file to obtain
configuration (verified by
`tests/integration/test_secret_scan.py`).

## Environment variables

### Inbound / runtime domain (`MCP_*`)

| Variable             | Default       | Description                                                                 |
| -------------------- | ------------- | --------------------------------------------------------------------------- |
| `MCP_BIND_HOST`      | `127.0.0.1`   | Address the HTTP transport binds to. Loopback values may run without a token; any other value **requires** `MCP_AUTH_TOKEN`. |
| `MCP_PORT`           | `8000`        | TCP port the HTTP transport binds to (`1`–`65535`).                          |
| `MCP_ADAPTER`        | `veops`       | Adapter name to compose. MVP registers `mock`; `veops` is added by the veops-adapter task. |
| `MCP_AUTH_TOKEN`     | *(none)*      | Bearer token authenticating **clients** of the adapter's HTTP endpoint. Required for any non-loopback bind. |
| `MCP_HTTP_TIMEOUT`   | `30`          | Upstream HTTP timeout in seconds (positive number).                          |
| `MCP_HTTP_MAX_RETRIES`| `2`          | Maximum retries for **idempotent (read)** upstream operations. Writes are never retried. |
| `MCP_LOG_LEVEL`      | `INFO`        | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.                      |

### Upstream CMDB domain (`VEOPS_*`)

This domain is **independent** from `MCP_AUTH_TOKEN`: it authenticates the
adapter *toward* the VeOps CMDB, not clients of the adapter.  The two
domains must be rotated independently and never share a value.

| Variable           | Default | Description                                                        |
| ------------------ | ------- | ------------------------------------------------------------------ |
| `VEOPS_BASE_URL`   | *(none)*| Base URL of the VeOps CMDB API. Must be an `http(s)` URL when set; validated at startup (fail closed). |
| `VEOPS_KEY`        | *(none)*| VeOps user key (identity); sent as the `_key` query parameter. Required by the `veops` adapter (fail closed). |
| `VEOPS_SECRET`     | *(none)*| VeOps user secret; signing material for the per-request `_secret` signature (SHA-1 over path + secret + sorted parameter values; see [VeOps Adapter](veops-adapter.md#authentication-keysecret-signing)). Required by the `veops` adapter (fail closed); never logged. |
| `VEOPS_TOKEN`      | *(none)*| **Deprecated in 0.2.0, never used to authenticate.** Read for diagnostics only: if set while `VEOPS_KEY`/`VEOPS_SECRET` are missing, startup fails with a migration error naming the two new variables. |

A copy of this table with placeholder values lives in [`.env.example`](../.env.example).

## Authentication policy

The inbound auth model is minimal and **fail-closed**
(:mod:`cmdb_mcp_adapter.server.auth`):

* **Loopback bind** (`127.0.0.1` / `::1` / `localhost`) — the endpoint may
  accept requests **without** a token.  The local host is the trust
  boundary.
* **Non-loopback bind** — `MCP_AUTH_TOKEN` **must** be configured (enforced
  at startup by :func:`cmdb_mcp_adapter.config.validate_settings`; the
  process exits non-zero before any protocol activity if it is missing).
  Every request must then carry `Authorization: Bearer <token>`; tokens
  are compared in constant time (`hmac.compare_digest`).
* **No token configured on a non-loopback bind** — rejected at startup.
  There is no implicit open mode for a network bind.

The decision function `authorize(settings, headers) -> str | None` returns
`None` (allow) or an error message.  `make_auth_middleware(settings)`
returns a callable the HTTP transport
([`docs/remote-http.md`](remote-http.md)) wraps into an ASGI middleware
middleware; the policy has exactly one owner in this module.

**Token rotation:** change `MCP_AUTH_TOKEN` (and/or `VEOPS_KEY` /
`VEOPS_SECRET`) in the process environment and **restart the process**.  There is no runtime
re-load; a restart is the rotation mechanism.  Tokens are never written
to disk, logged, or serialized into diagnostics.

## Observability: structured logging

`configure_logging(level)` (called by both transport launchers at
startup) emits **one JSON object per line on `stderr`** using the stdlib
`logging` and a custom formatter — no third-party logging library.  Each
line contains:

```json
{"timestamp": "...", "level": "INFO", "logger": "cmdb_mcp_adapter.core",
 "message": "operation end",
 "extra": {"correlation_id": "...", "tool": "get_instances",
           "adapter": "MockAdapter", "duration_ms": 1.23,
           "outcome": "ok", "event": "operation.end"}}
```

`stderr` is the diagnostics stream; it never carries MCP protocol frames
(`stdout` is reserved for the stdio protocol).

### Correlation IDs

`new_correlation_id()` creates a 128-bit uuid4 (hex).  Each MCP tool
invocation is assigned one ID (in the tool layer) and it is propagated
through the core service via a context variable, so every log line
emitted during that operation — adapter errors, retries, timeouts —
carries the same ID.  The core service logs one structured line per
operation with the fields above; `outcome` is `"ok"` or the stable error
category name.

### Redaction

`redact(obj, secret_values)` and the `RedactingFilter` (attached to the
root logger) prevent credential leakage:

* **Key-based** — a mapping key matching `token`, `secret`, `password`,
  `authorization`, `cookie`, `credential`, `private_key`, `api_key`, ...
  has its value replaced by `<redacted>` regardless of the value.
* **Value-based** — string values that look like bearer tokens (long
  high-entropy strings, `Bearer <...>` prefixes) are redacted wherever
  they appear, including nested inside large payloads.
* **Known secrets** — the configured tokens are passed to the filter as
  exact-match secrets and are redacted even if not token-shaped.

`upstream_detail` (raw vendor error text) is **never** emitted to MCP
clients and is redacted before it reaches a log line.  Full upstream
payloads are never logged verbatim.

## Health checks

* **Liveness** (`check_liveness`) — `{"status": "ok"}`.  Answers "is the
  process alive?" and must never fail once the process is running,
  including while it is draining during shutdown.  No work is performed.
* **Readiness** (`check_readiness`) — answers "is the process ready to
  serve?"  It validates the inbound configuration (a non-loopback bind
  requires a token) and the upstream configuration, then runs an
  **injectable, non-destructive** upstream reachability probe.  The MVP
  default probe is a no-op that reports `ok`; the veops-adapter task
  supplies a real (bounded, read-only) HTTP prober via the
  `build_asgi_app(prober=…)` injection point.  A probe failure or
  bad configuration yields `{"status": "unavailable", "reason": ...}`.

Liveness and readiness are distinct: a process can be *alive* (liveness
ok) but *not ready* (readiness unavailable) while a dependency is down or
the configuration is invalid.

## Timeouts, retry, and error mapping

* **Timeout** — upstream HTTP calls use a bounded timeout
  (`MCP_HTTP_TIMEOUT`, default 30s).
* **Retry** — `retry(fn, is_idempotent, max_retries, ...)` retries **only
  idempotent (read) operations** and **only** for the transient categories
  *timeout*, *rate limit*, and *upstream-server* (5xx).  Write operations
  (create/update/delete) are **never** retried — the first failure is
  returned as-is.  Backoff is exponential with full jitter, capped by
  `backoff_cap_seconds`.  On exhaustion the **original error (and its
  stable category) is re-raised unchanged** — a timeout stays a timeout.
* **Error categories** — the seven-plus stable categories from
  `core/errors.py` (`validation`, `authentication`, `authorization`,
  `not_found`, `timeout`, `rate_limit`, `upstream_server`,
  `unsupported_operation`, `configuration`, `internal`).  Non-retryable
  categories (validation, auth, authorization, not-found, configuration)
  are never retried.

## Graceful shutdown

`install_signal_handlers()` registers `SIGINT` / `SIGTERM` handlers that
set a process-wide shutdown event.  The stdio launcher installs them
before entering the MCP loop and, on exit, calls `flush_stderr()` so no
structured log line is lost; `restore_default_signals()` returns the
process to its normal signal behavior.  The HTTP launcher
([`docs/remote-http.md`](remote-http.md)) uses the same event: a watcher
task observes it, sets `uvicorn.Server.should_exit`, and the process
exits with a non-zero code after flushing stderr.

## Prohibited

The following are **explicitly out of scope** for the MVP and must not be
introduced without a deliberate, documented decision:

* **Public / internet exposure** of the adapter endpoint.
* **TLS / TLS termination** at the adapter (deferred; a reverse proxy or
  load balancer in front is the deployment task's concern, not the
  adapter's).
* **Reverse proxies / load balancers** as part of the adapter process.
* **OAuth / OIDC** (the MVP uses a single static bearer token only).
* **Full distributed tracing / APM platforms** (correlation IDs only).
* **Writing secrets to disk, to logs, or into example files.**
* **Reading configuration from files or a secret manager.**

These constraints are enforced by the fail-closed startup validation and
by the secret-scan / log-leak regression tests.
