"""Checks over the repository itself, not the integration.

This repo is public, so an address that resolves to somebody's real gear does
not belong in it — not in a fixture, not in a docs example.
"""

from __future__ import annotations

import os
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


#: Mail domains an example or fixture may use. Anything else is somebody's inbox.
ALLOWED_MAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")

#: Home directories an example may name. A real username is not one of them.
PLACEHOLDER_HOMES = {"you", "user", "username", "example"}

HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")


def _private_terms() -> list[str]:
    """Terms from PRIVATE_TERMS (a CI secret) and a local, gitignored .private-terms.

    The list is secret, so a finding names its index, never the term.
    """
    raw = os.environ.get("PRIVATE_TERMS", "")
    local = Path(__file__).resolve().parent.parent / ".private-terms"
    if local.is_file():
        raw += "\n" + local.read_text(encoding="utf-8")
    return [line.strip().lower() for line in raw.splitlines() if line.strip()]


def find_offences(text: str, terms: list[str]) -> list[str]:
    """Every private address, personal email, real home path or private term in text."""
    found: list[str] = []
    for address in sorted(set(PRIVATE_ADDRESS.findall(text)) - {PLACEHOLDER_HOST}):
        found.append(f"address {address}")
    for match in EMAIL.finditer(text):
        if match.group(1).lower() not in ALLOWED_MAIL_DOMAINS:
            found.append(f"email {match.group(0)}")
    for match in HOME_PATH.finditer(text):
        if match.group(1) not in PLACEHOLDER_HOMES:
            found.append(f"home path {match.group(0)}")
    lowered = text.lower()
    for index, term in enumerate(terms, start=1):
        if term in lowered:
            found.append(f"private term #{index}")
    return found


def test_the_detectors_fire() -> None:
    """Each rule catches what it is for; assembled at runtime so this file stays clean."""
    address = "192.168." + "7.9"
    email = "someone" + "@" + "real-church.org"
    home = "/Us" + "ers/realname"
    allowed = "ok" + "@" + "example.com"
    planted = "\n".join([address, email, allowed, home + "/x", "/Users/you/x", "Some Ministry Name"])
    assert find_offences(planted, ["some ministry name"]) == [
        "address " + address,
        "email " + email,
        "home path " + home,
        "private term #1",
    ]


def test_no_personal_detail_appears_anywhere() -> None:
    """No personal email, home path or private term ships in a public repo."""
    terms = _private_terms()
    if not terms:
        print("[hygiene] PRIVATE_TERMS not set; checking generic rules only")
    files = _repo_files()
    assert len(files) > 10, "the walk found almost nothing; it is not walking the repo"

    offenders: list[str] = []
    for path in files:
        if path.name == ".private-terms":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders.extend(f"{path.name}: {offence}" for offence in find_offences(text, terms))

    assert sorted(offenders) == []
