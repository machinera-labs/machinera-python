import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_retention_block_matches_contract() -> None:
    renderer = runpy.run_path(str(ROOT / "scripts" / "render_retention.py"))
    reference = (ROOT / "api.md").read_text(encoding="utf-8")
    assert renderer["update"](reference) == reference, (
        "Regenerate retention docs: PYTHONPATH=src python scripts/render_retention.py"
    )
