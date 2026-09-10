"""Checks over the repository itself, not the integration.

This repo is public, so an address that resolves to somebody's real gear does
not belong in it — not in a fixture, not in a docs example.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The one address this repo may use in an example or a fixture. RFC 5737 keeps
#: no private-range placeholder, so this is a convention rather than a reserved
#: address; what matters is that it is the same one everywhere.
PLACEHOLDER_HOST = "192.168.1.50"

PRIVATE_ADDRESS = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")

SKIP_DIRS = {".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}


def _repo_files() -> list[Path]:
    """Every text file in the working tree, walked rather than globbed."""
    root = Path(__file__).resolve().parent.parent
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


def test_only_the_placeholder_lan_address_appears_anywhere() -> None:
    """No real LAN address ships in a public repo."""
    files = _repo_files()
    assert len(files) > 10, "the walk found almost nothing; it is not walking the repo"

    offenders: dict[str, list[str]] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for address in set(PRIVATE_ADDRESS.findall(text)):
            if address != PLACEHOLDER_HOST:
                offenders.setdefault(address, []).append(path.name)

    assert offenders == {}
