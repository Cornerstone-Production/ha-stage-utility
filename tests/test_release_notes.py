"""Cover the release-notes grouping logic directly, without a git history."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "release_notes.py"
_spec = importlib.util.spec_from_file_location("release_notes", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
release_notes = importlib.util.module_from_spec(_spec)
sys.modules["release_notes"] = release_notes
_spec.loader.exec_module(release_notes)


def _with_commits(commits: list[tuple[str, str]]):
    return patch.object(release_notes, "_commits", return_value=commits)


def test_groups_feat_and_fix_into_new_and_fixed() -> None:
    with _with_commits(
        [
            ("feat(cues): add a cue-token rotation flow", ""),
            ("fix(coordinator): reconnect after a dropped stream", ""),
            ("docs: note the new flow", ""),
        ]
    ):
        notes = release_notes.build_notes("1.1.0", "v1.0.0")
    assert "## New" in notes
    assert "add a cue-token rotation flow" in notes
    assert "## Fixed" in notes
    assert "reconnect after a dropped stream" in notes
    assert "note the new flow" not in notes  # docs is invisible


def test_breaking_change_gets_its_own_section() -> None:
    with _with_commits([("feat(config-flow)!: require a cue token on setup", "BREAKING CHANGE: old entries break")]):
        notes = release_notes.build_notes("2.0.0", "v1.0.0")
    assert "## Breaking" in notes
    assert "require a cue token on setup" in notes


def test_beta_only_fix_excluded_from_stable_release_but_counted() -> None:
    with _with_commits([("fix(config-flow): the setup guide link was wrong", "Beta-only: true")]):
        notes = release_notes.build_notes("1.1.0", "v1.0.0")
    assert "the setup guide link was wrong" not in notes
    assert "1 further fix" in notes


def test_beta_only_fix_kept_in_a_prerelease() -> None:
    with _with_commits([("fix(config-flow): the setup guide link was wrong", "Beta-only: true")]):
        notes = release_notes.build_notes("1.1.0-beta.1", "v1.0.0-beta.1")
    assert "the setup guide link was wrong" in notes


def test_no_visible_commits_reads_as_a_maintenance_release() -> None:
    with _with_commits([("chore: bump a dependency", ""), ("ci: pin an action", "")]):
        notes = release_notes.build_notes("1.0.1", "v1.0.0")
    assert notes == "Maintenance release — no user-facing changes."


def test_duplicate_lines_are_not_repeated() -> None:
    with _with_commits(
        [
            ("feat(cues): add a cue-token rotation flow", ""),
            ("feat(cues): add a cue-token rotation flow", ""),
        ]
    ):
        notes = release_notes.build_notes("1.1.0", "v1.0.0")
    assert notes.count("add a cue-token rotation flow") == 1
