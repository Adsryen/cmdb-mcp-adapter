# Docker Deployment

This document is the operator runbook for running the CMDB MCP adapter as
a containerized `cmdb-mcp http` (Streamable HTTP) service.  It covers
prerequisites, building the image, running it with docker compose,
configuration, the single-replica limitation, TLS, updates, and security
notes.  The adapter itself is unchanged: the container simply wraps the
existing CLI entry point, so everything documented in
[remote-http.md](remote-http.md) and
[configuration-security-observability.md](configuration-security-observability.md)
applies verbatim.

## Contents

1. [Prerequisites](#prerequisites)
2. [Building the image](#building-the-image)
3. [Running with docker compose](#running-with-docker-compose)
4. [Configuration](#configuration)
5. [Single-replica limitation](#single-replica-limitation)
6. [TLS and reverse proxies](#tls-and-reverse-proxies)
7. [Updating the image](#updating-the-image)
8. [Security notes](#security-notes)

## Prerequisites

* A Docker daemon with the `docker` CLI (v23+) and the compose plugin
  (`docker compose` subcommand).
* Network access to a registry mirror for the **base image**
  (default `python:3.12-slim`) and to a **PyPI index**
  (default `https://mirrors.aliyun.com/pypi/simple/`).  `pypi.org` is
  not used by default because the committed `uv.lock` records the
  Alibaba mirror as its package registry; if you can reach `pypi.org`
  and your lockfile was built against it, pass a different
  `--index-url` instead.
* The four required environment variables (see
  [Configuration](#configuration)): `MCP_AUTH_TOKEN`, `VEOPS_BASE_URL`,
  `VEOPS_KEY`, `VEOPS_SECRET`.  They must come from your shell
  environment or a git-ignored `.env` file in the compose project
  directory — compose fails fast if any is missing (same fail-closed
  posture as the adapter).

## Building the image

The recommended entry point is the build script, which reads the version
from `pyproject.toml`, tags the image accordingly, and passes the mirror
build-args:

```bash
./scripts/build-docker.sh                      # tags: cmdb-mcp-adapter:<version> and :latest
./scripts/build-docker.sh my-registry:1234/img # explicit tags
./scripts/build-docker.sh --platform linux/arm64
./scripts/build-docker.sh --index-url https://pypi.org/simple/
./scripts/build-docker.sh --base-image my-registry.example.com/library/python:3.12-slim
```

Environment overrides (same effect as the flags): `UV_INDEX_URL`,
`DOCKER_BASE_IMAGE`, `SOURCE_URL` (OCI source label).  Any extra
`--build-arg K=V` is passed straight through to `docker build`.

Equivalent raw invocation:

```bash
docker build \
  --build-arg UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  --build-arg BASE_IMAGE=python:3.12-slim \
  --build-arg VERSION=0.2.0 \
  -t cmdb-mcp-adapter:0.2.0 -t cmdb-mcp-adapter:latest .
```

### Image layout

* **Two stages.**  The build stage installs `uv` and the project (plus
  its locked dependencies) into `/opt/venv`; the runtime stage copies
  only that venv into a fresh copy of the same base image.
* **Non-root.**  The runtime runs as the unprivileged, `nologin`
  `cmdb-mcp` user.
* **Healthcheck.**  Uses the Python standard library (`urllib.request`)
  to GET `http://127.0.0.1:${MCP_PORT:-8000}/healthz` — no `curl` or
  other runtime dependency is added.  The probe always targets loopback,
  so it works regardless of the configured bind host.
* **Entrypoint.**  `cmdb-mcp http` with no `--host`/`--port`: binding,
  port, adapter choice, and credentials are entirely environment-driven.
  A non-loopback bind without `MCP_AUTH_TOKEN` fails closed at startup
  (the container exits with a non-zero code), matching the
  documented behavior in [remote-http.md](remote-http.md).
* **Labels.**  `org.opencontainers.image.title/version/source/…` are
  injected from the `VERSION`/`SOURCE_URL` build-args.

## Running with docker compose

```bash
export MCP_AUTH_TOKEN='change-me-inbound-client-token-0123456789'
export VEOPS_BASE_URL='http://veops.example.com:8000'
export VEOPS_KEY='change-me-veops-user-key-0123456789'
export VEOPS_SECRET='change-me-veops-user-secret-0123456789'

docker compose up -d
docker compose ps        # wait for "healthy"

# Liveness (no auth required):
curl -fsS http://localhost:8000/healthz
# {"status": "ok"}

# Readiness (no auth; 503 + reasons while the upstream prober is down):
curl -fsS http://localhost:8000/readyz || true

# MCP endpoint — unauthenticated requests are rejected:
curl -i http://localhost:8000/mcp -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
# 401 …
```

Or as a one-shot `docker run`:

```bash
docker run -d --name cmdb-mcp-adapter -p 8000:8000 \
  -e MCP_BIND_HOST=0.0.0.0 -e MCP_PORT=8000 -e MCP_ADAPTER=veops \
  -e MCP_AUTH_TOKEN="$MCP_AUTH_TOKEN" \
  -e VEOPS_BASE_URL="$VEOPS_BASE_URL" \
  -e VEOPS_KEY="$VEOPS_KEY" -e VEOPS_SECRET="$VEOPS_SECRET" \
  cmdb-mcp-adapter:latest
```

The compose file sets `restart: unless-stopped` and a healthcheck (the
same probe as the image's).  `docker compose config` can be used to
validate the file without starting anything.

## Configuration

Configuration is exactly the standard two-domain environment contract
described in
[configuration-security-observability.md](configuration-security-observability.md);
`[.env.example](../.env.example)` lists every variable with
placeholders.  Precedence is unchanged: CLI flags > env vars > safe
defaults (the container entrypoint passes no flags).

| Variable           | In compose             | Notes                                                                                          |
| ------------------ | ---------------------- | ---------------------------------------------------------------------------------------------- |
| `MCP_BIND_HOST`    | `0.0.0.0` (fixed)      | Non-loopback → `MCP_AUTH_TOKEN` is **required** (fail closed).                                  |
| `MCP_PORT`         | `8000` (fixed)         | Also the port the healthcheck probes.                                                          |
| `MCP_ADAPTER`      | `veops`                | Switch to `mock` and drop the `VEOPS_*` lines for an in-process adapter.                        |
| `MCP_AUTH_TOKEN`   | `${MCP_AUTH_TOKEN:?…}` | Bearer token authenticating MCP **clients**.  Required; compose fails fast if unset.            |
| `VEOPS_BASE_URL`   | `${VEOPS_BASE_URL:?…}` | VeOps CMDB base URL; required by the `veops` adapter (fail closed).                             |
| `VEOPS_KEY`        | `${VEOPS_KEY:?…}`      | VeOps user key (`_key` query parameter); required by the `veops` adapter (fail closed).         |
| `VEOPS_SECRET`     | `${VEOPS_SECRET:?…}`   | VeOps user secret (per-request signing material; never logged); required by the `veops` adapter (fail closed). |
| `MCP_HTTP_TIMEOUT`, `MCP_HTTP_MAX_RETRIES`, `MCP_LOG_LEVEL` | not set (defaults) | Optional; add to the compose `environment:` block to override. |

The `${VAR:?…}` syntax means compose aborts before creating the
container when a variable is missing — deployment cannot silently fall
back to an insecure or half-configured state.

## Single-replica limitation

The streamable-HTTP session state lives **in process memory** (see
[remote-http.md](remote-http.md)).  Therefore:

* Run **exactly one** adapter container per set of MCP clients.  The
  compose file defines a single service on purpose.
* If you need high availability or scaling, put a reverse proxy with
  **sticky sessions** in front first; a true multi-replica deployment
  requires moving the session store out of process memory (an
  external store such as Redis) and is a separate, future task.

## TLS and reverse proxies

The adapter always speaks **plain HTTP** on an internal socket.  TLS is
terminated by a reverse proxy or load balancer in front of the
container (nginx, haproxy, a cloud LB, `cloudflared`, …).  Point the
proxy at the container's `8000` port and use `/healthz` for liveness
and `/readyz` for readiness gating; pass the client's `Authorization`
header through untouched so the adapter's bearer-token check keeps
working.  Never bind the port to the public internet directly.

## Updating the image

1. Check out the new version (or fetch the new image).
2. Rebuild: `./scripts/build-docker.sh` (tags update automatically from
   `pyproject.toml`; the `:latest` alias moves with it).
3. Recreate: `docker compose up -d`.

**Note:** recreating the container discards in-process MCP session
state; existing client sessions must reconnect after the update
(sessions are per-process by design — see
[single-replica limitation](#single-replica-limitation)).  The
in-flight shutdown is graceful: the process handles SIGTERM and exits
cleanly, so `docker compose up -d` (or `docker stop`) is sufficient.

## Security notes

* **Secrets never live in the image or in git.**  `MCP_AUTH_TOKEN` and
  `VEOPS_KEY` / `VEOPS_SECRET` are injected at runtime from the
  environment or a git-ignored `.env`.  The build context excludes
  `.env` and `.git` (see `.dockerignore`).
* **Token rotation:** change the value in `.env`/the environment and
  `docker compose up -d` to recreate the process with the new value.
  Rotation of `MCP_AUTH_TOKEN` invalidates existing sessions
  (in-process state, as above).
* **Non-root runtime:** the container runs as the unprivileged
  `cmdb-mcp` user with no `sudo`/`cap_add`; the image adds no
  non-standard packages (no curl, no shell tools) and no package
  installation happens at container start.
* **Image hygiene:** scan the built image (e.g. `trivy image
  cmdb-mcp-adapter:latest` or your registry's scanner) and pin base
  image versions when you need a stable build baseline; the default
  `python:3.12-slim` tag floats.
* **Do not expose the port publicly** — see
  [remote-http.md](remote-http.md).  Binding to `0.0.0.0` is for a
  trusted internal network only.
