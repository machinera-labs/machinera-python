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

`src/machinera/_contract.py` is generated from the service's API contract; do not
edit it by hand.

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

The tag workflow runs only in `machinera-labs/machinera-python`. Maintainers must
configure PyPI trusted publishing for `publish.yml` and the GitHub environment
`pypi`, with required reviewers, before publishing. A `v` tag must match the package,
wheel, and source metadata versions. The workflow builds once, publishes the checked
artifacts after environment approval, then attaches them to a GitHub Release.
