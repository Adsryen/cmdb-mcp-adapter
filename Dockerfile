# syntax=docker/dockerfile:1
#
# CMDB MCP adapter — multi-stage image for the `cmdb-mcp http` transport.
#
# Build args (all have defaults; scripts/build-docker.sh passes these
# explicitly):
#   BASE_IMAGE   base image for both stages (default python:3.12-slim;
#                override to point at a registry mirror, e.g.
#                my-registry.example.com/library/python:3.12-slim)
#   UV_INDEX_URL PyPI index used for every install (default: Alibaba mirror,
#                because pypi.org is unreachable from the dev environment and
#                the committed uv.lock records that mirror as its registry)
#   VERSION      image version label (default 0.1.0; the build script reads
#                it from pyproject.toml)
#   SOURCE_URL   source repository URL for the OCI label (default: empty)
#
# The runtime stage runs as the unprivileged `cmdb-mcp` user and starts
# `cmdb-mcp http`; host/port/adapter/auth are driven purely by environment
# variables (see .env.example), so nothing is hard-coded here.

ARG BASE_IMAGE=python:3.12-slim
ARG UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG VERSION=0.1.0
ARG SOURCE_URL=

# ---------------------------------------------------------------------------
# Stage 1: build — install the project into an isolated /opt/venv with uv
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS build

ARG UV_INDEX_URL
ARG VERSION

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install uv itself from the same index (pypi.org may be unreachable).
RUN pip install --no-cache-dir --index-url "${UV_INDEX_URL}" uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Create the virtualenv and install the project (plus its dependencies)
# into it.  uv.lock pins everything; the lockfile's recorded registry must
# match UV_INDEX_URL (both default to the Alibaba mirror).
RUN uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python \
      --index-url "${UV_INDEX_URL}" \
      --no-cache \
      .

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim, non-root, health-checked
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE}

ARG VERSION
ARG SOURCE_URL

LABEL org.opencontainers.image.title="cmdb-mcp-adapter" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="CMDB MCP adapter exposing the VeOps CMDB through the Model Context Protocol (Streamable HTTP transport)" \
      org.opencontainers.image.source="${SOURCE_URL}"

# The adapter is a stateless pass-through (session state aside); it needs
# no writable state, so the venv stays read-only for the process.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=build /opt/venv /opt/venv

# Non-root user with a nologin shell; the world-readable venv is enough.
RUN useradd -r -s /usr/sbin/nologin cmdb-mcp

USER cmdb-mcp

EXPOSE 8000

# Liveness probe using only the Python standard library (no curl in the
# runtime image).  The port is read from MCP_PORT at check time; we always
# probe loopback so the check works regardless of the configured bind host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["sh", "-c", "python -c 'import urllib.request, os; urllib.request.urlopen(\"http://127.0.0.1:%s/healthz\" % os.environ.get(\"MCP_PORT\", \"8000\"), timeout=3)'"]

# `cmdb-mcp http` — bind host, port, adapter, and credentials all come from
# the environment (see .env.example / docs/docker-deployment.md).  A
# non-loopback bind without MCP_AUTH_TOKEN fails closed at startup.
ENTRYPOINT ["cmdb-mcp", "http"]
