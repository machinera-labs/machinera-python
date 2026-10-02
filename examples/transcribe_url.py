from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from machinera import Machinera


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe a direct audio URL.")
    parser.add_argument("url")
    args = parser.parse_args(argv)
    with Machinera() as client:
        result = client.transcribe_url(args.url, model="transcribe-v1")
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
