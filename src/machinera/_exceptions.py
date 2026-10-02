from __future__ import annotations

import threading
from typing import Any

from . import _codes
from ._contract import ERROR_CODES

# Refusals that prove an unkeyed synchronous request did not run, so replaying it is safe.
SYNC_REPLAYABLE_CODES = frozenset(
    code for code, entry in ERROR_CODES.items() if entry.retryable and entry.status in (400, 408)
) | {
    _codes.input_busy.code,
    _codes.inline_admission_refused.code,
    _codes.no_serving_capacity.code,
}


def retry_eligible(
    status: int | None, code: str | None, retryable: bool | None, replay_safe: bool
) -> bool:
    """Whether a received error response may be retried; the retry loop and is_transient agree."""
    return (
        (replay_safe or status == 429 or code in SYNC_REPLAYABLE_CODES)
        and retryable is True
        and status not in (401, 403)
    )


class MachineraError(Exception):
    """Base exception for SDK failures."""

    @property
    def is_transient(self) -> bool:
        """True when trying again later may succeed; never True where the SDK refuses to retry.

        A RecoverableJobError is transient only with a job_id: resume that job. A status
        error is transient when the SDK's own retry rule would retry it, treating the
        "sync_submit" phase as not replay-safe. A local failure (file I/O or a closed
        client) is never transient.
        """
        if isinstance(self, RecoverableJobError):
            return self.job_id is not None
        if not isinstance(self, APIError) or self._local:
            return False
        unsent = self.phase != "sync_submit"
        if isinstance(self, APIConnectionError):
            return self.retryable is True or (self.retryable is None and unsent)
        if isinstance(self, APIStatusError):
            return retry_eligible(self.status_code, self.code, self.retryable, unsent)
        return False


class RecoverableJobError(MachineraError):
    """Marker for a call that ended while its job may still run; resume job_id when set.

    job_id is None when no job ID was observed. The job may still have been accepted
    (its response can be lost), so recover by repeating the identical call with
    idempotency_key=operation_key, never with a fresh key.
    """

    job_id: str | None


class APIError(MachineraError):
    """Sanitized failure with nullable service metadata and recovery context."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: dict[str, object] | None = None,
        code: str | None = None,
        retryable: bool | None = None,
        request_id: str | None = None,
        job_id: str | None = None,
        operation_key: str | None = None,
        phase: str | None = None,
        last_status: str | None = None,
        upload_id: str | None = None,
        storage_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.code = code
        self.retryable = (
            False
            if isinstance(self, (DeadlineExceededError, AmbiguousSubmissionError))
            else retryable
        )
        self.request_id = request_id
        self.job_id = job_id
        self.operation_key = operation_key
        self.phase = phase
        self.last_status = last_status
        self.upload_id = upload_id
        self.storage_code = storage_code
        self._file_released: threading.Event | None = None
        self._inline_cap: int | None = None
        self._local = False

    def __str__(self) -> str:
        text = super().__str__()
        return text if self.request_id is None else f"{text} (request_id: {self.request_id})"

    @property
    def status(self) -> int | None:
        """Read-only alias for status_code."""
        return self.status_code

    def wait_for_file_release(self, timeout: float | None = None) -> bool:
        """Wait until the SDK stops accessing the input file; zero only checks readiness."""
        return self._file_released is None or self._file_released.wait(timeout)


class APIStatusError(APIError):
    """An unsuccessful HTTP response, or a local size rejection."""


class BadRequestError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class NotFoundError(APIStatusError):
    pass


class ConflictError(APIStatusError):
    pass


class PayloadTooLargeError(APIStatusError):
    pass


class UnprocessableEntityError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class APIResponseValidationError(APIError):
    """A response body or shape the SDK cannot use; never retried automatically."""


class DeadlineExceededError(APIError, RecoverableJobError):
    """Call budget exhausted; resume job_id when set, as the job may still be running.

    With phase "sync_submit", reconcile instead of resubmitting. Otherwise repeat the
    identical call with idempotency_key=operation_key; never use a fresh key.
    """


class AmbiguousSubmissionError(APIError):
    """An unkeyed synchronous request may have run; there is no job to resume.

    Reconcile instead of resubmitting with either a new or the reported key.
    """


class TerminalJobError(APIError):
    """The job reached a failed or unrecognized terminal status; last_status names it."""


class UploadError(APIError):
    """The file upload failed or was refused; see the README's Large files section."""


class IntegrityError(UploadError):
    """The input changed, or the stored upload did not match; keep the input unchanged."""


class TerminalIntegrityError(IntegrityError, TerminalJobError):
    """The job failed because the uploaded file did not match; resuming cannot succeed."""


class TranscriptionInterrupted(APIError, RecoverableJobError, KeyboardInterrupt):
    """Interrupted call; resume job_id when set, and reconcile when ambiguous is True.

    Otherwise repeat the identical call with idempotency_key=operation_key.
    """

    def __init__(self, message: str, *, ambiguous: bool = False, **context: Any) -> None:
        super().__init__(message, **context)
        self._ambiguous = ambiguous

    @property
    def ambiguous(self) -> bool:
        """True when an unkeyed synchronous request may have run; reconcile, never resubmit."""
        return self._ambiguous
