from __future__ import annotations

import runpy
import subprocess
import sys
from importlib.metadata import requires
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github/workflows/ci.yml").read_text()
PUBLISH = (ROOT / ".github/workflows/publish.yml").read_text()
MINIMUM = ROOT / "scripts/minimum_dependencies.py"


def test_minimum_pins_derive_from_runtime_requirements() -> None:
    result = subprocess.run(
        [sys.executable, str(MINIMUM)], capture_output=True, text=True, check=True
    )
    pins = dict(pin.split("==") for pin in result.stdout.split())
    requirements = [Requirement(value) for value in requires("machinera") or []]
    assert set(pins) == {requirement.name for requirement in requirements}
    for requirement in requirements:
        lower = max(
            Version(spec.version) for spec in requirement.specifier if spec.operator == ">="
        )
        assert Version(pins[requirement.name]) == lower
        assert lower in requirement.specifier
    minimum_job = CI.split("  test-minimum:\n", 1)[1].split("  lock:\n", 1)[0]
    install = "python -m pip install $(python scripts/minimum_dependencies.py)"
    check = "python scripts/minimum_dependencies.py --check"
    assert minimum_job.index(install) < minimum_job.index(check) < minimum_job.index("pytest -q")
    assert "==" not in minimum_job


@pytest.mark.parametrize("requirement", ["sample>1", "sample>=1,!=1", "sample[extra]>=1"])
def test_minimum_pins_reject_unsupported_bounds(requirement: str) -> None:
    minimum_pins = runpy.run_path(str(MINIMUM))["minimum_pins"]
    with pytest.raises(ValueError):
        minimum_pins([requirement])


def test_minimum_pins_handle_changes_and_markers() -> None:
    minimum_pins = runpy.run_path(str(MINIMUM))["minimum_pins"]
    assert minimum_pins(["sample>=1.2.3,<2", "unused>=1; python_version < '2'"]) == [
        "sample==1.2.3"
    ]


def test_release_publishes_the_ci_artifact() -> None:
    assert CI.count("python -m build") == 1
    assert "python -m build" not in PUBLISH
    assert "upload-artifact@" not in PUBLISH
    assert CI.count("upload-artifact@") == 1
    assert "name: ci-distributions" in CI.split("  build:\n", 1)[1]
    assert "name: ci-distributions" in CI.split("  wheel-import:\n", 1)[1]
    assert PUBLISH.count("download-artifact@") == 3
    assert PUBLISH.count("name: ci-distributions") == 3
    verification = PUBLISH.split("  verify-release:\n", 1)[1].split("  publish:\n", 1)[0]
    assert "needs: ci" in verification
    assert "Require the tagged commit to be on main" in verification
    assert 'python scripts/check_release.py "$RELEASE_TAG"' in verification
    assert "needs: verify-release" in PUBLISH.split("  publish:\n", 1)[1]
    assert "needs: [verify-release, publish]" in PUBLISH.split("  release:\n", 1)[1]
    assert "environment:\n      name: pypi" in PUBLISH


def test_build_uses_locked_tools() -> None:
    build = CI.split("  build:\n", 1)[1].split("  wheel-import:\n", 1)[0]
    assert "uv sync --locked --group dev" in build
    assert "uv run python -m build" in build
    assert "uv run twine check dist/*" in build
    assert "build==" not in CI + PUBLISH and "twine==" not in CI + PUBLISH
