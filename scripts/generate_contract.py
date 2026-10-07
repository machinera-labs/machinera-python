"""Render the SDK contract from the numeric public registry snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = Path(__file__).with_name("public_contract.json")
TARGET = ROOT / "src" / "machinera" / "_contract.py"
FIXTURES = ROOT / "tests" / "fixtures"


def render(data: dict) -> str:
    body = """
from collections.abc import Mapping
from typing import NamedTuple


class ErrorCode(NamedTuple):
    code: int
    status: int
    type: str
    retryable: bool


ERROR_CODES: Mapping[int, ErrorCode] = {
"""
    for entry in data["errors"]:
        code = entry["public_code"]
        body += (
            f"    {code}: ErrorCode({code}, {entry['status']}, "
            f"{entry['type']!r}, {entry['retryable']}),\n"
        )
    body += "}\n\n"
    for name, test in (
        ("RETRYABLE_CODES", "entry.retryable"),
        ("TERMINAL_CODES", "not entry.retryable"),
    ):
        body += (
            f"{name}: frozenset[int] = frozenset("
            f"code for code, entry in ERROR_CODES.items() if {test})\n"
        )
    for name, values in data["behaviors"].items():
        body += f"\n{name}: frozenset[int] = frozenset({{{', '.join(map(str, values))}}})\n"
    for name, value in data["constants"].items():
        if isinstance(value, list):
            if name in ("JOB_STATUSES", "PUBLISHED_MODEL_ALIASES", "UPLOAD_GRANT_STATES"):
                annotation, literal = "tuple[str, ...]", repr(tuple(value))
            else:
                annotation, literal = (
                    "frozenset[str]",
                    f"frozenset({{{', '.join(map(repr, value))}}})",
                )
        else:
            annotation, literal = type(value).__name__, repr(value)
        body += f"\n{name}: {annotation} = {literal}\n"
    formatted = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(TARGET), "-"],
        input=body,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    body = "\n" + formatted.lstrip("\n")
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f'"""Generated service contract; do not edit."""\n# contract-sha256: {digest}\n{body}'


def render_upload_fixture(schema: dict) -> str:
    examples = {
        "upload_id": "upload-1",
        "put_url": "https://storage.example/object?signature=private-signature",
        "Content-Length": "4",
        "Content-Type": "audio/flac",
        "Content-MD5": "uTUqbVY4eWdkTfUx8Ye9RA==",
        "expires_at": 1700003600,
        "upload_deadline": 1700086400,
        "max_upload_bytes": 2147483648,
        "sync_inline_body_bytes": 100,
        "async_inline_body_bytes": 200,
        "put_ttl_seconds": 3600,
        "submit_grace_seconds": 300,
        "retention_max_seconds": 172800,
    }

    def sample(shape: dict, name: str = "") -> object:
        if shape["type"] == "object":
            selected = set(shape.get("required", [])) | {"put_url", "method", "required_headers"}
            return {
                key: sample(value, key)
                for key, value in shape["properties"].items()
                if key in selected
            }
        if "const" in shape:
            return shape["const"]
        if "enum" in shape:
            return shape["enum"][0]
        if isinstance(shape["type"], list) and "null" in shape["type"]:
            return None
        return examples[name]

    return json.dumps(sample(schema), indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=INPUT,
        help="Numeric public contract snapshot to import or check",
    )
    parser.add_argument(
        "--upload-schema",
        type=Path,
        default=FIXTURES / "upload_grant_schema.json",
        help="Published upload grant schema to import or check",
    )
    args = parser.parse_args()
    data = json.loads(args.snapshot.read_text())
    schema = json.loads(args.upload_schema.read_text())
    for target, source in (
        (INPUT, json.dumps(data, indent=2) + "\n"),
        (FIXTURES / "upload_grant_schema.json", json.dumps(schema, indent=2) + "\n"),
        (TARGET, render(data)),
        (FIXTURES / "upload_grant.json", render_upload_fixture(schema)),
    ):
        if args.check:
            if target.read_text() != source:
                raise SystemExit("Regenerate the public contract with scripts/generate_contract.py")
        else:
            target.write_text(source)


if __name__ == "__main__":
    main()
