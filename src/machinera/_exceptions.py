from __future__ import annotations

import threading
from typing import Any


class MachineraError(Exception):
    """Base exception for SDK failures."""


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


class DeadlineExceededError(APIError):
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


class TranscriptionInterrupted(APIError, KeyboardInterrupt):
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
