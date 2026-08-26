# cmdb-mcp-adapter

A **generic CMDB → MCP adapter**. It exposes CMDB resources (models,
instances, search, mutations, relations) through the
[Model Context Protocol](https://modelcontextprotocol.io), so any MCP
client (Claude Desktop, the `mcp` CLI, your own agent) can read and
mutate CMDB data through one stable, vendor-neutral tool contract.

The first vendor adapter is **`veops`** (the VeOps CMDB); an
in-process **`mock`** adapter provides a deterministic offline dataset
for development. Both adapters expose the **same seven MCP tools**
(see [MCP Tool Contract](docs/mcp-tool-contract.md)):
`get_models`, `get_instances`, `create_instance`, `update_instance`,
`delete_instance`, `search_instances`, `get_relations`.

The runtime is a **single Python process** with two transports:

* **Streamable HTTP** (primary team deployment) — long-running service
  on an internal network, see [Remote HTTP](docs/remote-http.md).
* **Local stdio** (secondary compatibility path) — host-launched
  child process, see [Local stdio](docs/local-stdio.md).

Both transports share one composition root
(`cmdb_mcp_adapter.server.app.create_app`) — there is exactly one
server implementation.

---

## Architecture

One Python process, one composition root
(`cmdb_mcp_adapter.server.app.create_app`), two transports, any number
of adapters:

```text
MCP host (client)
   │  stdio (host-launched child)          HTTP (Streamable HTTP, /mcp)
   ▼                                        ▼
transport layer  ──  server/stdio.py  |  server/http.py + server/asgi.py
   ▼
cmdb_mcp_adapter.server.mcp_server   (FastMCP / low-level dispatcher: the 7 tools)
   ▼
cmdb_mcp_adapter.core.CoreService    (validation, page-size clamping, logging)
   ▼
CMDBAdapter protocol                   (core/adapter.py — vendor-neutral)
   ├── adapters/mock.py               (offline, credential-free)
   ├── adapters/veops/                (VeOps CMDB; lazy registration)
   └── adapters/<future>/             (e.g. BlueKing — see Contributing)
   ▼
vendor CMDB HTTP API
```

`core/` contains no vendor logic; every vendor's endpoints, fields, and
errors are translated *inside* its own adapter package before the core or
the tool layer ever sees them.

---

## Supported scope

| Dimension          | Current (MVP)                                                            |
| ------------------ | ------------------------------------------------------------------------ |
| Transports         | Streamable HTTP (primary team deployment); local stdio (secondary)       |
| Adapters           | ``veops`` (real CMDB), ``mock`` (offline)                                |
| Deferred adapters  | BlueKing, WeCMDB, iTop — out of MVP scope; see [Contributing](#contributing) |
| Python             | 3.10–3.12 ([policy](docs/python-support-policy.md))                      |
| Deployment         | Single process, internal network, plain HTTP                              |
| Deferred           | No TLS/Docker/systemd/reverse proxy/OAuth yet — see [MVP boundaries](#mvp-boundaries) |

---

## Quickstart

Requires Python 3.10+ and
[`uv`](https://docs.astral.sh/uv/) (see
[Python Support Policy](docs/python-support-policy.md); the committed
`uv.lock` records the Alibaba PyPI mirror as its index, so if your
environment blocks the default index use:
`uv sync --index-url https://mirrors.aliyun.com/pypi/simple/`).

### 1. Run the Streamable HTTP service (primary)

From a source checkout:

```bash
uv sync
uv run cmdb-mcp --version    # cmdb-mcp 0.2.0 (no credentials, no network)
uv run cmdb-mcp http --help
```

Environment variables (full table in
[Configuration, Security, and Observability](docs/configuration-security-observability.md)):

| Variable            | Default     | Meaning                                                        |
| ------------------- | ----------- | -------------------------------------------------------------- |
| `MCP_BIND_HOST`     | `127.0.0.1` | Bind address. Non-loopback **requires** `MCP_AUTH_TOKEN`.      |
| `MCP_PORT`          | `8000`      | TCP port (`1`–`65535`).                                         |
| `MCP_ADAPTER`       | `veops`     | `veops` (real CMDB) or `mock` (offline, credential-free).      |
| `MCP_AUTH_TOKEN`    | *(none)*    | Bearer token authenticating *clients* of `/mcp`. Required when the bind is non-loopback. |
| `VEOPS_BASE_URL`    | *(none)*    | VeOps CMDB base URL (required for the `veops` adapter).        |
| `VEOPS_KEY`         | *(none)*    | VeOps user key (with `VEOPS_SECRET`, required for `veops`; used for per-request signing). |
| `VEOPS_SECRET`      | *(none)*    | VeOps user secret (with `VEOPS_KEY`, required for `veops`; never logged). |

Start with the mock adapter (no credentials, no network):

```bash
MCP_ADAPTER=mock uv run cmdb-mcp http
```

Start against a real CMDB (values are placeholders — replace them):

```bash
MCP_ADAPTER=veops \
MCP_AUTH_TOKEN=REPLACE_ME \
MCP_BIND_HOST=0.0.0.0 MCP_PORT=8000 \
VEOPS_BASE_URL=http://veops.example.com:8000 \
VEOPS_KEY=your-veops-key \
VEOPS_SECRET=your-veops-secret \
uv run cmdb-mcp http
```

Verify the process:

```bash
# Liveness — always 200 once the process is running
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}

# Readiness — 200 when configuration is valid and the upstream probe
# passes; 503 with a redacted reasons list otherwise
curl http://127.0.0.1:8000/readyz
# {"status":"ok"}
```

> **Internal-network boundary (MVP).** The service speaks **plain HTTP
> only**: no TLS of its own, no built-in reverse proxy, no OAuth. The
> Bearer token on `/mcp` is the entire inbound security boundary, and a
> non-loopback bind fails closed at startup without `MCP_AUTH_TOKEN`.
> Do not expose the port to the public internet — see
> [Remote HTTP: Security boundary](docs/remote-http.md#security-boundary-important).

### 2. Run as a local stdio process (secondary)

An MCP host spawns `cmdb-mcp stdio` as a child process and speaks
JSON-RPC on its `stdin`/`stdout`. Add to your MCP host config
(e.g. `claude_desktop_config.json`); replace the placeholders:

```json
{
  "mcpServers": {
    "cmdb": {
      "command": "/home/you/.local/bin/cmdb-mcp",
      "args": ["stdio"],
      "env": {
        "MCP_ADAPTER": "veops",
        "VEOPS_BASE_URL": "http://veops.example.com:8000",
        "VEOPS_KEY": "your-veops-key",
        "VEOPS_SECRET": "your-veops-secret",
        "MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

On Windows the `command` value is the path to `cmdb-mcp.exe` with
escaped backslashes, e.g.
`"C:\\Users\\you\\.local\\bin\\cmdb-mcp.exe"`; `args` and `env` are
byte-identical. A credential-free variant replaces the whole `env`
block with `{ "MCP_ADAPTER": "mock" }`. Credentials travel **only**
through `env`, never `args` — full platform notes and the exit-code
contract live in [Local stdio](docs/local-stdio.md).

### Platforms (Windows / Linux / macOS)

The `cmdb-mcp` binary behaves identically on all three platforms; the
only differences are shell-specific (how the MCP host sets environment
variables and quotes paths):

* POSIX vs. Windows environment variables, path separators, and JSON
  escaping — [Local stdio §1.6](docs/local-stdio.md#16-platform-differences-windows-vs-linux-vs-macos).
* Bind addresses, token handling, and the internal-network boundary
  for the HTTP service —
  [Remote HTTP](docs/remote-http.md).
* `uv` install / resolution notes (including the Alibaba mirror) —
  [Local stdio §1.1](docs/local-stdio.md#11-developer-path-all-three-platforms).

### Mock mode, health checks, troubleshooting

* **Mock mode** — `MCP_ADAPTER=mock` runs the in-process
  [MockAdapter](src/cmdb_mcp_adapter/adapters/mock.py) with a
  deterministic offline dataset (8 `host` + 2 `service` instances and
  two relations). No credentials, no network — use it for development,
  demos, and Inspector sessions
  ([details](docs/local-stdio.md#5-mock-mode-mcp_adaptermock)).
* **Health checks** — `GET /healthz` (liveness, always `200` once
  running) and `GET /readyz` (readiness, `200`/`503` with redacted
  reasons), both unauthenticated;
  [Remote HTTP: Health probes](docs/remote-http.md#health-probes-curl)
  and [the probes contract](docs/configuration-security-observability.md#health-checks).
* **Troubleshooting** — exit codes, fail-closed startup errors,
  token rotation, and upstream error categories are consolidated in
  [Operations](docs/operations.md) and
  [Local stdio §7](docs/local-stdio.md#7-exit-codes-signals-startup-failure-timeouts-missing-credentials).

---

## Documentation

| Document | Contents |
| -------- | -------- |
| [MCP Tool Contract](docs/mcp-tool-contract.md) | The seven frozen tool names, parameter schemas, normalized output shapes, and error categories. |
| [Configuration, Security, and Observability](docs/configuration-security-observability.md) | Environment variable tables, precedence, auth policy, logging/redaction, health probes, retry/timeout, shutdown. |
| [Remote HTTP](docs/remote-http.md) | Running the Streamable HTTP service, endpoints, client connections (curl, Claude Desktop, Inspector), session semantics, security boundary. |
| [Local stdio](docs/local-stdio.md) | Host-launched stdio: install/upgrade/uninstall, MCP host configuration, stdout discipline, Inspector debugging, mock mode, exit codes. |
| [VeOps Adapter](docs/veops-adapter.md) | The `veops` adapter: assumed API contract, configuration, error mapping, retry, readiness probe, testing without a real CMDB. |
| [Python Support and Dependency Policy](docs/python-support-policy.md) | Supported Python versions (3.10–3.12), dependency/lockfile policy, SDK upgrade and rollback rules. |
| [Operations](docs/operations.md) | Logs, health endpoints, upgrade/rollback, token rotation, troubleshooting table. |
| [Changelog](CHANGELOG.md) | Release history, versioning, and rollback process. |
| [`.env.example`](.env.example) | Every environment variable with placeholder values. |

---

## Installing the package (no public PyPI publish)

`cmdb-mcp-adapter` is **not published to public PyPI**. The user
install path is a **local wheel** or a **private index**:

```bash
# build a local wheel (hatchling backend; no network needed)
uv build
# install it into an isolated environment (pipx recommended)
pipx install dist/cmdb-mcp_adapter-0.2.0-py3-none-any.whl
# or into a virtualenv
python -m venv .venv && source .venv/bin/activate
pip install dist/cmdb-mcp_adapter-0.2.0-py3-none-any.whl
```

The developer path is always `uv sync` + `uv run cmdb-mcp …` in a
source checkout. Details (upgrade, uninstall, diagnostics) are in
[Local stdio §1](docs/local-stdio.md#1-install-upgrade-uninstall-diagnose);
the versioning and rollback process is in
[CHANGELOG.md](CHANGELOG.md#release-process--rollback).

---

## Contributing

Development happens in this repository; see **[CONTRIBUTING.md](CONTRIBUTING.md)**
for the project layout, the local development loop, and the step-by-step
process for adding a new CMDB adapter (the `veops` adapter is the
reference).

* **Quality gates** — a change is not done until all three are green:
  `uv run --no-sync ruff check .`, `uv run --no-sync mypy src`,
  `uv run --no-sync pytest -q`.
* **Frozen contract** — the seven MCP tool names, the normalized fields,
  the `/mcp` / `/healthz` / `/readyz` endpoints, and the `MCP_*` /
  `VEOPS_*` environment names are public API; changing any of them is a
  breaking change that needs a migration note
  ([details](CONTRIBUTING.md#5-frozen-compatibility-surfaces)).
* **Line endings** — text files are committed as LF; the `.gitattributes`
  policy and `.gitignore` coverage are explained in
  [CONTRIBUTING §6](CONTRIBUTING.md#6-line-endings-gitattributes-gitignore).

---


## MVP boundaries

The following are **explicitly out of scope** for the current MVP
(deferred; see
[Prohibited](docs/configuration-security-observability.md#prohibited)):

* **Docker / container images** and **systemd unit files**.
* **TLS** termination and **reverse proxies / load balancers** in front
  of the service (the adapter keeps speaking plain HTTP on an internal
  socket).
* **OAuth / OIDC** (a single static bearer token only).
* **TypeScript client SDK** and any non-Python implementation.
* **Kubernetes deployment** and multi-replica session stores.
* **Windows service installer** (stdio is host-launched, not a service).
* **Distributed tracing / APM platforms** (correlation IDs only).

Adding any of these is a deliberate, documented decision, not a
configuration.
