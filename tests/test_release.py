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
        member = tarfile.TarInfo("machinera/PKG-INFO")
        member.size = len(metadata)
        source.addfile(member, io.BytesIO(metadata))


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
    probe = subprocess.run([*isolated, "-c", "import machinera"], cwd=tmp_path, check=False)
    assert probe.returncode != 0
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
