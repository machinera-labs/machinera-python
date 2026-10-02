from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    DeadlineExceededError,
    InternalServerError,
    Machinera,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    TerminalJobError,
    TranscriptionInterrupted,
    UnprocessableEntityError,
    UploadError,
)

RECONCILE = "Execution may have started. Reconcile the operation before submitting again."
REPEAT = "Repeat the identical call with idempotency_key set to the error's operation key."


def recovery_guidance(error: APIError) -> str:
    if isinstance(error, TerminalJobError):
        return "The job failed. Review the failure before deciding whether to create a new job."
    if isinstance(error, AmbiguousSubmissionError):
        return RECONCILE
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return "Check the API key and its permissions before retrying."
    if isinstance(error, (BadRequestError, UnprocessableEntityError, PayloadTooLargeError)):
        return "Correct the input or configured limits before retrying."
    if isinstance(error, UploadError):
        return "Keep the file unchanged and follow the large-file recovery steps."
    if isinstance(error, TranscriptionInterrupted) and error.ambiguous:
        return RECONCILE
    if error.phase == "sync_submit" and not isinstance(error, RateLimitError):
        return RECONCILE
    if isinstance(error, (NotFoundError, ConflictError)):
        return "The job or key cannot be found or replayed. Reconcile; do not use a new key."
    if error.job_id is not None:
        return "Preserve the job ID privately and resume that job; do not submit a replacement."
    if isinstance(error, (RateLimitError, InternalServerError)):
        return "Safe retries were exhausted. Pause first. " + REPEAT
    if isinstance(
        error,
        (
            DeadlineExceededError,
            TranscriptionInterrupted,
            APIConnectionError,
            APIResponseValidationError,
        ),
    ):
        return "Admission was not confirmed. " + REPEAT
    return "The service refused the operation. Inspect the error code and reconcile first."


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
