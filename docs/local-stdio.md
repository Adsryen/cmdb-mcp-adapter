# Local stdio Run and Development Workflow

This document is the source of truth for the **local compatibility path**:
running the adapter as a host-launched `stdio` process on a desktop
(workstation) rather than as the team's Streamable HTTP service.  It covers
cross-platform install / upgrade / uninstall / diagnosis, MCP host
configuration, the stdout discipline, Inspector debugging, mock mode, the
opt-in real-CMDB smoke test, and the exit-code / signal contract.

The stdio path is a **secondary** deployment target.  The team-deployment
primary is the HTTP service (see [`remote-http.md`](remote-http.md)).  Both
transports reuse the **same** composition root (`create_app`) and the **same**
configuration boundary — there is exactly one server implementation in this
repository.  Nothing here defines a second server; `stdio` is one transport
launcher over the shared `cmdb-mcp-adapter` object.

---

## 1. Install, upgrade, uninstall, diagnose

Two user modes exist:

| Mode | Who | How the process is launched |
|------|-----|------------------------------|
| **Developer** (source checkout) | Contributors | `uv` inside a git checkout; the `cmdb-mcp` console script is available via `uv run`. |
| **User** (installed package) | End users who have a Python environment | A package manager (`pip` / `pipx`) installs the `cmdb-mcp-adapter` wheel, which provides the `cmdb-mcp` console command. |

> **MVP note on packaging.** `cmdb-mcp-adapter` is **not published to
> public PyPI**.  The user path is a **local wheel** built from the source
> (`uv build` produces `dist/cmdb-mcp_adapter-<version>-py3-none-any.whl`)
> or a wheel mirrored to a **private index** (e.g.
> `pip install --index-url <private-index-url> cmdb-mcp-adapter` /
> `pipx install --index-url <private-index-url> cmdb-mcp-adapter`).  See
> the [README](../README.md) "Installing the package" section and
> [`CHANGELOG.md`](../CHANGELOG.md#release-process--rollback) for the
> release/rollback process.  The *developer* path (`uv run`) is the
> supported way to use it today on all three platforms.

### 1.1 Developer path (all three platforms)

Prerequisite: [`uv`](https://docs.astral.sh/uv/) installed and on `PATH`.

```text
# from the repository root
uv sync
uv run cmdb-mcp --version
```

On a machine where `uv` is not installed, install it first (see the official
`uv` docs for the platform one-liner).  The lockfile `uv.lock` pins the
Alibaba mirror as its index; if your environment blocks the default index,
resolve against the same mirror:

```text
uv sync --index-url https://mirrors.aliyun.com/pypi/simple/
```

### 1.2 User path (installed console command)

There is **no public PyPI publish**: obtain the wheel by building it
(`uv build`, hatchling backend, no network required) or by pulling it
from your organization's **private index** if one mirrors the
release artifacts.  Install into an isolated environment — `pipx` is
recommended for a single CLI so it does not pollute a site-packages:

```text
# build the local wheel from the source checkout (hatchling backend)
uv build

# pipx, from the local wheel (isolated venv per app)
pipx install dist/cmdb-mcp_adapter-<version>-py3-none-any.whl

# or plain pip into a virtualenv
python -m venv .venv && source .venv/bin/activate   # POSIX
# PowerShell (Windows):  python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install dist/cmdb-mcp_adapter-<version>-py3-none-any.whl

# alternatively, from a private index that mirrors the release artifacts
pip install --index-url <private-index-url> cmdb-mcp-adapter
pipx install --index-url <private-index-url> cmdb-mcp-adapter
```

After install the command is on `PATH` as `cmdb-mcp`.

### 1.3 Upgrade

There is no in-place "version bump" command.  Upgrade by **re-running the
install for the new version** — the console command is the same name, so the
new version simply replaces the old:

```text
uv sync            # developer: refresh the checkout's env from the lockfile
pipx upgrade cmdb-mcp-adapter     # user (pipx, from the index)
pip install --upgrade --index-url <private-index-url> cmdb-mcp-adapter   # user (pip, private index)
# or, with a freshly built local wheel:
pip install --force-reinstall dist/cmdb-mcp_adapter-<version>-py3-none-any.whl
```

The MCP host does not need any change on upgrade: the `command`/`args`/`env`
in its config stay identical; only the resolved `cmdb-mcp` executable under
it changes.

### 1.4 Uninstall

```text
pipx uninstall cmdb-mcp-adapter          # user (pipx)
pip uninstall cmdb-mcp-adapter           # user (pip)
rm -rf <checkout>                        # developer: remove the source checkout
```

On a developer machine that was using `uv`, removing the checkout also removes
its `.venv`; there is no global state left behind.

### 1.5 Diagnostics (no CMDB contact)

These work **without any credentials and without network access**, so they are
safe to run as a first "did it install?" check:

```text
cmdb-mcp --version        # or: cmdb-mcp -V   ->  "cmdb-mcp <version>"
cmdb-mcp --help           # lists the stdio / http subcommands
cmdb-mcp stdio --help     # stdio-specific help
cmdb-mcp http --help      # http-specific help (--host / --port)
```

The `--version`/`--help` actions are handled by the argument parser before any
settings are loaded, so they can never fail on missing credentials.

### 1.6 Platform differences (Windows vs Linux vs macOS)

The `cmdb-mcp` binary behaves identically everywhere; the only differences are
in *how the shell sets environment variables and quotes paths* when the **MCP
host** (not the adapter) launches it.

| Concern | POSIX (Linux / macOS) | Windows (PowerShell) |
|---------|----------------------|-----------------------|
| Path separator | `/` (forward slash) | `\` (backslash) in `PATH`; use forward or escaped backslash in JSON strings |
| Absolute path to command | `/usr/local/bin/cmdb-mcp` or `~/.venv/bin/cmdb-mcp` | `C:\Users\you\.local\bin\cmdb-mcp.exe` |
| Set an env var in a shell | `export VEOPS_KEY=...` | `$env:VEOPS_KEY = "..."` |
| Quote a value with spaces | `export X='a b'` | `$env:X = "a b"` |
| Quoting inside a JSON config | JSON always uses `"` — escape embedded quotes as `\"` | same JSON escaping; Windows path `C:\x\y` must be written `C:\\x\\y` in a JSON string |
| Executable on `PATH` | `cmdb-mcp` | `cmdb-mcp.exe` (Python console scripts on Windows carry `.exe`) |
| Execution policy (only if you run a `.ps1` to launch it) | n/a | If Windows blocks scripts, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` or invoke `python` directly. The `cmdb-mcp.exe` console script is a normal executable and is **not** subject to the PS execution policy. |

**Key rule that holds on every platform:** the MCP host owns process
start-up and environment injection.  The adapter never reads interactive
input from `stdin` (stdin carries MCP protocol frames), and it never
reads credentials from the command line (see §2 — credentials are env-only).

---

## 2. MCP host configuration (stdio)

The MCP host spawns the adapter as a **child process** and speaks JSON-RPC
over the child's `stdin`/`stdout`.  The host is configured with a small
schema; the **only** fields that matter for stdio are:

| Field | Meaning | Notes |
|-------|---------|-------|
| `command` | Absolute path to the executable to spawn. | Absolute paths are strongly preferred so the host does not depend on its own `PATH` resolution. On Windows include the `.exe`. |
| `args` | Array of command-line arguments. | For this adapter this is exactly `["stdio"]`. **Never put credentials here.** |
| `env` | Object mapping env-var names to values injected into the child. | The **only** supported channel for credentials (see below). |
| `cwd` | Working directory to launch in (optional). | The adapter reads no files for config, so `cwd` is cosmetic; set it to the checkout or a stable dir if you want relative logs. |

### 2.1 Credential rule (all platforms)

> **Credentials travel *only* through the `env` mapping — never through
> `args`.**

Command-line arguments are visible in the OS process table (`ps`, Task
Manager, `wmic process`) to any local user, so a token in `args` is a
credential leak.  The adapter reads `VEOPS_BASE_URL` / `VEOPS_KEY` /
`VEOPS_SECRET` (and the inbound `MCP_*` vars) **exclusively** from the
process environment.  There is no `--token` / `--url` flag and no
file-based secret source.

### 2.2 Example A — Claude Desktop style JSON

Claude Desktop (and other hosts that use a `mcpServers` JSON block) expects a
name → `{command, args, env}` map.  This example uses the **veops** adapter
and shows placeholder values you replace in your own host config file:

**POSIX (Linux / macOS):**

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

**Windows (PowerShell / Claude Desktop on Windows):**

```json
{
  "mcpServers": {
    "cmdb": {
      "command": "C:\\Users\\you\\.local\\bin\\cmdb-mcp.exe",
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

The only platform difference is the `command` path (backslashes escaped in
the JSON string, `.exe` suffix).  `args` and `env` are byte-identical across
platforms.

> **Mock variant** — replace the `env` block with `{ "MCP_ADAPTER": "mock" }`
> to run with no credentials at all (see §5).

### 2.3 Example B — MCP Inspector / `mcp` CLI style

The official `mcp` Python SDK ships a CLI (`mcp`) whose `dev` / `run` /
`install` subcommands accept a *server file* plus env.  This is the
"inspector-style" config: you point it at a tiny Python file that exposes a
`server` object and let the SDK launch it.

> **SDK 1.29 reality check (verified with `uv run mcp --help` and
> `uv run mcp dev --help`):** the base `mcp` package does **not** install the
> CLI — `mcp --help` prints `Error: typer is required. Install with
> 'pip install mcp[cli]'`.  Install the extra, e.g.
> `uv run --with 'mcp[cli]' mcp --help`.  The 1.29 CLI exposes exactly:
> `version`, `dev`, `run`, `install`.  There is **no** `mcp inspect`
> subcommand in 1.29; the Inspector is reached through **`mcp dev`** (which
> shells out to `npx @modelcontextprotocol/inspector`).

The `mcp dev` / `mcp run` commands expect a Python file whose **module**
exposes an attribute named `server` (an `mcp.server.fastmcp.FastMCP`
instance).  The repository ships exactly that file at
[`scripts/mcp_dev_server.py`](../scripts/mcp_dev_server.py):

```python
from cmdb_mcp_adapter.config import load_settings
from cmdb_mcp_adapter.server.app import create_app

server = create_app(load_settings())
```

Because it builds the server through the **same** `create_app` composition
root, the Inspector talks to the identical object the `cmdb-mcp stdio`
command does.  Run the Inspector (mock, no credentials):

```text
# needs the CLI extra + Node/npm for the Inspector UI
MCP_ADAPTER=mock uv run --with 'mcp[cli]' \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    mcp dev scripts/mcp_dev_server.py
```

`mcp dev` then runs `npx @modelcontextprotocol/inspector <uv ... mcp run
scripts/mcp_dev_server.py>` and opens the Inspector web UI.  A plain
non-interactive launch of the same file is:

```text
MCP_ADAPTER=mock uv run --with 'mcp[cli]' mcp run scripts/mcp_dev_server.py
```

(Verified: with `MCP_ADAPTER=mock` and stdin closed, `mcp run` exits 0 with
0 bytes on stdout — same protocol discipline as `cmdb-mcp stdio`.)

`mcp install` is a convenience that writes the equivalent `mcpServers` entry
into Claude Desktop's config for you; use it the same way as §2.2.

### 2.4 `MCP_ADAPTER` (env) vs. adapter selection

Adapter selection is **environment-only**: the `MCP_ADAPTER` variable picks
which registered adapter (`mock` or `veops`) the composition root builds.

* There is **no `--adapter` CLI flag** in this build.  `cmdb-mcp stdio --help`
  shows only `-h/--help`; the `stdio` subcommand intentionally exposes no
  host/port/adapter flags (it only speaks on stdio).  If you want to override
  the adapter, set `MCP_ADAPTER` in the host's `env` block — do **not** add a
  flag to the launch command.
* Precedence is `CLI > env > default`, but for the adapter only the **env**
  channel exists, so `MCP_ADAPTER` is authoritative.  The default is
  `veops` (see the env table in
  [`configuration-security-observability.md`](configuration-security-observability.md));
  set `MCP_ADAPTER=mock` for credential-free development.

---

## 3. Stdout discipline (protocol-only stdout)

The stdio transport has a hard rule, enforced by the launcher and by
regression tests:

* **`stdout` carries only MCP protocol frames** (JSON-RPC / JSONRPC 2.0, one
  line each).  The MCP host's protocol reader is attached to stdout; any
  stray byte would corrupt the stream.
* **All diagnostics — structured JSON logs, configuration errors, retry
  events, shutdown notices — go to `stderr`.**
* On a **configuration failure the process exits non-zero *before* writing
  anything to stdout**, so stdout is exactly **0 bytes** (verified: see the
  "Missing credentials" test in
  [`tests/unit/test_cli.py`](../tests/unit/test_cli.py) and
  [`tests/integration/test_fail_closed.py`](../tests/integration/test_fail_closed.py)).

Regression coverage (all run in CI, no credentials, no network):

* `tests/unit/test_cli.py::TestStdioLauncher` — stdio starts with the mock
  adapter; unknown adapter fails with empty stdout.
* `tests/unit/test_cli.py::TestGracefulShutdown` — SIGINT / SIGTERM exits
  keep stdout empty; SIGINT exits 130.
* `tests/integration/test_fail_closed.py` — non-loopback-without-token and
  bad-upstream-scheme both fail closed with empty stdout on both transports.
* `tests/integration/test_secret_scan.py` — no live secret in the repo;
  config never reads files.

Because stdout is byte-for-byte protocol, you can safely redirect a running
stdio server's stdout to a file and it will contain nothing but valid
JSON-RPC lines.

---

## 4. Inspector / debugging

Two independent ways to debug a running adapter:

### 4.1 MCP Inspector (GUI) — `mcp dev`

Preferred for interactive tool exploration.  See §2.3 for the exact command.
Requires the `mcp[cli]` extra plus Node.js/npm (the Inspector UI is an
`npx`-served web app).  Use `MCP_ADAPTER=mock` for a fully local,
credential-free session; use `veops` + real env to debug against a live
CMDB.

### 4.2 Manual JSON-RPC over stdio (no Node, no GUI)

For CI / headless debugging you can speak raw JSON-RPC to the process.  A
minimal "does it come up and list tools" handshake:

```text
MCP_ADAPTER=mock uv run python - <<'PY'
import json, os, subprocess, sys, time
env = {**os.environ, "MCP_ADAPTER": "mock"}
p = subprocess.Popen([sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, env=env)
p.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize",
  "params":{"protocolVersion":"2025-06-18","capabilities":{},
            "clientInfo":{"name":"smoke","version":"0"}}})+"\n"); p.stdin.flush()
# read the initialize response
line = p.stdout.readline()
print("init:", json.loads(line)["result"]["serverInfo"])
p.stdin.write(json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"})+"\n"); p.stdin.flush()
p.stdin.write(json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list"})+"\n"); p.stdin.flush()
print("tools:", [t["name"] for t in json.loads(p.stdout.readline())["result"]["tools"]])
p.kill()
PY
```

Verified output (this repo, mock adapter):

```text
init: {'name': 'cmdb-mcp-adapter', 'version': '1.29.0'}
tools: ['get_models','get_instances','create_instance','update_instance',
        'delete_instance','search_instances','get_relations']
```

> **Process-lifetime note (verified):** the stdio server does **not** have an
> idle timeout.  It stays alive as long as the host keeps `stdin` open, even
> with no traffic.  It exits `0` when the host closes `stdin` (clean EOF) or
> sends SIGINT/SIGTERM.  A host therefore "stops" the server by closing
> stdin or terminating the process — there is no in-band shutdown message.

---

## 5. Mock mode (`MCP_ADAPTER=mock`)

`MCP_ADAPTER=mock` selects the in-memory
[`MockAdapter`](../src/cmdb_mcp_adapter/adapters/mock.py): a deterministic,
offline dataset that implements the full core protocol.  It makes no network
calls and needs no credentials.  Use it for development, demos, contract
testing, and Inspector sessions.

Mock data shape — two models, matching the normalized contract:

| Model | Fields | Seed instances |
|-------|--------|----------------|
| `host` | `hostname` (req), `ip` (req), `os`, `role` (enum: web/db/cache) | 8 hosts: `host-1` … `host-8` (`web-<n>.example.com`, `10.0.x.y`) |
| `service` | `name` (req), `env` (enum: dev/prod) | 2 services: `svc-orders`, `svc-billing` |

Relations: `host-1 →(runs)→ svc-orders`, `host-2 →(runs)→ svc-billing`.
All seven tools work against the mock; `get_instances` is cursor-paginated
(`page_size` default 50, max 1000).  The mock also supports error injection
and per-operation disablement for the contract test suite, but a plain
`MCP_ADAPTER=mock` run uses the clean dataset above.

Credential-free Inspector session:

```text
MCP_ADAPTER=mock uv run --with 'mcp[cli]' mcp dev scripts/mcp_dev_server.py
```

---

## 6. Opt-in real-CMDB live test

A live check against a **real** VeOps 2.6.2 deployment exists but is
**off by default** and never runs in CI:

* File:
  [`tests/integration/test_veops_live.py`](../tests/integration/test_veops_live.py)
* Gate: the pytest **`optin`** mark **and** `skipif MCP_VEOPS_LIVE != "1"`.
  With `MCP_VEOPS_LIVE` unset (the normal case) the test **skips**
  cleanly, so the standard `uv run pytest -q` suite stays green with zero
  credentials.
* To run it, supply real credentials **only through the environment**
  (never committed, never on the command line):

```text
MCP_VEOPS_LIVE=1 \
VEOPS_BASE_URL=http://veops.example.com:8000 \
VEOPS_KEY=<your-veops-key> \
VEOPS_SECRET=<your-veops-secret> \
uv run --no-sync pytest -q tests/integration/test_veops_live.py
```

The test is **read-only** (`GET` only): it calls `list_models()`
(asserting the deployment's `quick_reference` and `credential` CI types
are present), `get_instances("quick_reference")` (asserting the Chinese
content fields are non-empty), and
`search_instances("credential", "ACC-0001")` (asserting the expected
hit).  Nothing in this repository contains a real credential; the
secret-scan test keeps it that way.

---

## 7. Exit codes, signals, startup failure, timeouts, missing credentials

### 7.1 Exit codes (stdio process)

| Situation | Exit code | stdout | stderr |
|-----------|-----------|--------|--------|
| Normal operation, host closes stdin (clean EOF) | `0` | protocol frames only | structured JSON logs |
| Configuration error at startup (bad adapter name, missing/invalid `VEOPS_*`, non-loopback without token, bad `VEOPS_BASE_URL` scheme) | `1` | **0 bytes** | one line `[stdio] configuration error: <category>: <msg>` |
| Other core/startup error (e.g. adapter factory fails) | `1` | **0 bytes** | one line `[stdio] <category>: <msg>` or `[stdio] failed to start: <err>` |
| `SIGINT` (Ctrl-C) | `130` (shell convention) **or** `-2` (killed-by-signal, as `subprocess` reports) | **0 bytes** | whatever the SDK's default handler emits (none in practice) |
| `SIGTERM` | clean shutdown; non-zero or `0` depending on where the loop is | **0 bytes** | structured logs; stderr flushed before exit |
| No subcommand given (`cmdb-mcp` with no args) | `2` | usage text on **stdout** | — (diagnostic-only mode, not a protocol process) |
| `--version` / `-V` | `0` | `cmdb-mcp <version>` | — |
| `--help` | `0` | usage | — |

Notes:

* The **`2`** and **`0`** diagnostic exit codes above are for the
  `--version`/`--help`/no-subcommand paths, which print to stdout because they
  are *not* protocol processes.  Once the `stdio` subcommand is entered, the
  0/1/130 rules apply and stdout is protocol-only.
* `SIGINT` is delivered to the SDK's stdio loop, which has restored the
  default `SIGINT` handler by then; the OS therefore terminates the process
  with the signal (reported as `-2` by `subprocess`), or the interpreter's
  default handler exits `130`.  Both are "the signal killed it"; the contract
  is **non-zero + 0 bytes on stdout**, not a specific number.
* `SIGTERM` sets the process-wide shutdown event; the stdio launcher flushes
  `stderr` on exit so no structured log line is lost.

### 7.2 Startup failure & missing credentials (fail closed)

The adapter **refuses to start** (exit `1`, 0-byte stdout, a `configuration`
category on stderr) when:

| Missing / invalid | stderr message (contains) |
|-------------------|----------------------------|
| `MCP_ADAPTER` names an unregistered adapter | `configuration: adapter '<name>' is not available in this build; known adapters: ...` |
| `MCP_ADAPTER=veops` with no `VEOPS_BASE_URL` | `configuration: VEOPS_BASE_URL is required for the veops adapter ...` |
| `MCP_ADAPTER=veops` with `VEOPS_BASE_URL` set but no `VEOPS_KEY`/`VEOPS_SECRET` | `configuration: VEOPS_KEY and VEOPS_SECRET are required for the veops adapter ...` |
| legacy `VEOPS_TOKEN` set with the new pair missing | `configuration: VEOPS_TOKEN is not used by adapter version 0.2.0: ... set VEOPS_KEY and VEOPS_SECRET instead ...` |
| `VEOPS_BASE_URL` not an `http(s)` URL | `configuration: Invalid VEOPS_BASE_URL value: ... (must be an http(s) URL)` |
| non-loopback bind with no `MCP_AUTH_TOKEN` (http path; stdio ignores bind) | `configuration: binding to non-loopback ... requires MCP_AUTH_TOKEN ...` |

Verified live (this repo):

```text
$ MCP_ADAPTER=veops uv run cmdb-mcp stdio </dev/null
[stdio] configuration error: configuration: VEOPS_BASE_URL is required for the veops adapter (an http(s) base URL for the CMDB endpoint)
# exit code 1, stdout = 0 bytes
```

### 7.3 Timeouts & retry (runtime, not startup)

Once running, upstream (adapter → CMDB) HTTP calls use a bounded timeout
(`MCP_HTTP_TIMEOUT`, default 30 s).  Only **idempotent (read)** operations are
retried (default `MCP_HTTP_MAX_RETRIES=2`), and only for the transient
categories *timeout*, *rate limit*, and *upstream-server* (5xx).  **Writes are
never retried.**  On retry exhaustion the **original error and its stable
category are re-raised unchanged** — a timeout stays a `timeout`.  A tool call
that ultimately fails is returned to the MCP client as `isError=true` with
text `<category>: <message>`; the raw upstream detail is never exposed.
(See
[`configuration-security-observability.md`](configuration-security-observability.md#timeouts-retry-and-error-mapping)
and the error-category table in
[`mcp-tool-contract.md`](mcp-tool-contract.md#error-categories).)

---

## 8. Quick-start cheat sheet

```text
# developer, mock, credential-free
uv sync
MCP_ADAPTER=mock uv run cmdb-mcp stdio --help

# developer, real CMDB (env-only credentials)
MCP_ADAPTER=veops VEOPS_BASE_URL=http://veops.example.com:8000 \
  VEOPS_KEY=$MY_VEOPS_KEY VEOPS_SECRET=$MY_VEOPS_SECRET uv run cmdb-mcp stdio

# run the Inspector (mock)
MCP_ADAPTER=mock uv run --with 'mcp[cli]' mcp dev scripts/mcp_dev_server.py

# opt-in live read-only test (real deployment, GET only)
MCP_VEOPS_LIVE=1 VEOPS_BASE_URL=http://veops.example.com:8000 \
  VEOPS_KEY=$MY_VEOPS_KEY VEOPS_SECRET=$MY_VEOPS_SECRET \
  uv run --no-sync pytest -q tests/integration/test_veops_live.py

# diagnostics (no credentials, no network)
cmdb-mcp --version
cmdb-mcp stdio --help
```
