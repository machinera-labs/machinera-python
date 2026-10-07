from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from machinera import __version__

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release.py"


@pytest.fixture
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def distributions(path: Path, wheel_version: str, source_version: str) -> None:
    with zipfile.ZipFile(path / "machinera.whl", "w") as wheel:
        wheel.writestr(
            "machinera.dist-info/METADATA", f"Name: machinera\nVersion: {wheel_version}\n"
        )
    metadata = f"Name: machinera\nVersion: {source_version}\n".encode()
    with tarfile.open(path / "machinera.tar.gz", "w:gz") as source:
        entry = tarfile.TarInfo("machinera/PKG-INFO")
        entry.size = len(metadata)
        source.addfile(entry, io.BytesIO(metadata))


def test_release_versions_match(checker: ModuleType, tmp_path: Path) -> None:
    distributions(tmp_path, __version__, __version__)
    checker.check_release(f"v{__version__}", tmp_path)


@pytest.mark.parametrize("mismatch", ["tag", "wheel", "source"])
def test_release_rejects_version_mismatch(
    checker: ModuleType, tmp_path: Path, mismatch: str
) -> None:
    distributions(
        tmp_path,
        "0.0.0" if mismatch == "wheel" else __version__,
        "0.0.0" if mismatch == "source" else __version__,
    )
    with pytest.raises(ValueError, match="does not match"):
        checker.check_release("v0.0.0" if mismatch == "tag" else f"v{__version__}", tmp_path)


def test_release_requires_both_artifacts(checker: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one wheel"):
        checker.check_release(f"v{__version__}", tmp_path)


def test_release_check_runs_without_the_package_installed(tmp_path: Path) -> None:
    isolated = [sys.executable, "-I", "-S"]
    tracker = subprocess.run([*isolated, "-c", "import machinera"], cwd=tmp_path, check=False)
    assert tracker.returncode != 0
    distributions(tmp_path, __version__, __version__)
    for tag, expected in ((f"v{__version__}", 0), ("v0.0.0", 1)):
        result = subprocess.run(
            [*isolated, str(SCRIPT), tag, "--dist", str(tmp_path)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stderr


def test_public_sources_pass(checker: ModuleType) -> None:
    checker.check_public_sources()


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    for name in ("README.md", "api.md", "CHANGELOG.md"):
        (tmp_path / name).write_text("", encoding="utf-8")
    version_file = tmp_path / "src" / "machinera" / "_version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(f'__version__ = "{__version__}"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(f"## {__version__}\n", encoding="utf-8")
    (tmp_path / "examples" / "nested").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize(
    "name", ["README.md", "api.md", "examples/nested/demo.py", "examples/requirements.txt"]
)
@pytest.mark.parametrize("pin", [None, "0.0.0", __version__])
def test_install_instructions_require_unpinned_package(
    checker: ModuleType, source_tree: Path, name: str, pin: str | None
) -> None:
    requirement = "machinera" if pin is None else f"machinera=={pin}"
    (source_tree / name).write_text(
        f'# Install\n# pip install "{requirement}"\n',
        encoding="utf-8",
    )
    if pin is not None:
        with pytest.raises(ValueError, match="Exact install pin forbidden") as caught:
            checker.check_public_sources(source_tree)
        assert f"{name}:2:" in str(caught.value)
        assert "use pip install machinera" in str(caught.value)
    else:
        checker.check_public_sources(source_tree)


def test_install_pins_ignore_changelog(checker: ModuleType, source_tree: Path) -> None:
    (source_tree / "CHANGELOG.md").write_text(
        f"## {__version__}\n\n## 0.0.0\npip install machinera==0.0.0\n", encoding="utf-8"
    )
    checker.check_public_sources(source_tree)


@pytest.mark.parametrize(
    "source",
    [
        "code: str | None = None",
        'def f(code: "str"): pass',
        'error(code="1234")',
        'ErrorCode("1234", 400, "api_error", False)',
        'data = {"code": "1234"}',
        'code = "1234"',
        'error.code = "1234"',
        'assert error.code == "1234"',
    ],
)
def test_release_rejects_string_codes(checker: ModuleType, source: str) -> None:
    with pytest.raises(ValueError, match="code"):
        checker.check_numeric_codes(source)


def test_numeric_guard_accepts_public_types(checker: ModuleType) -> None:
    checker.check_numeric_codes("code: int | None = None\nerror(code=1234)")


@pytest.mark.parametrize("separator", ["_", "-", ".", "/", "", " "])
def test_copy_guard_checks_identifier_parts(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, separator: str
) -> None:
    import hashlib
    import json

    policy = tmp_path / "terms.json"
    policy.write_text(
        json.dumps(
            {
                "terms": [[1, hashlib.sha256(b"restricted").hexdigest()]],
                "allowed_identifiers": {"allowedRestrictedField": "Public field."},
            }
        )
    )
    monkeypatch.setattr(checker, "TERM_POLICY", policy)
    checker.term_policy.cache_clear()
    assert checker.public_copy_hits(f"before{separator}Restricted{separator}After")
    assert checker.public_copy_hits('"restricted"')
    assert not checker.public_copy_hits("allowedRestrictedField")
    assert checker.public_copy_hits("allowedRestrictedFieldExtra")


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "api.md",
        "examples/nested/data.txt",
        "scripts/tool.py",
        ".github/workflows/test.yml",
        "uv.lock",
    ],
)
def test_previous_version_elsewhere_fails(
    checker: ModuleType, source_tree: Path, name: str
) -> None:
    (source_tree / "CHANGELOG.md").write_text(
        f"## {__version__}\n\n## 8.7.6 — 2025-01-01\n\n## 8.7.5\n", encoding="utf-8"
    )
    target = source_tree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Reference\n# 8.7.6\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Previous release version 8.7.6") as caught:
        checker.check_public_sources(source_tree)
    assert f"{name}:2" in str(caught.value)


@pytest.mark.parametrize("previous_parts", [(0, 2, 0), (0, 2, 1)])
@pytest.mark.parametrize("reference", ["10.2.0", "v10.2.0", "0.2.10", "bare", "tag"])
def test_previous_version_matches_complete_tokens(
    checker: ModuleType, source_tree: Path, previous_parts: tuple[int, int, int], reference: str
) -> None:
    previous = ".".join(map(str, previous_parts))
    current = "9.0.0"
    (source_tree / "src/machinera/_version.py").write_text(
        f'__version__ = "{current}"\n', encoding="utf-8"
    )
    (source_tree / "CHANGELOG.md").write_text(f"## {current}\n\n## {previous}\n", encoding="utf-8")
    text = previous if reference == "bare" else f"v{previous}" if reference == "tag" else reference
    (source_tree / "README.md").write_text(f"Reference: {text}\n", encoding="utf-8")
    if reference in ("bare", "tag"):
        with pytest.raises(ValueError, match="Previous release version"):
            checker.check_public_sources(source_tree)
    else:
        checker.check_public_sources(source_tree)


def test_previous_version_ignores_history_and_local_artifacts(
    checker: ModuleType, source_tree: Path
) -> None:
    (source_tree / "CHANGELOG.md").write_text(
        f"## {__version__}\n\n## 8.7.6\nHistorical 8.7.6\n\n## 8.7.5\n", encoding="utf-8"
    )
    (source_tree / "README.md").write_text("Older release 8.7.5\n", encoding="utf-8")
    for directory in (".git", ".venv", "dist", "__pycache__"):
        (source_tree / directory).mkdir()
        (source_tree / directory / "local").write_text("8.7.6", encoding="utf-8")
    checker.check_public_sources(source_tree)


@pytest.fixture
def bump_tree(source_tree: Path) -> Path:
    import shutil

    scripts = source_tree / "scripts"
    scripts.mkdir()
    for name in ("bump_version.py", "check_release.py", "public_terms.json"):
        shutil.copyfile(SCRIPT.with_name(name), scripts / name)
    version_file = source_tree / "src" / "machinera" / "_version.py"
    version_file.write_text('__version__ = "8.7.6"\n', encoding="utf-8")
    (source_tree / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Pending change.\n\n## 8.7.6\n\nHistory.\n",
        encoding="utf-8",
    )
    for name in ("README.md", "api.md", "examples/nested/install.txt"):
        (source_tree / name).write_text("pip install machinera\n", encoding="utf-8")
    return source_tree


def run_bump(root: Path, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "bump_version.py"), version],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("versions", [((8, 7, 6), (8, 8, 0)), ((0, 2, 1), (0, 2, 10))])
def test_bump_version_updates_sources_and_checks(
    bump_tree: Path, versions: tuple[tuple[int, int, int], tuple[int, int, int]]
) -> None:
    previous, current = (".".join(map(str, parts)) for parts in versions)
    for name in (
        "src/machinera/_version.py",
        "CHANGELOG.md",
    ):
        path = bump_tree / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("8.7.6", previous), encoding="utf-8"
        )
    history = (bump_tree / "CHANGELOG.md").read_text(encoding="utf-8")
    result = run_bump(bump_tree, current)
    assert result.returncode == 0, result.stderr
    assert (bump_tree / "src/machinera/_version.py").read_text() == f'__version__ = "{current}"\n'
    for name in ("README.md", "api.md", "examples/nested/install.txt"):
        assert (bump_tree / name).read_text() == "pip install machinera\n"
    assert (bump_tree / "CHANGELOG.md").read_text() == history.replace(
        f"## {previous}", f"## {current}\n\n## {previous}"
    )


def test_bump_version_fails_on_remaining_hit(bump_tree: Path) -> None:
    (bump_tree / "api.md").write_text("SDK version 8.7.6\n", encoding="utf-8")
    result = run_bump(bump_tree, "8.8.0")
    assert result.returncode != 0
    assert "Previous release version 8.7.6: api.md:1" in result.stderr
    assert '"8.8.0"' in (bump_tree / "src/machinera/_version.py").read_text()


def test_bump_version_rejects_install_pin_without_rewriting_it(bump_tree: Path) -> None:
    readme = bump_tree / "README.md"
    content = "pip install machinera==8.8.0\n"
    readme.write_text(content, encoding="utf-8")
    result = run_bump(bump_tree, "8.8.0")
    assert result.returncode != 0
    assert "Exact install pin forbidden: README.md:1" in result.stderr
    assert readme.read_text(encoding="utf-8") == content
    assert '"8.8.0"' in (bump_tree / "src/machinera/_version.py").read_text()


@pytest.mark.parametrize("version", ["invalid", "8.8", "v8.8.0", "08.8.0", "8.7.6", "8.7.5"])
def test_bump_version_rejects_invalid_input_without_edits(bump_tree: Path, version: str) -> None:
    before = {path: path.read_bytes() for path in bump_tree.rglob("*") if path.is_file()}
    result = run_bump(bump_tree, version)
    assert result.returncode != 0
    assert all(path.read_bytes() == content for path, content in before.items())
