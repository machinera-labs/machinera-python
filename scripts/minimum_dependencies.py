from __future__ import annotations

import argparse
from importlib.metadata import requires, version

from packaging.requirements import Requirement
from packaging.version import Version


def minimum_pins(dependencies: list[str]) -> list[str]:
    pins = []
    for dependency in dependencies:
        requirement = Requirement(dependency)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        lower = [Version(spec.version) for spec in requirement.specifier if spec.operator == ">="]
        if not lower or requirement.url or requirement.extras:
            raise ValueError(f"Expected an inclusive runtime lower bound: {requirement}")
        minimum = max(lower)
        if minimum not in requirement.specifier:
            raise ValueError(f"Lower bound is excluded: {requirement}")
        pins.append(f"{requirement.name}=={minimum}")
    return sorted(pins)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pin installed SDK runtime requirements at minima."
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    pins = minimum_pins(requires("machinera") or [])
    if not pins:
        raise ValueError("No runtime requirements found")
    if args.check:
        for pin in pins:
            name, minimum = pin.split("==")
            if Version(version(name)) != Version(minimum):
                raise ValueError(f"Expected {pin}, installed {version(name)}")
    else:
        print(" ".join(pins))


if __name__ == "__main__":
    main()
