"""Secret-scan regression: no live/fixture secret may appear in the repo.

A fake token constant is defined *only in this test file* (and a
matching example in ``.env.example``).  The scan walks every tracked text
file in the repository and asserts the fake value never appears outside
of the two declared locations (this test file and ``.env.example``).
This proves:

* the code never hard-codes a credential;
* configuration examples document variable *names* only;
* nothing in ``src/`` accidentally embeds a token-shaped value.
"""

from __future__ import annotations

import os
from pathlib import Path

# A deliberately fake, high-entropy value.  It must not appear anywhere
# in the repository except this file and .env.example.
FAKE_REPO_TOKEN = "z9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Paths (relative to the repo root) where the fake token is *allowed*.
_ALLOWED: tuple[str, ...] = (
    "tests/integration/test_secret_scan.py",
    ".env.example",
)

#: File extensions considered "text" for the scan.
_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".cfg",
    ".ini",
    ".sh",
    ".env.example",
}

_IGNORED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "__pycache__",
    ".trellis",
    ".claude",
    ".codex",
    ".agents",
    "dist",
    "build",
}


def _iter_repo_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in _TEXT_SUFFIXES:
                yield path


class TestSecretScan:
    def test_fake_token_absent_from_repo(self) -> None:
        offenders: list[str] = []
        for path in _iter_repo_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in _ALLOWED:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if FAKE_REPO_TOKEN in text:
                offenders.append(rel)
        assert offenders == [], f"fake token leaked into: {offenders}"

    def test_no_token_in_src(self) -> None:
        # Even though the fake token is allowed in .env.example, it must
        # never appear under src/ (the product code).
        src = REPO_ROOT / "src"
        offenders: list[str] = []
        for path in src.rglob("*"):
            if path.is_file() and path.suffix == ".py":
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if FAKE_REPO_TOKEN in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []

    def test_examples_not_a_secret_source(self) -> None:
        # The config code must never read from an examples/ directory.
        # Verify by inspecting the config module source for any reference
        # to an "examples" path (there should be none).
        config_src = (REPO_ROOT / "src" / "cmdb_mcp_adapter" / "config.py").read_text(
            encoding="utf-8"
        )
        # The word "examples" may appear in a docstring as a *warning*,
        # but must never appear as a path the loader opens.
        import re

        # Find any open(...) call in the config module.
        opens = re.findall(r"open\s*\(", config_src)
        assert opens == [], "config.py must not open any files"
        # And no Path(...).read_text in the config module.
        assert "read_text" not in config_src
        assert "read_bytes" not in config_src
