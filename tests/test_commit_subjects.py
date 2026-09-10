"""Cover the conventional-commit checker directly against the pattern it enforces.

Imports scripts/check_commit_subjects.py without going through git, so these
run without a repo history to check.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_commit_subjects.py"
_spec = importlib.util.spec_from_file_location("check_commit_subjects", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_commit_subjects = importlib.util.module_from_spec(_spec)
sys.modules["check_commit_subjects"] = check_commit_subjects
_spec.loader.exec_module(check_commit_subjects)

subject_is_valid = check_commit_subjects.subject_is_valid


@pytest.mark.parametrize(
    "subject",
    [
        "feat: add cue token rotation",
        "fix(config-flow): reject a host with no scheme",
        "perf(coordinator): skip the fallback poll while the stream is up",
        "refactor: extract the manifest parser",
        "docs: note the Beta-only trailer",
        "test: cover the fallback backoff",
        "build: bump pytest-homeassistant-custom-component",
        "ci: pin hassfest to a commit sha",
        "chore: ignore the ruff cache",
        "feat(config-flow)!: require a cue token on setup",
        'Revert "feat: add cue token rotation"',
        "feat(a.b+c-d/e): scope punctuation ruff/mjs already allows",
    ],
)
def test_valid_subjects(subject: str) -> None:
    assert subject_is_valid(subject) is True


@pytest.mark.parametrize(
    "subject",
    [
        "Add cue token rotation",  # no type
        "Fix: wrong case on the type",
        "feat(config-flow, coordinator): two scopes with a comma and a space",
        "feat(Config Flow): scope with spaces",
        "feature: not a recognised type",
        "fix:no space after the colon",
        "fix():empty scope with no space",
        "",
    ],
)
def test_invalid_subjects(subject: str) -> None:
    assert subject_is_valid(subject) is False


def test_main_reports_ok_for_no_commits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(check_commit_subjects, "_commit_subjects", lambda base: [])
    assert check_commit_subjects.main(["prog"]) == 0
    assert "No non-merge commits to check." in capsys.readouterr().out


def test_main_fails_on_a_bad_subject(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(check_commit_subjects, "_commit_subjects", lambda base: ["feat: fine", "nope not conventional"])
    assert check_commit_subjects.main(["prog"]) == 1
    out = capsys.readouterr()
    assert "  ok   feat: fine" in out.out
    assert "  BAD  nope not conventional" in out.out
    assert "do not follow the convention" in out.err


def test_main_reports_error_when_base_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_commit_subjects, "_commit_subjects", lambda base: None)
    assert check_commit_subjects.main(["prog", "origin/beta"]) == 1
    assert "Could not read commits" in capsys.readouterr().err
