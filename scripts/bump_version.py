from __future__ import annotations

import argparse
import re
from pathlib import Path

from check_release import (
    RELEASE_HEADING,
    ROOT,
    check_public_sources,
    package_version,
)


def bump_version(version: str, root: Path = ROOT) -> None:
    if re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version) is None:
        raise ValueError("Version must have the form X.Y.Z")
    current = package_version(root)
    if tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
        raise ValueError("Version must be newer than the package version")
    changelog = root / "CHANGELOG.md"
    history = changelog.read_text(encoding="utf-8")
    headings = list(RELEASE_HEADING.finditer(history))
    if not headings or headings[0].group(1) != current:
        raise ValueError("The latest CHANGELOG release must match the package version")
    if version in [heading.group(1) for heading in headings]:
        raise ValueError("Version already has a CHANGELOG section")
    version_file = root / "src" / "machinera" / "_version.py"
    source = version_file.read_text(encoding="utf-8")
    source, count = re.subn(
        r'(?m)^__version__ = [\'"][^\'"\n]+[\'"]$', f'__version__ = "{version}"', source
    )
    if count != 1:
        raise ValueError("Expected one __version__ assignment")
    updates = {version_file: source}
    offset = headings[0].start()
    updates[changelog] = history[:offset] + f"## {version}\n\n" + history[offset:]
    for path, content in updates.items():
        path.write_text(content, encoding="utf-8")
    check_public_sources(root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump the SDK version and check release sources.")
    parser.add_argument("version", metavar="X.Y.Z")
    args = parser.parse_args()
    try:
        bump_version(args.version)
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
