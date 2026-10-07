from __future__ import annotations

import threading
from typing import Any

from ._contract import (
    SYNC_ACCEPTANCE_AMBIGUOUS_CODES,
    SYNC_FALLBACK_CODES,
    SYNC_REPLAYABLE_CODES,
    UPLOAD_EXPIRED_CODES,
)


def retry_eligible(
    status: int | None,
    code: int | None,
    retryable: bool | None,
    replay_safe: bool,
    replay_after_send: bool = False,
    guidance: bool | None = None,
) -> bool:
    """Classify status retries; see api.md#synchronous-replay and api.md#machineraerror."""
    if not replay_safe and (code in SYNC_FALLBACK_CODES or code in SYNC_ACCEPTANCE_AMBIGUOUS_CODES):
        return False
    if (
        replay_after_send
        and not replay_safe
        and status is not None
        and 500 <= status < 600
        and guidance is not False
    ):
        return True
    replayable = replay_safe or status == 429 or code in SYNC_REPLAYABLE_CODES or replay_after_send
    return replayable and retryable is True and status not in (401, 403)


def explicit_guidance(body: object) -> bool | None:
    """The explicit retryable flag a service response carried, or None."""
    value = body.get("retryable") if isinstance(body, dict) else None
    return value if isinstance(value, bool) else None


def connection_replayable(unsent: bool, replay_safe: bool, replay_after_send: bool) -> bool:
    """Whether a failed exchange may be retried; the retry loop records it as retryable.

    replay_after_send is the opt-in sync_replay="always" policy for synchronous requests.
    """
    return unsent or replay_safe or replay_after_send


class MachineraError(Exception):
    """Base exception for SDK failures."""

    @property
    def is_transient(self) -> bool:
        """True when repeating the identical call, as made, is safe and may succeed.
        See api.md#machineraerror for the decision table.
        """
        if isinstance(self, TranscriptionInterrupted) or getattr(self, "_key_rotated", False):
            return False
        sdk_key = getattr(self, "_caller_key", None) is False
        lost = getattr(self, "_submission_lost", False)
        if sdk_key and lost and getattr(self, "job_id", None) is None:
            return False
        if isinstance(self, RecoverableJobError):
            if self.job_id is not None:
                return not sdk_key
            phase = getattr(self, "phase", None)
            if phase in ("prepare", "concurrency_wait"):
                return True
            if phase == "sync_submit":
                return bool(getattr(self, "_sync_replay", False))
            return getattr(self, "_caller_key", None) is True
        if not isinstance(self, APIError) or self._local:
            return False
        if isinstance(self, TerminalJobError):
            return self.retryable is True and sdk_key
        if self.job_id is not None and sdk_key:
            return False
        unsent = self.phase != "sync_submit"
        if isinstance(self, APIConnectionError):
            return self.retryable is True or (self.retryable is None and unsent)
        if isinstance(self, APIStatusError):
            return retry_eligible(
                self.status_code,
                self.code,
                self.retryable,
                unsent,
                self._sync_replay,
                explicit_guidance(self.body),
            )
        return False


class RecoverableJobError(MachineraError):
    """Recovery context for an unfinished call; see api.md failure-table rows 3 and 5."""

    job_id: str | None


class APIError(MachineraError):
    """Sanitized failure with nullable service metadata and recovery context."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: dict[str, object] | None = None,
        code: int | None = None,
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
        if code is not None and type(code) is not int:
            raise TypeError("code must be an integer or None")
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
        self._multipart_cap: int | None = None
        self._local = False
        self._sync_replay = False
        self._caller_key: bool | None = None
        self._submission_lost = False
        self._key_rotated = False

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
    """Call budget exhausted; see api.md failure-table rows 3 and 5 for resume guidance."""


class AmbiguousSubmissionError(APIError):
    """A synchronous request may have run with no job to resume; see api.md failure-table row 4."""


class TerminalJobError(APIError):
    """The job reached a failed or unrecognized terminal status; last_status names it."""


class UploadError(APIError):
    """The file upload failed or was refused; see api.md#file-upload-recovery-and-expiry."""


class IntegrityError(UploadError):
    """The input changed, or the stored upload did not match; keep the input unchanged."""


class TerminalIntegrityError(IntegrityError, TerminalJobError):
    """The job failed because the uploaded file did not match; resuming cannot succeed."""


class TranscriptionInterrupted(APIError, RecoverableJobError, KeyboardInterrupt):
    """Interrupted call; see api.md failure-table row 1 for recovery guidance."""

    def __init__(self, message: str, *, ambiguous: bool = False, **context: Any) -> None:
        super().__init__(message, **context)
        self._ambiguous = ambiguous

    @property
    def ambiguous(self) -> bool:
        """Whether a synchronous request may have run; see api.md failure-table rows 1 and 4."""
        return self._ambiguous


def invalid_response(message: str, status_code: int) -> APIResponseValidationError:
    return APIResponseValidationError(message, status_code=status_code, retryable=False)


def expired_upload(
    message: str = "Upload has expired; recover any accepted job", *, status_code: int | None = None
) -> UploadError:
    return UploadError(
        message, code=next(iter(UPLOAD_EXPIRED_CODES)), status_code=status_code, retryable=False
    )
