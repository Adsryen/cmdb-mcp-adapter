"""Documentation validation (runs in the normal CI suite).

This is the executable layer of the docs-validation job: it proves the
user-facing documentation (README.md, CHANGELOG.md, docs/*.md) has not
drifted from the runtime:

* every *relative* markdown link in the docs resolves (file + GitHub
  style anchor);
* ``cmdb-mcp --version`` matches ``__version__`` in
  ``src/cmdb_mcp_adapter/__init__.py`` **and** the ``version`` field in
  ``pyproject.toml``;
* the CLI surface documented in the README is real: ``cmdb-mcp``
  (no args → exit 2 + usage), ``--help``, ``stdio --help``, and
  ``http --help`` all behave as documented;
* the README quickstart stdio JSON example is valid JSON, names the
  ``stdio`` subcommand, and carries no placeholder leak on the
  credential rule (env-only credentials);
* a **mock stdio smoke flow**: spawn ``cmdb-mcp stdio`` with
  ``MCP_ADAPTER=mock`` and drive a raw JSON-RPC
  initialize → tools/list handshake, asserting the frozen stable tool
  names;
* an **HTTP health smoke**: start ``cmdb-mcp http`` with the mock
  adapter on a free loopback port and GET ``/healthz`` / ``/readyz``,
  expecting ``200 {"status":"ok"}``;
* a **secret scan over the docs**: the repo's locked fake token
  (defined in ``tests/integration/test_secret_scan.py``) must not
  appear in any markdown file, and the docs must not contain obvious
  real-secret shapes (``sk-…``, ``Bearer <32+ char token>``).

Stdlib only (plus the in-repo project) so the test runs with no extra
dependencies.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DOC_FILES: tuple[Path, ...] = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "CHANGELOG.md",
) + tuple(sorted((REPO_ROOT / "docs").glob("*.md")))

#: The frozen MCP tool names (the stable public contract: seven
#: primitives plus the four intent-level cmdb_* tools).
STABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_models",
        "get_instances",
        "create_instance",
        "update_instance",
        "delete_instance",
        "search_instances",
        "get_relations",
        "cmdb_preflight_write",
        "cmdb_create_card",
        "cmdb_update_card",
        "cmdb_link_relations",
    }
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------


def _github_anchor(heading: str) -> str:
    """GitHub's anchor algorithm (lowercase, punctuation stripped,
    spaces → dashes, `.` and `` ` `` removed)."""
    slug = heading.lower().replace("`", "").strip()
    slug = slug.replace("(", "").replace(")", "")
    slug = re.sub(r"[^\w\s\-]", "", slug, flags=re.UNICODE)
    return slug.replace(" ", "-")


def _document_headings(md: str) -> set[str]:
    """Set of GitHub anchors reachable in one markdown document."""
    anchors: set[str] = set()
    for line in md.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            anchors.add(_github_anchor(m.group(1)))
    return anchors


def _inline_links(md: str) -> list[tuple[int, str]]:
    """Yield (line_no, target) for every inline [text](target) link.

    Code fences are excluded so shell/JSON examples are not treated as
    links.
    """
    links: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(md.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in re.finditer(r"\[[^\]]*\]\(([^)#\s]+)(#[^)\s]*)?\)", line):
            links.append((lineno, m.group(1)))
    return links


def test_doc_files_exist() -> None:
    for path in DOC_FILES:
        assert path.is_file(), f"missing docs file: {path.relative_to(REPO_ROOT)}"


@pytest.mark.parametrize(
    "doc_path",
    DOC_FILES,
    ids=[p.relative_to(REPO_ROOT).as_posix() for p in DOC_FILES],
)
def test_relative_links_resolve(doc_path: Path) -> None:
    md = _read(doc_path)
    broken: list[str] = []
    for lineno, target in _inline_links(md):
        if target.startswith(("http://", "https://", "mailto:")):
            continue  # external links are not verified offline
        path_part, _, frag = target.partition("#")
        base = doc_path.parent / (path_part or ".")
        if not base.is_file():
            broken.append(f"line {lineno}: file not found: {target}")
            continue
        if frag:
            target_md = base.read_text(encoding="utf-8")
            if frag not in _document_headings(target_md):
                broken.append(f"line {lineno}: anchor not found: {target}")
    assert not broken, "broken relative links:\n" + "\n".join(broken)


# --------------------------------------------------------------------------
# version + CLI drift
# --------------------------------------------------------------------------


def _run_cli(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cmdb_mcp_adapter", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "VEOPS_"))},
    )


def test_version_matches_sources() -> None:
    init_src = _read(REPO_ROOT / "src" / "cmdb_mcp_adapter" / "__init__.py")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_src, re.MULTILINE)
    assert m, "__version__ not found in src/cmdb_mcp_adapter/__init__.py"
    src_version = m.group(1)

    pyproject = _read(REPO_ROOT / "pyproject.toml")
    m2 = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject, re.MULTILINE)
    assert m2, "version not found in pyproject.toml"
    assert src_version == m2.group(1), (
        f"__version__ {src_version!r} != pyproject version {m2.group(1)!r}"
    )

    result = _run_cli(["--version"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"cmdb-mcp {src_version}", result.stdout


def test_cli_help_surface() -> None:
    noargs = _run_cli([])
    assert noargs.returncode == 2, "no-subcommand invocation must exit 2 (usage mode)"
    assert "stdio" in noargs.stdout and "http" in noargs.stdout, noargs.stdout

    for args in (["--help"], ["stdio", "--help"], ["http", "--help"]):
        result = _run_cli(args)
        assert result.returncode == 0, f"{' '.join(args)}: {result.stderr}"
    assert "--host" in _run_cli(["http", "--help"]).stdout

    readme = _read(REPO_ROOT / "README.md")
    for documented in ("cmdb-mcp http", "cmdb-mcp stdio", "cmdb-mcp --version"):
        assert documented in readme


def test_readme_stdio_example_is_valid_json() -> None:
    readme = _read(REPO_ROOT / "README.md")
    blocks = re.findall(r"```json\s*\n(.*?)```", readme, re.DOTALL)
    assert blocks, "README must contain at least one fenced JSON example"
    examples = [json.loads(b) for b in blocks]
    hosts = [ex for ex in examples if "mcpServers" in ex]
    assert hosts, "README must include an MCP host (mcpServers) configuration example"
    cmdb = hosts[0]["mcpServers"]["cmdb"]
    assert cmdb["args"] == ["stdio"], "stdio example must launch the stdio subcommand"
    assert "VEOPS_KEY" in cmdb.get("env", {})
    # credentials must be placeholders, never real-looking values
    for value in cmdb.get("env", {}).values():
        assert not re.fullmatch(r"[A-Za-z0-9_-]{32,}", value), f"env value looks real: {value!r}"


# --------------------------------------------------------------------------
# secret scan over the docs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_path",
    DOC_FILES,
    ids=[p.relative_to(REPO_ROOT).as_posix() for p in DOC_FILES],
)
def test_docs_contain_no_secrets(doc_path: Path) -> None:
    from tests.integration.test_secret_scan import FAKE_REPO_TOKEN

    text = _read(doc_path)
    assert FAKE_REPO_TOKEN not in text, "locked fake token leaked into the docs"
    # obvious real-secret shapes (OpenAI/Anthropic/GitHub keys)
    assert not re.search(r"\b(sk|pk|ghp|gho|xox[baprs])-[A-Za-z0-9_-]{16,}\b", text)
    # a Bearer token with a 32+ char high-entropy value
    assert not re.search(r"Bearer\s+[A-Za-z0-9._-]{32,}", text)


# --------------------------------------------------------------------------
# mock stdio smoke flow (real subprocess, raw JSON-RPC)
# --------------------------------------------------------------------------


def _stdio_smoke(timeout: float = 30.0) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "VEOPS_"))}
    env["MCP_ADAPTER"] = "mock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cmdb_mcp_adapter", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None

        def send(payload: dict[str, object]) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        def recv() -> dict[str, object]:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            assert line, "stdio process closed stdout prematurely"
            return json.loads(line)

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "docs-smoke", "version": "0"},
                },
            }
        )
        init = recv()
        assert init.get("id") == 1 and "result" in init, init
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv()
        assert tools.get("id") == 2 and "result" in tools, tools
        names = [t["name"] for t in tools["result"]["tools"]]  # type: ignore[index]
        return names
    finally:
        proc.kill()
        proc.wait(timeout=15)


def test_mock_stdio_smoke_lists_stable_tools() -> None:
    names = _stdio_smoke()
    assert set(names) == STABLE_TOOL_NAMES, f"unexpected tool list: {names}"


# --------------------------------------------------------------------------
# HTTP health smoke (real subprocess on a free loopback port)
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mock_http_health_and_readiness() -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "VEOPS_"))}
    env.update({"MCP_ADAPTER": "mock", "MCP_PORT": str(port)})
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cmdb_mcp_adapter",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 30.0
        ok = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1
                ) as resp:
                    if resp.status == 200:
                        ok = True
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        if not ok:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"server did not become healthy; stderr: {err}")

        for path in ("/healthz", "/readyz"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                assert resp.status == 200, f"{path} returned {resp.status}"
                body = json.loads(resp.read().decode("utf-8"))
            assert body == {"status": "ok"}, f"{path} unexpected body: {body}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
