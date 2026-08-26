#!/usr/bin/env bash
#
# Build the cmdb-mcp-adapter container image.
#
# Usage:
#   ./scripts/build-docker.sh [options] [TAG ...]
#
# Options:
#   --platform <p>         target platform for `docker build`
#                          (e.g. linux/amd64, linux/arm64); repeatable
#   --build-arg K=V        extra build arg; repeatable
#   --index-url <url>      override UV_INDEX_URL (default: Alibaba PyPI mirror,
#                          matching the registry recorded in uv.lock)
#   --base-image <img>     override BASE_IMAGE (default: python:3.12-slim,
#                          or $DOCKER_BASE_IMAGE if set)
#   -h | --help            show this help
#
# Tags:
#   Positional arguments are image tags (name[:tag]).  With none given the
#   defaults are: cmdb-mcp-adapter:<version from pyproject.toml> and
#   cmdb-mcp-adapter:latest.
#
# Environment:
#   DOCKER_BASE_IMAGE      same as --base-image
#   UV_INDEX_URL           same as --index-url
#
set -euo pipefail

cd "$(dirname "$0")/.."

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

PLATFORMS=()
BUILD_ARGS=()
TAGS=()

INDEX_URL="${UV_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
BASE_IMAGE="${DOCKER_BASE_IMAGE:-python:3.12-slim}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)
      [[ $# -ge 2 ]] || { echo "error: --platform needs a value" >&2; exit 2; }
      PLATFORMS+=("$2"); shift 2 ;;
    --build-arg)
      [[ $# -ge 2 ]] || { echo "error: --build-arg needs K=V" >&2; exit 2; }
      BUILD_ARGS+=("$2"); shift 2 ;;
    --index-url)
      [[ $# -ge 2 ]] || { echo "error: --index-url needs a value" >&2; exit 2; }
      INDEX_URL="$2"; shift 2 ;;
    --base-image)
      [[ $# -ge 2 ]] || { echo "error: --base-image needs a value" >&2; exit 2; }
      BASE_IMAGE="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    -*)
      echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      TAGS+=("$1"); shift ;;
  esac
done

# Version for the default tag and the OCI version label: read from
# pyproject.toml so the image label always matches the project version.
VERSION="$(grep -m1 '^version' pyproject.toml | sed -E 's/^version *= *"([^"]+)".*/\1/')"
[[ -n "$VERSION" ]] || { echo "error: could not read version from pyproject.toml" >&2; exit 1; }

if [[ ${#TAGS[@]} -eq 0 ]]; then
  TAGS=("cmdb-mcp-adapter:${VERSION}" "cmdb-mcp-adapter:latest")
fi

# SOURCE_URL defaults to git remote origin, when one is configured.
SOURCE_URL="${SOURCE_URL:-$(git remote get-url origin 2>/dev/null || true)}"

CMD=(docker build --build-arg "UV_INDEX_URL=${INDEX_URL}"
       --build-arg "BASE_IMAGE=${BASE_IMAGE}"
       --build-arg "VERSION=${VERSION}"
       --build-arg "SOURCE_URL=${SOURCE_URL}")
for p in "${PLATFORMS[@]}"; do CMD+=(--platform "$p"); done
for a in "${BUILD_ARGS[@]}"; do CMD+=(--build-arg "$a"); done
for t in "${TAGS[@]}"; do CMD+=(-t "$t"); done
CMD+=(.)

echo "==> ${CMD[*]}"
"${CMD[@]}"

echo
echo "Build complete. Run it with docker compose:"
echo
echo "  export MCP_AUTH_TOKEN='...' VEOPS_BASE_URL='https://cmdb.example.com/api' VEOPS_TOKEN='...'"
echo "  docker compose up -d"
echo "  curl -fsS http://localhost:8000/healthz"
echo
echo "…or directly:"
echo
echo "  docker run -d --name cmdb-mcp-adapter -p 8000:8000 \\"
echo "    -e MCP_BIND_HOST=0.0.0.0 -e MCP_PORT=8000 -e MCP_ADAPTER=veops \\"
echo "    -e MCP_AUTH_TOKEN='\$MCP_AUTH_TOKEN' \\"
echo "    -e VEOPS_BASE_URL='\$VEOPS_BASE_URL' -e VEOPS_TOKEN='\$VEOPS_TOKEN' \\"
echo "    ${TAGS[0]}"
