# Operations

Operational reference for running the CMDB MCP adapter in production
on an internal network: logging, health endpoints, upgrade/rollback,
credential rotation, and a troubleshooting table. Everything here is
extracted from the normative documents — the source of truth remains
[Configuration, Security, and
Observability](configuration-security-observability.md),
[Remote HTTP](remote-http.md), and
[Local stdio](local-stdio.md).

## Logs

Both transports write **one JSON object per line on `stderr`** (never
on `stdout`, which is protocol-only in the stdio transport).  Logs are
emitted through the stdlib `logging` module with a `RedactingFilter`;
no third-party logging library is used.

```json
{"timestamp": "...", "level": "INFO", "logger": "cmdb_mcp_adapter.core",
 "message": "operation end",
 "extra": {"correlation_id": "...", "tool": "get_instances",
           "adapter": "MockAdapter", "duration_ms": 1.23,
           "outcome": "ok", "event": "operation.end"}}
```

* **Redaction** — mapping keys named `token` / `secret` / `password` /
  `authorization` / … are replaced with `<redacted>`; string values
  that look like bearer tokens are redacted wherever they appear; and
  the configured secrets (`MCP_AUTH_TOKEN`, `VEOPS_KEY`, `VEOPS_SECRET`) are
  registered as exact-match known secrets.  Raw upstream detail is
  never logged verbatim.
* **Correlation IDs** — every tool invocation gets a 128-bit
  correlation ID that propagates through core and adapter, so all
  lines belonging to one operation (including retries and timeouts)
  share it.
* **Level** — `MCP_LOG_LEVEL` (`DEBUG`…`CRITICAL`, default `INFO`).
* **Startup errors** — a fail-closed configuration failure prints one
  line `[stdio] configuration error: <category>: <msg>` (or the
  HTTP equivalent) to stderr **before** any protocol activity and
  exits non-zero; see [Local
  stdio §7.2](local-stdio.md#72-startup-failure--missing-credentials-fail-closed).

## Health endpoints (HTTP transport)

| Probe  | Path       | Auth   | Behavior |
| ------ | ---------- | ------ | -------- |
| Liveness  | `GET /healthz` | none | Always `200 {"status":"ok"}` once the process is running, including while draining on shutdown. |
| Readiness | `GET /readyz`  | none | `200` when configuration is valid and the upstream probe passes; `503 {"status":"unavailable","reasons":[…]}` otherwise. Reasons are redacted. |

The veops adapter wires a real, bounded, read-only probe into
`/readyz` (`GET /status` against the CMDB; 5-second timeout); the mock
adapter uses a no-op prober.  Both probes are unauthenticated by
design (internal load-balancer/orchestrator probes).  See
[Remote HTTP: Health probes](remote-http.md#health-probes-curl).

## Upgrade

There is no in-place version bump.  Upgrade by **rebuilding/reinstalling
the new version**:

```text
# developer (source checkout)
uv sync
uv build            # if you install from local wheels

# user (pipx / pip)
pipx upgrade cmdb-mcp-adapter                                  # private index
pip install --upgrade --index-url <private-index-url> cmdb-mcp-adapter
pip install --force-reinstall dist/cmdb-mcp_adapter-<ver>-py3-none-any.whl
```

Because session state in the HTTP transport is in-memory, **a restart
invalidates all active MCP sessions**; clients re-initialize after the
new process is ready (`/readyz` → 200).  The console command name does
not change, so the MCP host configuration (`command`/`args`/`env`)
stays identical across upgrades.

## Rollback

Roll back by stopping the process and starting the **previous
known-good version** (wheel or private-index revision recorded in
[CHANGELOG.md](../CHANGELOG.md#release-process--rollback)):

```text
# stop the running service
kill <pid>            # SIGINT/SIGTERM; the process flushes stderr and exits

# start the previous version with the same environment
MCP_AUTH_TOKEN=<token> ... uv run --with 'cmdb-mcp-adapter==<prev>' cmdb-mcp http
# or: pip install cmdb-mcp-adapter==<prev> --index-url <private-index-url>
```

No data migration is involved: the adapter holds no persistent state,
and the MCP protocol is stateless across a re-initialization.

## Credential rotation

Both token domains are **rotated by changing the environment variable
and restarting the process** — there is no runtime re-load:

* `MCP_AUTH_TOKEN` — inbound client token.  Update it in the process
  environment, restart, then update the `Authorization: Bearer <token>`
  header in each MCP client's config.  Active sessions are dropped by
  the restart, so rotate clients first or accept a short re-init window.
* `VEOPS_KEY` / `VEOPS_SECRET` — upstream CMDB credential pair (used
  for the per-request `_key`/`_secret` signature).  Rotate them in the
  CMDB (user management), then update the environment and restart.

The two domains (inbound client token and upstream credential pair)
are **independent** and must never share values.
Tokens are never written to disk, to logs, or into diagnostics.

## Troubleshooting

| Symptom | Likely cause | Check / fix |
| ------- | ------------ | ----------- |
| Process exits immediately (exit `1`), one line `[stdio] configuration error: configuration: …` on stderr, 0 bytes on stdout | Fail-closed startup validation (see below) | Read the stderr line; it names the exact missing/invalid variable. |
| `configuration: adapter '<name>' is not available in this build` | `MCP_ADAPTER` names an unregistered adapter | MVP registers `mock` and `veops` only. |
| `configuration: VEOPS_BASE_URL is required for the veops adapter` | `MCP_ADAPTER=veops` with `VEOPS_BASE_URL` unset | Set an `http(s)` base URL, or use `MCP_ADAPTER=mock`. |
| `configuration: VEOPS_KEY and VEOPS_SECRET are required for the veops adapter` | `VEOPS_BASE_URL` set but `VEOPS_KEY`/`VEOPS_SECRET` unset | Set the key/secret pair (env only — never on the command line). |
| `configuration: Invalid VEOPS_BASE_URL value: …` | Base URL is not `http(s)` | Use a full `http(s)://` URL. |
| HTTP: `configuration: binding to non-loopback … requires MCP_AUTH_TOKEN` | Non-loopback bind without inbound token | Set `MCP_AUTH_TOKEN`, or bind loopback. |
| `/readyz` returns `503` with `reasons` | Bad configuration or upstream probe failed | The `reasons` text is redacted; check the stderr log for the same correlation. For veops, a 5xx / connect / timeout from the CMDB makes readiness unavailable while liveness stays `200`. |
| Tool call returns `authentication: …` | CMDB rejected the `VEOPS_KEY`/`VEOPS_SECRET` signature (upstream `401`; both a missing and a wrong signature return 401) | Rotate/fix the key/secret pair. |
| Tool call returns `authorization: …` | CMDB denied the action (`403`) | Check the CMDB account's permissions. |
| Tool call returns `timeout: …` | Upstream did not answer within `MCP_HTTP_TIMEOUT` (default 30 s, after retries) | Raise `MCP_HTTP_TIMEOUT` or fix the CMDB/network. |
| Tool call returns `rate_limit: …` | Upstream throttled (`429`; retried then exhausted) | Reduce client concurrency. |
| `401 Unauthorized` from `/mcp` (non-loopback bind) | Missing or wrong `Authorization: Bearer <token>` header | Set the header to the current `MCP_AUTH_TOKEN`; note the token rotates only by process restart. |
| `404` JSON on an unknown HTTP path | Request to a path other than `/mcp`, `/healthz`, `/readyz` | Use the endpoints table in [Remote HTTP](remote-http.md#endpoints). |
| `cmdb-mcp` with no subcommand exits `2` | Diagnostic-only invocation (usage text on stdout) | Provide `stdio` or `http`. |

Full exit-code contract (including `SIGINT`/`SIGTERM` behavior and the
protocol-only-stdout rule): [Local
stdio §7](local-stdio.md#7-exit-codes-signals-startup-failure-timeouts-missing-credentials).
Upstream status → category mapping table: [VeOps
Adapter](veops-adapter.md); category semantics:
[MCP Tool
Contract](mcp-tool-contract.md#error-categories).
