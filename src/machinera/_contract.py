"""Generated service contract; do not edit."""
# contract-sha256: 43b8e78037da4b5fd72cfd81a25161203cb14925a390736e9f8de3370551353c

from collections.abc import Mapping
from typing import NamedTuple


class ErrorCode(NamedTuple):
    code: str
    status: int
    type: str
    retryable: bool


# Public error codes.
ERROR_CODES: Mapping[str, ErrorCode] = {
    "api_key_forbidden": ErrorCode(
        code="api_key_forbidden",
        status=403,
        type="authentication_error",
        retryable=False,
    ),
    "audio_duration_exceeded": ErrorCode(
        code="audio_duration_exceeded",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "audio_ref_expired": ErrorCode(
        code="audio_ref_expired",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "audio_unavailable": ErrorCode(
        code="audio_unavailable",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "auth_rate_limited": ErrorCode(
        code="auth_rate_limited",
        status=429,
        type="rate_limit_exceeded",
        retryable=True,
    ),
    "batch_size_exceeded": ErrorCode(
        code="batch_size_exceeded",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "batch_status_deferred": ErrorCode(
        code="batch_status_deferred",
        status=429,
        type="rate_limit_exceeded",
        retryable=True,
    ),
    "billing_blocked": ErrorCode(
        code="billing_blocked",
        status=402,
        type="invalid_request_error",
        retryable=False,
    ),
    "broker_capacity_exhausted": ErrorCode(
        code="broker_capacity_exhausted",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "clip_exceeds_tier_capacity": ErrorCode(
        code="clip_exceeds_tier_capacity",
        status=429,
        type="rate_limit_exceeded",
        retryable=False,
    ),
    "config_missing": ErrorCode(
        code="config_missing",
        status=500,
        type="api_error",
        retryable=True,
    ),
    "content_md5_mismatch": ErrorCode(
        code="content_md5_mismatch",
        status=400,
        type="invalid_request_error",
        retryable=True,
    ),
    "duration_exceeds_declared": ErrorCode(
        code="duration_exceeds_declared",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "fusion_failed": ErrorCode(
        code="fusion_failed",
        status=502,
        type="api_error",
        retryable=True,
    ),
    "idempotency_payload_mismatch": ErrorCode(
        code="idempotency_payload_mismatch",
        status=422,
        type="invalid_request_error",
        retryable=False,
    ),
    "idempotency_replay_unavailable": ErrorCode(
        code="idempotency_replay_unavailable",
        status=409,
        type="invalid_request_error",
        retryable=False,
    ),
    "incomplete_body": ErrorCode(
        code="incomplete_body",
        status=400,
        type="api_error",
        retryable=True,
    ),
    "incomplete_upload": ErrorCode(
        code="incomplete_upload",
        status=400,
        type="invalid_request_error",
        retryable=True,
    ),
    "inline_admission_refused": ErrorCode(
        code="inline_admission_refused",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "inline_body_over_cap": ErrorCode(
        code="inline_body_over_cap",
        status=413,
        type="invalid_request_error",
        retryable=False,
    ),
    "inline_claim_timeout": ErrorCode(
        code="inline_claim_timeout",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "inline_completion_timeout": ErrorCode(
        code="inline_completion_timeout",
        status=504,
        type="api_error",
        retryable=True,
    ),
    "inline_dispatch_invalid": ErrorCode(
        code="inline_dispatch_invalid",
        status=502,
        type="api_error",
        retryable=True,
    ),
    "inline_lifecycle_unavailable": ErrorCode(
        code="inline_lifecycle_unavailable",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "inline_transfer_failed": ErrorCode(
        code="inline_transfer_failed",
        status=502,
        type="api_error",
        retryable=True,
    ),
    "input_busy": ErrorCode(
        code="input_busy",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "insufficient_balance": ErrorCode(
        code="insufficient_balance",
        status=402,
        type="invalid_request_error",
        retryable=False,
    ),
    "invalid_api_key": ErrorCode(
        code="invalid_api_key",
        status=401,
        type="authentication_error",
        retryable=False,
    ),
    "invalid_duration_declaration": ErrorCode(
        code="invalid_duration_declaration",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "invalid_request": ErrorCode(
        code="invalid_request",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "job_attempts_exhausted": ErrorCode(
        code="job_attempts_exhausted",
        status=200,
        type="api_error",
        retryable=True,
    ),
    "job_not_found": ErrorCode(
        code="job_not_found",
        status=404,
        type="invalid_request_error",
        retryable=False,
    ),
    "job_placement_lost": ErrorCode(
        code="job_placement_lost",
        status=200,
        type="api_error",
        retryable=True,
    ),
    "job_queue_timed_out": ErrorCode(
        code="job_queue_timed_out",
        status=200,
        type="rate_limit_exceeded",
        retryable=True,
    ),
    "job_shed": ErrorCode(
        code="job_shed",
        status=200,
        type="rate_limit_exceeded",
        retryable=True,
    ),
    "job_tombstoned": ErrorCode(
        code="job_tombstoned",
        status=200,
        type="api_error",
        retryable=True,
    ),
    "jobs_store_unconfigured": ErrorCode(
        code="jobs_store_unconfigured",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "length_required": ErrorCode(
        code="length_required",
        status=411,
        type="invalid_request_error",
        retryable=False,
    ),
    "media_unprobeable": ErrorCode(
        code="media_unprobeable",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "method_not_allowed": ErrorCode(
        code="method_not_allowed",
        status=405,
        type="invalid_request_error",
        retryable=False,
    ),
    "mock_box_unconfigured": ErrorCode(
        code="mock_box_unconfigured",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "model_required": ErrorCode(
        code="model_required",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "no_serving_capacity": ErrorCode(
        code="no_serving_capacity",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "non_english_audio": ErrorCode(
        code="non_english_audio",
        status=422,
        type="invalid_request_error",
        retryable=False,
    ),
    "origin_invalid": ErrorCode(
        code="origin_invalid",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "origin_not_configured": ErrorCode(
        code="origin_not_configured",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "payload_too_large": ErrorCode(
        code="payload_too_large",
        status=413,
        type="invalid_request_error",
        retryable=False,
    ),
    "queue_operation_rejected": ErrorCode(
        code="queue_operation_rejected",
        status=502,
        type="api_error",
        retryable=False,
    ),
    "request_aborted": ErrorCode(
        code="request_aborted",
        status=499,
        type="invalid_request_error",
        retryable=False,
    ),
    "request_body_timeout": ErrorCode(
        code="request_body_timeout",
        status=408,
        type="api_error",
        retryable=True,
    ),
    "request_headers_too_large": ErrorCode(
        code="request_headers_too_large",
        status=431,
        type="invalid_request_error",
        retryable=False,
    ),
    "request_path_too_long": ErrorCode(
        code="request_path_too_long",
        status=414,
        type="invalid_request_error",
        retryable=False,
    ),
    "result_unavailable": ErrorCode(
        code="result_unavailable",
        status=200,
        type="api_error",
        retryable=True,
    ),
    "result_unreadable": ErrorCode(
        code="result_unreadable",
        status=200,
        type="api_error",
        retryable=False,
    ),
    "service_unavailable": ErrorCode(
        code="service_unavailable",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "shared_queue_unavailable": ErrorCode(
        code="shared_queue_unavailable",
        status=503,
        type="api_error",
        retryable=True,
    ),
    "staged_uploads_unavailable": ErrorCode(
        code="staged_uploads_unavailable",
        status=503,
        type="api_error",
        retryable=False,
    ),
    "strong_pubkey_invalid": ErrorCode(
        code="strong_pubkey_invalid",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "strong_pubkey_missing": ErrorCode(
        code="strong_pubkey_missing",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "sub_ensemble_floor": ErrorCode(
        code="sub_ensemble_floor",
        status=502,
        type="api_error",
        retryable=False,
    ),
    "sync_size_cap": ErrorCode(
        code="sync_size_cap",
        status=413,
        type="invalid_request_error",
        retryable=False,
    ),
    "unknown_model": ErrorCode(
        code="unknown_model",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "unknown_url": ErrorCode(
        code="unknown_url",
        status=404,
        type="invalid_request_error",
        retryable=False,
    ),
    "unsupported_granularity": ErrorCode(
        code="unsupported_granularity",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "unsupported_language": ErrorCode(
        code="unsupported_language",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "unsupported_media_type": ErrorCode(
        code="unsupported_media_type",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "unsupported_response_format": ErrorCode(
        code="unsupported_response_format",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "unsupported_scheme": ErrorCode(
        code="unsupported_scheme",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "upload_already_bound": ErrorCode(
        code="upload_already_bound",
        status=409,
        type="invalid_request_error",
        retryable=False,
    ),
    "upload_expired": ErrorCode(
        code="upload_expired",
        status=410,
        type="invalid_request_error",
        retryable=False,
    ),
    "upload_incomplete": ErrorCode(
        code="upload_incomplete",
        status=409,
        type="invalid_request_error",
        retryable=False,
    ),
    "upload_integrity_mismatch": ErrorCode(
        code="upload_integrity_mismatch",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "upload_limit_exceeded": ErrorCode(
        code="upload_limit_exceeded",
        status=429,
        type="rate_limit_exceeded",
        retryable=True,
    ),
    "upload_not_found": ErrorCode(
        code="upload_not_found",
        status=404,
        type="invalid_request_error",
        retryable=False,
    ),
    "url_blocked": ErrorCode(
        code="url_blocked",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "url_fetch_unconfigured": ErrorCode(
        code="url_fetch_unconfigured",
        status=501,
        type="api_error",
        retryable=False,
    ),
    "url_unreachable": ErrorCode(
        code="url_unreachable",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
    "webhook_blocked": ErrorCode(
        code="webhook_blocked",
        status=400,
        type="invalid_request_error",
        retryable=False,
    ),
}

# Retryable public error codes.
RETRYABLE_CODES: frozenset[str] = frozenset(
    code for code, entry in ERROR_CODES.items() if entry.retryable
)

# Terminal public error codes.
TERMINAL_CODES: frozenset[str] = frozenset(
    code for code, entry in ERROR_CODES.items() if not entry.retryable
)

# Accepted media suffixes.
SUPPORTED_MEDIA_SUFFIXES: frozenset[str] = frozenset(
    {
        "flac",
        "m4a",
        "mp3",
        "mp4",
        "mpeg",
        "mpga",
        "ogg",
        "wav",
        "webm",
    }
)

# Served response formats.
RESPONSE_FORMATS: frozenset[str] = frozenset({"json", "text", "verbose_json"})

# Deferred response formats.
DEFERRED_RESPONSE_FORMATS: frozenset[str] = frozenset({"srt", "vtt"})

# Served timestamp granularities.
TIMESTAMP_GRANULARITIES: frozenset[str] = frozenset({"word"})

# Deferred timestamp granularities.
DEFERRED_TIMESTAMP_GRANULARITIES: frozenset[str] = frozenset({"segment"})

# Served language.
SERVED_LANGUAGE: str = "en"

# Synchronous multipart byte cap.
DEFAULT_SYNC_CAP_BYTES: int = 26214400

# Inline job body byte cap.
DEFAULT_INLINE_CAP_BYTES: int = 99614720

# Job descriptor byte cap.
MAX_DESCRIPTOR_BYTES: int = 65536

# Batch submission count cap.
MAX_BATCH_SUBMISSIONS: int = 64

# Batch request body byte cap.
MAX_BATCH_BODY_BYTES: int = 4195328

# Batch item descriptor byte cap.
MAX_BATCH_DESCRIPTOR_BYTES: int = 65536

# Batch status count cap.
MAX_BATCH_STATUSES: int = 300
