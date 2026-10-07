"""Render api.md retention minimums from the generated contract.

Run with PYTHONPATH=src python scripts/render_retention.py [--check].
"""

import argparse
from pathlib import Path

from machinera._contract import (
    DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S,
    DEFAULT_RESULT_RETENTION_S,
)

START = "<!-- BEGIN GENERATED RETENTION -->"
END = "<!-- END GENERATED RETENTION -->"
REFERENCE = Path(__file__).resolve().parents[1] / "api.md"


def duration(seconds: int) -> str:
    days = seconds / (24 * 60 * 60)
    return f"{days:g} {'day' if days == 1 else 'days'} ({seconds:,} s)"


def render() -> str:
    return (
        f"{START}\n"
        "Guaranteed minimums:\n\n"
        f"- Idempotency replay period ≥ {duration(DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S)}.\n"
        f"- Result retention ≥ {duration(DEFAULT_RESULT_RETENTION_S)}.\n\n"
        "Deployments may lengthen but never shorten either period below these defaults.\n"
        f"{END}"
    )


def update(text: str) -> str:
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("Expected exactly one generated retention block")
    before, rest = text.split(START)
    _, after = rest.split(END)
    return before + render() + after


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = REFERENCE.read_text(encoding="utf-8")
    expected = update(text)
    if args.check:
        if text != expected:
            raise SystemExit("Retention docs differ; run scripts/render_retention.py")
    else:
        REFERENCE.write_text(expected, encoding="utf-8")
