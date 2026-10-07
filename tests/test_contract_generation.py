from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("changed", [None, "snapshot", "schema"])
def test_published_input_drift_check_is_read_only(
    monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    script = ROOT / "scripts/generate_contract.py"
    spec = importlib.util.spec_from_file_location("generate_contract", script)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    snapshot = json.loads((ROOT / "scripts/public_contract.json").read_text())
    schema = json.loads((ROOT / "tests/fixtures/upload_grant_schema.json").read_text())
    if changed == "snapshot":
        snapshot["errors"][0]["retryable"] = not snapshot["errors"][0]["retryable"]
    elif changed == "schema":
        schema["properties"]["submit_expires_at"]["description"] = (
            "Changed public timing semantics."
        )
    inputs = {
        Path("public-input.json"): json.dumps(snapshot),
        Path("upload-input.json"): json.dumps(schema),
    }
    original_read = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        return inputs[path] if path in inputs else original_read(path, *args, **kwargs)

    def refuse_write(*args: object, **kwargs: object) -> None:
        pytest.fail("Drift checking must not write files")

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", refuse_write)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--check",
            "--snapshot",
            "public-input.json",
            "--upload-schema",
            "upload-input.json",
        ],
    )
    if changed is None:
        generator.main()
    else:
        with pytest.raises(SystemExit, match="Regenerate"):
            generator.main()
