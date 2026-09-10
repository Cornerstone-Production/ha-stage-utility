#!/usr/bin/env python3
"""The notes attached to a GitHub release.

Ported from stage-utility's scripts/release-notes.mjs, trimmed to what this
integration needs: no scope-label table (this repo has none), no bundled
install script (HACS is the only supported install path, and README.md
covers it), and no scope-overlap "build-out fix" heuristic — this repo is
small enough that `Beta-only: true` alone is a reasonable signal.

    python scripts/release_notes.py <version> [from-ref]

`from-ref` is the previous STABLE release for a stable release, and the
previous tag for a prerelease — the caller decides, because that is the same
anchor question the version calculation in release.yml answers.
"""

from __future__ import annotations

import re
import subprocess
import sys

#: Commit types that change nothing an operator could notice.
INVISIBLE = {"chore", "ci", "build", "docs", "test", "refactor", "style"}

#: `type(scope)!: subject`
CONVENTIONAL = re.compile(r"^([a-zA-Z]+)(?:\(([^)]*)\))?(!)?:\s*(.+)$")

#: `Beta-only: true` — this fixed a bug that never reached a stable release.
#: Only the author knows, so only the author can say. Prereleases keep such a
#: fix in the notes regardless: someone on the beta track has been running the
#: broken version.
BETA_ONLY = re.compile(r"^Beta-only:\s*(true|yes)\s*$", re.IGNORECASE | re.MULTILINE)

#: How many bullets a section may carry before the rest are summarised.
CAP = 12


def _commits(commit_range: str) -> list[tuple[str, str]]:
    """Subject and body for each commit in the range, oldest concerns first.

    Separated by ASCII record/unit separators, because a commit body contains
    blank lines, bullet lists and code fences, and every cheaper separator has
    appeared inside one.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--no-merges", "--format=%s%x00%b%x1e", commit_range],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    records = []
    for rec in out.split("\x1e"):
        subject, _, body = rec.partition("\x00")
        subject = subject.strip()
        if subject:
            records.append((subject, body))
    return records


def _section(title: str, items: list[str]) -> str:
    if not items:
        return ""
    shown = [f"- {s}" for s in items[:CAP]]
    rest = len(items) - CAP
    if rest > 0:
        shown.append(f"- …and {rest} more")
    return f"## {title}\n\n" + "\n".join(shown) + "\n"


def build_notes(version: str, from_ref: str | None) -> str:
    is_prerelease = "-" in version
    commit_range = f"{from_ref}..v{version}" if from_ref else f"v{version}"
    entries = _commits(commit_range)

    features: list[str] = []
    fixes: list[str] = []
    breaking: list[str] = []
    beta_only_fixes = 0
    seen: set[str] = set()

    for subject, body in entries:
        m = CONVENTIONAL.match(subject)
        if not m:
            continue
        raw_type, scope, bang, text = m.groups()
        commit_type = raw_type.lower()
        if commit_type in INVISIBLE and not bang:
            continue

        line = f"**{scope}** — {text}" if scope else text
        if line in seen:
            continue
        seen.add(line)

        if bang:
            breaking.append(line)
        elif commit_type == "feat":
            features.append(line)
        elif commit_type in ("fix", "perf"):
            if not is_prerelease and BETA_ONLY.search(body):
                beta_only_fixes += 1
            else:
                fixes.append(line)

    build_out_note = ""
    if beta_only_fixes:
        plural = beta_only_fixes != 1
        build_out_note = (
            f"{beta_only_fixes} further fix{'es' if plural else ''} "
            f"{'were' if plural else 'was'} never in a released version and "
            f"{'are' if plural else 'is'} not listed.\n"
        )

    fixed = ""
    if fixes:
        fixed = f"{_section('Fixed', fixes)}\n{build_out_note}"
    elif build_out_note:
        fixed = f"## Fixed\n\n{build_out_note}"

    parts = [
        _section("Breaking", breaking) if breaking else "",
        _section("New", features),
        fixed,
    ]
    body_text = "\n".join(p for p in parts if p).strip()
    return body_text if body_text else "Maintenance release — no user-facing changes."


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: release_notes.py <version> [from-ref]", file=sys.stderr)
        return 1
    version = argv[1]
    from_ref = argv[2] if len(argv) > 2 else None
    print(build_notes(version, from_ref))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
