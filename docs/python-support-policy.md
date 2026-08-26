# Python Support and Dependency Policy

## Supported Python versions

- **Minimum**: Python 3.10 (`requires-python = ">=3.10"` in `pyproject.toml`).
- **Primary supported versions**: 3.11 and 3.12.
- **CI matrix**: 3.10, 3.11, 3.12 (`.github/workflows/ci.yml`).

Releases must keep working on the declared minimum. Code must not use
syntax or standard-library features introduced after Python 3.10 without an
explicit version gate; `mypy` runs with `python_version = 3.10` to enforce
this.

Raising the minimum version is a breaking change: it requires a major or
minor version bump, an update of the support matrix in this document and in
`pyproject.toml`, and a release note.

## Dependency management

- Project metadata, dependencies, and the `cmdb-mcp` console script are
  declared in `pyproject.toml` (PEP 621).
- The lockfile is `uv.lock` and is committed; reproduce a development
  environment with `uv sync --locked` from the repository root.
- The lockfile records the Alibaba PyPI mirror
  (`https://mirrors.aliyun.com/pypi/simple/`) as its registry because
  pypi.org is unreachable from the development environment. Any `uv`
  invocation against this lockfile (sync, lock, export) must use the same
  mirror, e.g. `uv sync --locked --index-url
  https://mirrors.aliyun.com/pypi/simple/`; CI sets `UV_INDEX_URL` to the
  same URL. Regenerating the lockfile against pypi.org requires an
  environment with direct pypi.org access.
- Runtime dependencies: the official MCP Python SDK (`mcp`) and `uvicorn`.
- Dev-only dependencies (pytest, ruff, mypy) live in the `dev`
  dependency group and are never installed into production environments.

## MCP SDK upgrade policy

- The SDK is pinned to a compatible release range
  (`mcp>=1.29.0,<2.0.0`); the exact resolved version lives in `uv.lock`.
- Every SDK upgrade must, in order:
  1. Read the SDK release notes for breaking or deprecated behaviour.
  2. Pass the transport tests (`tests/integration/test_transports.py` and
     transport smoke tests once the real servers land).
  3. Pass the MCP tool contract tests once `unified-mcp-core` exists.
  4. Pass `uv run ruff check .`, `uv run mypy src`, and `uv run pytest`.
- Deprecated SDK APIs must be removed within one release after deprecation;
  removals are listed in release notes.

## Rollback policy

If an SDK upgrade breaks transport or contract tests, roll back by
restoring the previous pin in `pyproject.toml` and the previous `uv.lock`
commit, then re-running `uv sync --locked`. Do not widen the minimum
Python version as part of a rollback.

## Transport contract

Both `stdio` and `http` launchers obtain the shared MCP server object only
through `cmdb_mcp_adapter.server.app.create_app`; transports must not
construct vendor clients or call vendor endpoints. This boundary is
verified by `tests/integration/test_transports.py`.
