from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    APIError,
    APIStatusError,
    ConflictError,
    Machinera,
    NotFoundError,
    RecoverableJobError,
    TerminalJobError,
    TranscriptionInterrupted,
)

FAILURE_TABLE = (
    "https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling"
)


def recovery_guidance(error: APIError) -> str:
    if isinstance(error, TranscriptionInterrupted):
        action = "Row 1: stop and save recovery context."
        if error.ambiguous:
            action += " Execution may have started; see row 4."
    elif isinstance(error, TerminalJobError):
        action = "Row 2: the job failed; never resume it."
    elif error.job_id is not None:
        if error.retryable is not True and (
            isinstance(error, (NotFoundError, ConflictError))
            or 400 <= (error.status_code or 0) < 500
        ):
            action = "Row 3: permanent for this job (row 7)."
        else:
            action = "Row 3: resume that job within your retry budget."
    elif isinstance(error, AmbiguousSubmissionError):
        action = "Row 4: execution may have started; repeating may charge again."
    elif isinstance(error, RecoverableJobError) or (
        isinstance(error, (APIConnectionError, APIStatusError))
        and error.retryable is True
        and error.phase in ("job_submit", "submit")
    ):
        action = "Row 5: recover by phase and saved operation key."
    elif error.is_transient:
        action = "Row 6: pause before repeating the identical call with the same key."
    else:
        action = "Row 7: permanent for this input."
    return f"{action} Follow {FAILURE_TABLE}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Handle transcription failures without logging data."
    )
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        with Machinera() as client:
            result = client.transcribe_file(args.file, model="transcribe-v1")
    except (ValueError, TypeError):
        print("Correct the local configuration or input before retrying.", file=sys.stderr)
        return 1
    except APIError as error:
        print(recovery_guidance(error), file=sys.stderr)
        return 1
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
