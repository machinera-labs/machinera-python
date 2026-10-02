from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from machinera import APIError, Machinera


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe a large file with a saved key.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--operation-key", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--job-id")
    parser.add_argument("--upload-id")
    args = parser.parse_args(argv)
    try:
        with Machinera() as client:
            if args.resume:
                result = client.resume(
                    args.job_id,
                    file=args.file,
                    model="transcribe-v1",
                    operation_key=args.operation_key,
                    upload_id=args.upload_id,
                )
            else:
                result = client.transcribe_file(
                    args.file, model="transcribe-v1", idempotency_key=args.operation_key
                )
    except APIError as error:
        sys.stderr.write(
            json.dumps(
                {
                    "operation_key": error.operation_key,
                    "upload_id": error.upload_id,
                    "phase": error.phase,
                    "job_id": error.job_id,
                }
            )
            + "\n"
        )
        return 1
    except (ValueError, TypeError):
        sys.stderr.write("Correct the local configuration or input before retrying.\n")
        return 1
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
