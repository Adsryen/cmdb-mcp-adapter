# Remote HTTP Deployment

This document describes how to run the CMDB MCP adapter as a long-running
Streamable HTTP service, how to connect MCP clients to it, and what the
deployment boundaries are.

## Commands

### Development (loopback)

```bash
cmdb-mcp http --host 127.0.0.1 --port 8000
```

A loopback bind may run without an auth token; the local trust boundary
is the host itself.

### Internal network (non-loopback)

A non-loopback bind **requires** `MCP_AUTH_TOKEN`:

```bash
MCP_AUTH_TOKEN="<token>" cmdb-mcp http --host 0.0.0.0 --port 8000
```

The token is read from the environment at startup.  It is compared in
constant time (`hmac.compare_digest`) against every
`Authorization: Bearer <token>` header on `/mcp`.

## Endpoints

| Method | Path       | Auth     | Purpose                                      |
|--------|------------|----------|----------------------------------------------|
| POST   | `/mcp`     | Bearer*  | MCP Streamable HTTP (initialize, tools/*)    |
| GET    | `/mcp`     | Bearer*  | MCP SSE stream (server → client push)        |
| DELETE | `/mcp`     | Bearer*  | Terminate an MCP session                     |
| GET    | `/healthz` | none     | Liveness probe — always `200 {"status":"ok"}` |
| GET    | `/readyz`  | none     | Readiness probe — `200` / `503` + reasons    |

\* Bearer auth is enforced only when the bind is non-loopback.  Loopback
binds accept `/mcp` without a token.

## Health probes (curl)

```bash
# Liveness — always 200 once the process is running
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}

# Readiness — 200 when the configuration is valid and the upstream
# probe passes; 503 with a redacted reasons list otherwise
curl http://127.0.0.1:8000/readyz
# {"status":"ok"}
# or: {"status":"unavailable","reasons":["configuration: …"]}
```

Both probes are unauthenticated (intended for internal load-balancer /
orchestrator probes).  The readiness `reasons` text is run through the
shared redaction filter (both the inbound `MCP_AUTH_TOKEN` and the
upstream CMDB token are registered as known secrets), so a malformed
configuration or a chatty probe can never leak a secret into the
response body.

## Security boundary (important)

This MVP is an **internal-network** service.  The Bearer token on `/mcp`
is the *entire* inbound security boundary:

* The process speaks **plain HTTP only**.  There is no TLS of its own;
  termination at a reverse proxy / load balancer is deferred (below).
* The MCP SDK's **DNS-rebinding protection is disabled** on `/mcp`
  (together with the SDK's default CORS/origin allow-list), because the
  only clients of this endpoint are programmatic MCP clients that
  authenticate with the token — never browsers.  Disabling it is
  intentional; it is *not* a safety net against rebinding attacks.
* **Do not expose this port to the public internet.**  Binding to
  `0.0.0.0` is for a trusted internal network only; startup refuses a
  non-loopback bind without `MCP_AUTH_TOKEN` (fail-closed), and public
  exposure is a prohibited deployment, not a configuration.

If TLS, a reverse proxy, or rate limiting are added later, the
adapter keeps speaking plain HTTP on an internal socket in front of
them — no command or contract change is required.

## MCP client connections

### HTTP client (curl)

```bash
# Initialize (loopback, no token)
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"initialize",
    "params":{
      "protocolVersion":"2025-06-18",
      "capabilities":{},
      "clientInfo":{"name":"curl","version":"0"}
    }
  }'
```

For a non-loopback bind, add `-H "Authorization: Bearer <token>"`.

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cmdb": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

(For loopback binds the `headers` block can be omitted.)

### MCP CLI inspector

```bash
mcp dev --transport http --url http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <token>"
```

## Session semantics

The adapter runs a **single process** with **one**
`StreamableHTTPSessionManager`.  Sessions are **stateful** by default:

* `POST /mcp` without an `mcp-session-id` header creates a new session
  and returns an `mcp-session-id` in the response headers.
* Subsequent requests carry that session ID and are routed to the same
  in-memory server task.
* `DELETE /mcp` terminates the session.
* There is no shared state between sessions; each session has its own
  MCP protocol state.

Because the session state lives in memory, **restarting the process
invalidates all active sessions**.  Clients must re-initialize after a
restart.  This is acceptable for the single-instance MVP; a multi-replica
deployment would need a session-store abstraction (deferred).

## Timeouts and retries

* **Upstream** (adapter → CMDB) HTTP calls use `MCP_HTTP_TIMEOUT`
  (default 30 s) and `MCP_HTTP_MAX_RETRIES` (default 2, idempotent
  reads only).  These are independent of the MCP client's own
  timeouts.
* **Inbound** (MCP client → adapter) request handling is synchronous
  per request; there is no server-side request timeout beyond the
  uvicorn defaults.  Long-running tool calls block the request until
  the upstream responds or times out.
* The MCP SDK's streamable HTTP transport sends a `retry-interval`
  hint on SSE disconnect; the client may reconnect after that interval.

## Graceful shutdown

* `SIGINT` / `SIGTERM` sets the shared shutdown event
  (`lifecycle.shutdown_event`).
* A watcher task polls the event and sets `uvicorn.Server.should_exit`,
  which triggers uvicorn's normal shutdown sequence: stop accepting
  new connections, let in-flight requests finish, close the session
  manager's task group, exit.
* `flush_stderr()` runs after the server loop returns, so no trailing
  structured log line is lost.
* The process exits with a **non-zero** code (130) on a signal-driven
  shutdown so the caller knows it was interrupted.

## Deferred (not in this MVP)

Each item below can be added later **without changing the `cmdb-mcp
http` command or the MCP tool contract**:

* **Docker / container image** — now provided: `Dockerfile`,
  `docker-compose.yml`, and `scripts/build-docker.sh` at the repository
  root; see [docker-deployment.md](docker-deployment.md).
* **systemd / service unit** — a `[Service]` unit with
  `ExecStart=cmdb-mcp http --host … --port …` and
  `Restart=on-failure`; the process already handles SIGTERM.
* **TLS / TLS termination** — terminate TLS at a reverse proxy or
  load balancer in front; the adapter continues to speak plain HTTP on
  a localhost or internal socket.
* **Reverse proxy / load balancer** — put nginx / haproxy / a cloud LB
  in front; no adapter change required (the adapter is
  single-instance and stateful, so use sticky sessions or a single
  backend until a session store is added).
* **Kubernetes** — deploy as a single `Deployment` replica with the
  same command; add a `sessionStore` sidecar or external store before
  scaling to multiple replicas.
* **Multi-replica / horizontal scaling** — requires moving session
  state out of process memory (e.g. Redis); the MCP contract is
  unchanged.
* **Rate limiting** — add at the proxy/LB layer or as ASGI middleware;
  the adapter's auth and session logic is unaffected.
