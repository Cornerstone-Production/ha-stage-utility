# Contributing

## Branching

`main` ← `beta` ← feature branches.

- Feature work branches off `beta`, never off `main`.
- **Nothing is pushed directly to `beta` or `main`.** Every change — including a
  one-line fix — goes through a pull request off `beta`. Release automation is
  the exception: it commits the version bump and tag to those branches by
  design.
- **Never force-push `beta` or `main`.** HACS resolves installs against these
  branches' tags, and a rewrite breaks that.
- PRs are **merged, not squashed** — the individual commits are what the
  release tooling reads to decide the version, so the commits you write are
  the ones that count.

## Commit convention

This repo uses [Conventional Commits](https://www.conventionalcommits.org/).

```
type(scope): subject
```

- **type** — required, from the table below.
- **scope** — optional, lower-case, the area touched (`config-flow`,
  `coordinator`, `switch`, `button`, `diagnostics`, and so on). Two areas can
  be joined with `+`.
- **subject** — imperative mood, no trailing period. Say what the change does,
  not what you did: "add a state source to the diagnostics dump", not "added a
  state source".

Body is free-form and encouraged for anything non-obvious.

| Type | Meaning | Version bump |
|---|---|---|
| `feat` | A new capability a user can see | **minor** |
| `fix` | A bug fix | **patch** |
| `perf` | Faster or lighter with no behavior change | **patch** |
| `refactor` | Restructuring with no behavior change | none |
| `docs` | Documentation only | none |
| `test` | Tests only | none |
| `build` | Build system, dependencies | none |
| `ci` | CI configuration | none |
| `chore` | Anything else with no user impact | none |
| `revert` | Reverts a previous commit | matches what it reverts |

`scripts/check_commit_subjects.py` enforces the shape of the subject line in
CI (`commit-lint.yml`), against every non-merge commit in a PR. Run it
yourself before opening one:

```bash
python scripts/check_commit_subjects.py origin/beta
```

### Breaking changes

Append `!` after the type/scope, **and** explain the break in the body:

```
feat(config-flow)!: require a cue token on setup

Entries created before this release have no token stored and will need to be
re-added.
```

A `!` bumps the **major** version.

### `Beta-only: true`

Put this on a `fix:` or `perf:` commit whose bug **never reached a stable
release** — a regression introduced and repaired inside the same release
cycle. `scripts/release_notes.py` drops such a commit from a stable release's
**Fixed** list (counting it into a "further fixes were never in a released
version" line instead), because a reader upgrading from the last stable tag
never had the bug. A prerelease keeps it — somebody on the beta track has been
running the broken version, and for them the fix is the news.

## Releases

Automated by `.github/workflows/release.yml` from the commit types above.

| Push to | Produces |
|---|---|
| `beta` | a prerelease `X.Y.Z-beta.N`, tagged, published as a GitHub prerelease |
| `main` | the release `X.Y.Z`, tagged, published as the latest GitHub release |

A push containing only `docs`/`chore`/`refactor`/`test`/`ci`/`build` produces
**no release**. Otherwise the level is the highest severity among every commit
since the last stable release — one `feat` among several `docs` still makes it
a minor.

The workflow bumps `"version"` in
`custom_components/stage_utility/manifest.json`, commits and tags, then builds
`stage_utility.zip` from `custom_components/stage_utility` and attaches it to
the GitHub release — the asset HACS downloads, per `zip_release` in
`hacs.json`. Lint, the type check and the test suite all run again before
anything is tagged, so a red build cannot become a release.

## Before you open a PR

```bash
source .venv/bin/activate
ruff check .
ruff format --check .
mypy custom_components
pytest -q
```

CI (`ci.yml`) runs the same four, plus hassfest and HACS validation.

Two more questions, answered in the PR body:

- **Do the docs still describe this correctly?** A new config-flow field, a
  new refusal reason surfaced from Stage Utility, a changed default: each
  lands in the same commit as its mention in `README.md`.
- **Does it log anything?** If it can fail, retry, or silently skip work, log
  it — the debug logger under `custom_components.stage_utility` is what an
  operator turns on when something is wrong.

"No docs needed" and "nothing worth logging" are fine answers, as long as
they're a decision rather than an omission.
