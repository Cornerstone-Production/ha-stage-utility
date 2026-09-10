#!/usr/bin/env python3
"""The conventional-commit rule, in ONE place.

Ported from stage-utility's scripts/check-commit-subjects.mjs. CI enforces
this and nothing runs it locally, so the workflow calls this rather than
carrying its own copy of the pattern — two copies of a rule is how the copies
drift, and a lint rule that disagrees with the CI enforcing it is worse than
no lint rule.

    python scripts/check_commit_subjects.py [base]

`base` defaults to origin/beta, which is what a branch here is normally cut
from. Exits non-zero and names every subject that does not conform.
"""

from __future__ import annotations

import re
import subprocess
import sys

#: type(scope)!: subject — scope is lower-case and takes no spaces or commas.
SUBJECT_PATTERN = re.compile(r"^(feat|fix|perf|refactor|docs|test|build|ci|chore|revert)(\([a-z0-9.+/-]+\))?!?: .+")

#: `git revert` writes `Revert "<original subject>"`.
#:
#: Accepted as-is deliberately: the only way to fix such a commit once pushed
#: is to rewrite the branch, and beta and main are never force-pushed. The
#: generated form is unambiguous.
REVERT_PATTERN = re.compile(r'^Revert ".+"')

HELP_TEXT = """
One or more commit subjects do not follow the convention.

  type(scope): subject

type must be one of:
  feat fix perf refactor docs test build ci chore revert

scope is optional, lower-case, and takes NO spaces or commas —
"fix(editor, resi):" is the shape that keeps failing; use one scope,
or none at all.

Add "!" after the type/scope for a breaking change. Subject is
imperative mood, no trailing period.

See docs/contributing.md. Reword with:
  git rebase -i {base}"""


def subject_is_valid(subject: str) -> bool:
    return bool(SUBJECT_PATTERN.match(subject)) or bool(REVERT_PATTERN.match(subject))


def _commit_subjects(base: str) -> list[str] | None:
    try:
        # --no-merges: GitHub authors merge commits and they carry no release
        # meaning.
        out = subprocess.run(
            ["git", "log", "--no-merges", "--format=%s", f"{base}..HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    return [line for line in out.split("\n") if line]


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/beta"
    subjects = _commit_subjects(base)
    if subjects is None:
        print(f"Could not read commits for {base}..HEAD — is {base} fetched?", file=sys.stderr)
        return 1

    if not subjects:
        print("No non-merge commits to check.")
        return 0

    bad = 0
    for subject in subjects:
        if subject_is_valid(subject):
            print(f"  ok   {subject}")
        else:
            print(f"  BAD  {subject}")
            bad += 1

    if bad > 0:
        print(HELP_TEXT.format(base=base), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
