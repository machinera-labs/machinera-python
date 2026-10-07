# Contributing

Report bugs and request features in the
[public issue tracker](https://github.com/machinera-labs/machinera-python/issues).
Include the SDK and Python versions, expected behavior, and a minimal reproduction
with synthetic inputs. Remove credentials, source URLs, audio, transcripts, and
other private data. Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Development

Fork the [public repository](https://github.com/machinera-labs/machinera-python),
clone your fork, and create a branch for the change. See
[`.python-version`](.python-version) for the development Python version,
[`ci.yml`](.github/workflows/ci.yml) for what CI runs, and
[`publish.yml`](.github/workflows/publish.yml) for when it runs before publication.
Run `uv run ruff check . && uv run ruff format --check . &&
uv run mypy --strict src && uv run pytest -q` locally before asking for review.

```sh
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pytest -q
uv run python -m build
uv run twine check dist/*
```

`uv sync --locked --group dev` installs the committed development lock, as CI does.
With pip 25.1 or later, `python -m pip install -e . --group dev` installs the same
tools without the lock. After intentionally changing dependencies, regenerate the
lock with `uv lock`. Runtime dependency ranges live in `pyproject.toml`; the
development lock does not constrain users. CI also installs the lowest supported
runtime versions with pip to check those ranges independently of the lock.
`scripts/minimum_dependencies.py` derives exact pins from the installed package
metadata and verifies the installed versions before that suite runs.

`src/machinera/_contract.py` is generated from the numeric public API snapshot in
`scripts/public_contract.json`; do not edit it by hand. Refresh that snapshot from
the published registry, retaining only numeric descriptors and public behavior
sets, then run `uv run python scripts/generate_contract.py`. CI checks regeneration
and the upload fixture generated from `tests/fixtures/upload_grant_schema.json`,
a copy of the published OpenAPI upload grant schema, including field semantics.
Use `--snapshot <public-snapshot.json> --upload-schema <upload-schema.json>` to
refresh both inputs; add `--check` to compare published inputs without writing.
The generator renders `tests/fixtures/upload_grant.json` from that schema.
Keep only public numeric descriptors, behavior sets and constants in the snapshot. CI also runs
`uv run python scripts/check_release.py --source-only`. Exact `machinera==X.Y.Z`
install pins in `README.md`, `api.md`, and `examples/` are forbidden; use
`pip install machinera` instead. The previous released version
(the next version heading below the current version in `CHANGELOG.md`) must not
appear elsewhere in the shipped source tree. Local environments, caches, and
build artifacts are excluded. Historical references in `CHANGELOG.md` are exempt.
The copy guard
uses fingerprinted terms in `scripts/public_terms.json` to avoid publishing the
restricted vocabulary itself; its exact exceptions cover public wire fields and
standard Python/HTTP library APIs.

Use type hints, keep strict mypy clean, and format with Ruff (100 columns).
Keep Python 3.10 compatibility. Add focused offline tests for behavior changes,
using `httpx.MockTransport` and the helpers in `tests/`. Do not require real API
credentials or send network requests in tests. Preserve exact transcript text and
caller file ownership. Add comments only to explain a non-obvious reason; keep
public API docstrings short. End text files with one newline and no trailing spaces.

## Pull requests

Open a pull request against `main` in the public repository. Describe the problem,
observable behavior, and validation performed. Keep changes focused and update
examples, `api.md`, and the relevant category under `Unreleased` in `CHANGELOG.md`
when behavior changes. Maintainers may integrate contributions through their own
process, so a contribution may appear in a later release without a direct merge of
the original pull request.

## Release maintenance

Review `api.md` against the exported API at every release (tests check that every
export has an entry) and check examples on the supported Python matrix. Use
semantic versions and move completed changelog entries into a dated release
section when preparing a release.

Run `uv run python scripts/bump_version.py X.Y.Z` to set a newer package version
and insert an empty changelog release section
below `Unreleased`. The script then runs the source release checks. If references
remain, it exits non-zero and leaves its edits in place for review. Update remaining
references, refresh `uv.lock` with `uv lock`, and rerun
`uv run python scripts/check_release.py --source-only` before building the release.

The tag workflow runs only in `machinera-labs/machinera-python`. Maintainers must
configure PyPI trusted publishing for `publish.yml` and the GitHub environment
`pypi`, with required reviewers, before publishing. A `v` tag must match the package,
wheel, and source metadata versions. The workflow builds once, publishes the checked
artifacts after environment approval, then attaches them to a GitHub Release.
