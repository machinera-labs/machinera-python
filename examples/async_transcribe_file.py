from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from machinera import APIError, AsyncMachinera


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe a local file with asyncio.")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    try:
        async with AsyncMachinera() as client:
            result = await client.transcribe_file(args.file, model="transcribe-v1")
    except (APIError, OSError, ValueError, TypeError):
        print("Transcription failed; inspect recovery context before retrying.", file=sys.stderr)
        return 1
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
