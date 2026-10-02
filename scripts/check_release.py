from __future__ import annotations

import argparse
import ast
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "src" / "machinera" / "_version.py"


def package_version() -> str:
    for node in ast.parse(VERSION_FILE.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets] == ["__version__"]
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise ValueError("Package version not found")


def check_release(tag: str, directory: Path) -> None:
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
    parser.add_argument("tag")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    check_release(args.tag, args.dist)


if __name__ == "__main__":
    main()
