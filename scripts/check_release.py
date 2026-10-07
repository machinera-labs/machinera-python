from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import tarfile
import zipfile
from email.parser import BytesParser
from functools import lru_cache
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "src" / "machinera" / "_version.py"


ROOT = VERSION_FILE.parents[2]
INSTALL_PIN = re.compile(r"\bmachinera==([0-9]+\.[0-9]+\.[0-9]+)\b")
RELEASE_HEADING = re.compile(r"^## ([0-9]+\.[0-9]+\.[0-9]+)(?:[ \t].*)?$", re.MULTILINE)
TERM_POLICY = Path(__file__).with_name("public_terms.json")


@lru_cache(maxsize=1)
def term_policy() -> tuple[re.Pattern[str], dict[int, set[str]], set[str]]:
    policy = json.loads(TERM_POLICY.read_text())
    allowed = policy["allowed_identifiers"]
    exceptions = re.compile(
        r"(?<![0-9A-Za-z_])(?:"
        + "|".join(re.escape(value) for value in sorted(allowed, key=len, reverse=True))
        + r")(?![0-9A-Za-z_])"
    )
    fingerprints: dict[int, set[str]] = {}
    for length, fingerprint in policy["terms"]:
        fingerprints.setdefault(length, set()).add(fingerprint)
    return exceptions, fingerprints, set(policy.get("retired_code_fingerprints", []))


def public_copy_hits(text: str) -> bool:
    """Match vendored term fingerprints without publishing restricted vocabulary."""
    exceptions, fingerprints, retired = term_policy()
    for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        if hashlib.sha256(identifier.encode()).hexdigest() in retired:
            return True
    for raw, content in ((False, exceptions.sub(" ", text)), (True, text)):
        tokens = re.findall(r"[0-9A-Za-z]+", content)
        plain = [token.lower() for token in tokens]
        camel = [
            part.lower()
            for token in tokens
            for part in re.split(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", token)
        ]
        for length, values in fingerprints.items():
            if raw and length == 1:
                continue
            for stream in (plain, camel):
                for offset in range(len(stream) - length + 1):
                    value = " ".join(stream[offset : offset + length])
                    if hashlib.sha256(value.encode()).hexdigest() in values:
                        return True
    return False


def check_numeric_codes(source: str) -> None:
    """Reject string error codes and annotations in Python package sources."""
    tree = ast.parse(source)
    for item in ast.walk(tree):
        candidates: list[ast.AST] = []
        if isinstance(item, ast.keyword) and item.arg == "code":
            candidates.append(item.value)
        elif isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
            if item.func.id == "ErrorCode" and item.args:
                candidates.append(item.args[0])
        elif isinstance(item, ast.Dict):
            candidates.extend(
                value
                for key, value in zip(item.keys, item.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "code"
            )
        elif isinstance(item, ast.Assign) and any(
            (isinstance(target, ast.Name) and target.id == "code")
            or (isinstance(target, ast.Attribute) and target.attr == "code")
            for target in item.targets
        ):
            candidates.append(item.value)
        elif isinstance(item, ast.Compare) and (
            isinstance(item.left, ast.Attribute)
            and item.left.attr == "code"
            or isinstance(item.left, ast.Name)
            and item.left.id == "code"
        ):
            candidates.extend(item.comparators)
        annotation = None
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if item.target.id == "code":
                annotation = item.annotation
                if item.value is not None:
                    candidates.append(item.value)
        if isinstance(item, ast.arg) and item.arg == "code":
            annotation = item.annotation
        if annotation is not None and re.search(r"\b(?:str|StrictStr)\b", ast.unparse(annotation)):
            raise ValueError("Error code annotation must be numeric")
        if any(
            isinstance(value, ast.Constant) and isinstance(value.value, str) for value in candidates
        ):
            raise ValueError("String error code in package source")


def install_pin_paths(root: Path) -> list[Path]:
    paths = [root / "README.md", root / "api.md"]
    paths.extend(path for path in (root / "examples").rglob("*") if path.is_file())
    return paths


def check_install_pins(root: Path) -> None:
    for path in install_pin_paths(root):
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if INSTALL_PIN.search(line):
                raise ValueError(
                    f"Exact install pin forbidden: {path.relative_to(root)}:{number}: "
                    "use pip install machinera"
                )


def shipped_paths(root: Path) -> list[Path]:
    # Exclude local environments and generated artifacts, which are not shipped.
    excluded = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
        "htmlcov",
    }
    paths = []
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(
            name for name in folders if name not in excluded and not name.endswith(".egg-info")
        )
        paths.extend(
            Path(directory) / name
            for name in sorted(files)
            if name not in {".git", ".env", ".coverage", ".DS_Store"}
            and not name.endswith((".pyc", ".pyo"))
        )
    return paths


def check_previous_version(root: Path) -> None:
    version = package_version(root)
    changelog = root / "CHANGELOG.md"
    releases = RELEASE_HEADING.findall(changelog.read_text(encoding="utf-8"))
    if version not in releases:
        raise ValueError(f"Missing CHANGELOG heading for package version {version}")
    previous = releases[releases.index(version) + 1 :]
    if not previous:
        return
    needle = re.compile(r"(?<![0-9.])" + re.escape(previous[0]) + r"(?![0-9])")
    for path in shipped_paths(root):
        if path == changelog:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if needle.search(line):
                raise ValueError(
                    f"Previous release version {previous[0]}: {path.relative_to(root)}:{number}"
                )


def check_public_sources(root: Path = ROOT) -> None:
    check_install_pins(root)
    check_previous_version(root)
    paths = [root / name for name in ("README.md", "api.md", "CHANGELOG.md")]
    for name in ("src/machinera", "examples", "tests"):
        paths.extend(
            path for path in (root / name).rglob("*") if path.suffix in (".py", ".md", ".json")
        )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".py" and root / "src" in path.parents:
            check_numeric_codes(source)
        for number, line in enumerate(source.splitlines(), 1):
            if public_copy_hits(line):
                raise ValueError(f"Public-copy check failed: {path.relative_to(root)}:{number}")


def package_version(root: Path = ROOT) -> str:
    version_file = root / "src" / "machinera" / "_version.py"
    for element in ast.parse(version_file.read_text(encoding="utf-8")).body:
        if (
            isinstance(element, ast.Assign)
            and [getattr(target, "id", None) for target in element.targets] == ["__version__"]
            and isinstance(element.value, ast.Constant)
            and isinstance(element.value.value, str)
        ):
            return element.value.value
    raise ValueError("Package version not found")


def check_release(tag: str, directory: Path) -> None:
    check_public_sources()
    __version__ = package_version()
    if tag != f"v{__version__}":
        raise ValueError("Release tag does not match the package version")
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Expected exactly one wheel metadata file")
        wheel_metadata = wheel.read(names[0])
    with tarfile.open(sources[0]) as source:
        members = [
            member
            for member in source.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        if len(members) != 1:
            raise ValueError("Expected exactly one source metadata file")
        stream = source.extractfile(members[0])
        if stream is None:
            raise ValueError("Source metadata must be a regular file")
        with stream:
            source_metadata = stream.read()
    for content in (wheel_metadata, source_metadata):
        metadata = BytesParser().parsebytes(content)
        if metadata["Name"] != "machinera" or metadata["Version"] != __version__:
            raise ValueError("Distribution metadata does not match the package name and version")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a release tag against both distributions.")
    parser.add_argument("tag", nargs="?")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.source_only:
        check_public_sources()
    elif args.tag is None:
        parser.error("tag is required unless --source-only is set")
    else:
        check_release(args.tag, args.dist)


if __name__ == "__main__":
    main()
