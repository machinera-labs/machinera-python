"""Generated service contract; do not edit."""
# contract-sha256: c5cefb4e18ffa01eb98dfa586db7cb83cda2d8208acc7e30fb8039939357e7be

from collections.abc import Mapping
from typing import NamedTuple


class ErrorCode(NamedTuple):
    code: int
    status: int
    type: str
    retryable: bool


ERROR_CODES: Mapping[int, ErrorCode] = {
    1001: ErrorCode(1001, 404, "invalid_request_error", False),
    1002: ErrorCode(1002, 409, "invalid_request_error", False),
    1003: ErrorCode(1003, 410, "invalid_request_error", False),
    1004: ErrorCode(1004, 400, "invalid_request_error", False),
    1005: ErrorCode(1005, 409, "invalid_request_error", False),
    1006: ErrorCode(1006, 414, "invalid_request_error", False),
    1007: ErrorCode(1007, 431, "invalid_request_error", False),
    1008: ErrorCode(1008, 400, "invalid_request_error", False),
    1009: ErrorCode(1009, 400, "invalid_request_error", False),
    1010: ErrorCode(1010, 400, "invalid_request_error", False),
    1011: ErrorCode(1011, 400, "invalid_request_error", False),
    1012: ErrorCode(1012, 400, "invalid_request_error", False),
    1013: ErrorCode(1013, 400, "invalid_request_error", False),
    1014: ErrorCode(1014, 413, "invalid_request_error", False),
    1015: ErrorCode(1015, 400, "invalid_request_error", False),
    1016: ErrorCode(1016, 400, "invalid_request_error", False),
    1017: ErrorCode(1017, 400, "invalid_request_error", False),
    1018: ErrorCode(1018, 499, "invalid_request_error", False),
    1019: ErrorCode(1019, 400, "invalid_request_error", False),
    1020: ErrorCode(1020, 400, "invalid_request_error", False),
    1021: ErrorCode(1021, 413, "invalid_request_error", False),
    1022: ErrorCode(1022, 413, "invalid_request_error", False),
    1023: ErrorCode(1023, 411, "invalid_request_error", False),
    1024: ErrorCode(1024, 400, "invalid_request_error", False),
    1025: ErrorCode(1025, 400, "invalid_request_error", False),
    1026: ErrorCode(1026, 400, "invalid_request_error", False),
    1027: ErrorCode(1027, 400, "invalid_request_error", False),
    1028: ErrorCode(1028, 400, "invalid_request_error", False),
    1029: ErrorCode(1029, 422, "invalid_request_error", False),
    1030: ErrorCode(1030, 409, "invalid_request_error", False),
    1031: ErrorCode(1031, 422, "invalid_request_error", False),
    1032: ErrorCode(1032, 400, "invalid_request_error", False),
    1033: ErrorCode(1033, 400, "invalid_request_error", False),
    1034: ErrorCode(1034, 400, "invalid_request_error", False),
    1035: ErrorCode(1035, 400, "invalid_request_error", False),
    1036: ErrorCode(1036, 404, "invalid_request_error", False),
    1037: ErrorCode(1037, 405, "invalid_request_error", False),
    1038: ErrorCode(1038, 404, "invalid_request_error", False),
    1042: ErrorCode(1042, 400, "invalid_request_error", False),
    1043: ErrorCode(1043, 409, "api_error", False),
    2001: ErrorCode(2001, 402, "invalid_request_error", False),
    2002: ErrorCode(2002, 402, "invalid_request_error", False),
    2003: ErrorCode(2003, 401, "authentication_error", False),
    2004: ErrorCode(2004, 403, "authentication_error", False),
    3001: ErrorCode(3001, 429, "rate_limit_exceeded", True),
    3002: ErrorCode(3002, 429, "rate_limit_exceeded", False),
    3003: ErrorCode(3003, 429, "rate_limit_exceeded", False),
    3004: ErrorCode(3004, 429, "rate_limit_exceeded", True),
    3005: ErrorCode(3005, 429, "rate_limit_exceeded", True),
    4001: ErrorCode(4001, 503, "api_error", True),
    4002: ErrorCode(4002, 502, "api_error", False),
    4003: ErrorCode(4003, 504, "api_error", False),
    4004: ErrorCode(4004, 502, "api_error", True),
    4005: ErrorCode(4005, 503, "api_error", False),
    4006: ErrorCode(4006, 503, "api_error", False),
    4007: ErrorCode(4007, 503, "api_error", False),
    4008: ErrorCode(4008, 503, "api_error", True),
    4009: ErrorCode(4009, 503, "api_error", True),
    4010: ErrorCode(4010, 503, "api_error", True),
    4011: ErrorCode(4011, 503, "api_error", True),
    4012: ErrorCode(4012, 502, "api_error", True),
    4013: ErrorCode(4013, 500, "api_error", False),
    4014: ErrorCode(4014, 503, "api_error", False),
    4015: ErrorCode(4015, 503, "api_error", False),
    4016: ErrorCode(4016, 503, "api_error", False),
    4017: ErrorCode(4017, 503, "api_error", False),
    4018: ErrorCode(4018, 400, "api_error", True),
    4019: ErrorCode(4019, 408, "api_error", True),
    4020: ErrorCode(4020, 400, "invalid_request_error", False),
    4021: ErrorCode(4021, 400, "invalid_request_error", True),
    5001: ErrorCode(5001, 503, "api_error", False),
    5002: ErrorCode(5002, 502, "api_error", False),
    5003: ErrorCode(5003, 501, "api_error", False),
    5004: ErrorCode(5004, 502, "api_error", False),
    5005: ErrorCode(5005, 200, "api_error", True),
    5006: ErrorCode(5006, 200, "rate_limit_exceeded", True),
    5007: ErrorCode(5007, 200, "rate_limit_exceeded", True),
    5008: ErrorCode(5008, 200, "api_error", True),
    5009: ErrorCode(5009, 200, "api_error", True),
    5010: ErrorCode(5010, 200, "api_error", True),
    5011: ErrorCode(5011, 200, "api_error", False),
    5012: ErrorCode(5012, 500, "api_error", False),
    5013: ErrorCode(5013, 500, "api_error", False),
    5014: ErrorCode(5014, 502, "api_error", False),
    5015: ErrorCode(5015, 500, "api_error", False),
    5016: ErrorCode(5016, 503, "api_error", False),
}

RETRYABLE_CODES: frozenset[int] = frozenset(
    code for code, entry in ERROR_CODES.items() if entry.retryable
)
TERMINAL_CODES: frozenset[int] = frozenset(
    code for code, entry in ERROR_CODES.items() if not entry.retryable
)

UPLOAD_ERROR_CODES: frozenset[int] = frozenset({1001, 1002, 1003, 1004, 1005, 5016})

UPLOAD_INTEGRITY_CODES: frozenset[int] = frozenset({1004})

UPLOADS_UNAVAILABLE_CODES: frozenset[int] = frozenset({5001})

SYNC_CAP_FALLBACK_CODES: frozenset[int] = frozenset({1021, 1022})

SYNC_FALLBACK_CODES: frozenset[int] = frozenset({4008})

UPLOAD_INCOMPLETE_CODES: frozenset[int] = frozenset({1002})

UPLOAD_EXPIRED_CODES: frozenset[int] = frozenset({1003})

SYNC_REPLAYABLE_CODES: frozenset[int] = frozenset({4001, 4008, 4018, 4019, 4021})

JOB_STATUSES: tuple[str, ...] = ("queued", "processing", "completed", "error")

PENDING_JOB_STATUSES: frozenset[str] = frozenset({"processing", "queued"})

TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"completed", "error"})

PUBLISHED_MODEL_ALIASES: tuple[str, ...] = ("transcribe-v1",)

DEFAULT_RESULT_RETENTION_S: int = 259200

DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S: int = 86400

UPLOAD_GRANT_STATES: tuple[str, ...] = ("pending", "bound", "expired")

IDEMPOTENCY_KEY_HEADER: str = "Idempotency-Key"

CONTENT_MD5_HEADER: str = "X-Content-MD5"

RETRY_AFTER_HEADER: str = "Retry-After"

SUPPORTED_MEDIA_SUFFIXES: frozenset[str] = frozenset(
    {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "ogg", "wav", "webm"}
)

RESPONSE_FORMATS: frozenset[str] = frozenset({"json", "text", "verbose_json"})

DEFERRED_RESPONSE_FORMATS: frozenset[str] = frozenset({"srt", "vtt"})

TIMESTAMP_GRANULARITIES: frozenset[str] = frozenset({"word"})

DEFERRED_TIMESTAMP_GRANULARITIES: frozenset[str] = frozenset({"segment"})

SERVED_LANGUAGE: str = "en"

DEFAULT_SYNC_CAP_BYTES: int = 26214400

MAX_DESCRIPTOR_BYTES: int = 65536

MAX_BATCH_SUBMISSIONS: int = 64

MAX_BATCH_BODY_BYTES: int = 4195328

MAX_BATCH_DESCRIPTOR_BYTES: int = 65536

MAX_BATCH_STATUSES: int = 300

DEFAULT_MULTIPART_CAP_BYTES: int = 99614720
