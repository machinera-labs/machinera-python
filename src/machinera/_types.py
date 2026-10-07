from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ._contract import (
    DEFAULT_SYNC_CAP_BYTES,
    MAX_DESCRIPTOR_BYTES,
    RESPONSE_FORMATS,
)

if TYPE_CHECKING:
    # Static Literal arguments cannot be derived from runtime collections.
    ResponseFormat = Literal["json", "text", "verbose_json"]
else:
    ResponseFormat = Literal[tuple(sorted(RESPONSE_FORMATS))]

JobStatus = Literal["queued", "processing", "completed", "error"]

if TYPE_CHECKING:
    from pydantic import ModelWrapValidatorHandler


def positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class TimeoutPolicy:
    """HTTP phase and elapsed budgets in seconds; field declarations set defaults.

    Phases accept None to disable inactivity limits. A watchdog bounds preparation,
    concurrency waits, retries and complete HTTP exchanges by the total deadline;
    poll requests also have their own elapsed bound. These are explicit SDK limits.
    HTTP inactivity semantics: https://www.python-httpx.org/advanced/timeouts/
    """

    connect: float | None = 5
    write: float | None = 600
    read: float | None = 600
    pool: float | None = 600
    poll_request: float = 30
    deadline: float = 3600

    def __post_init__(self) -> None:
        for name in ("connect", "write", "read", "pool", "poll_request", "deadline"):
            value = getattr(self, name)
            if value is not None or name in ("poll_request", "deadline"):
                positive(value, name)


class Unset:
    pass


UNSET = Unset()
Timeout = float | httpx.Timeout | TimeoutPolicy | None | Unset


def resolve_timeout(value: Timeout, inherited: TimeoutPolicy) -> TimeoutPolicy:
    if isinstance(value, Unset):
        return inherited
    if isinstance(value, TimeoutPolicy):
        return value
    if isinstance(value, httpx.Timeout):
        phases = value.as_dict()
    elif value is None:
        phases = dict.fromkeys(("connect", "write", "read", "pool"), None)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        positive(value, "timeout")
        phases = dict.fromkeys(("connect", "write", "read", "pool"), value)
    else:
        raise TypeError("timeout must be seconds, httpx.Timeout, None, or TimeoutPolicy")
    return replace(
        inherited,
        connect=phases["connect"],
        write=phases["write"],
        read=phases["read"],
        pool=phases["pool"],
    )


@dataclass(frozen=True)
class RetryPolicy:
    """Replay-safe step attempts, backoff and polling; field declarations set defaults.

    max_attempts includes the initial request. Exponential backoff starts at
    initial_delay and is capped by max_delay before uniform jitter in [0.75, 1].

    Positive Retry-After is a minimum; zero leaves bounded backoff in effect.
    poll_interval sets normal polling cadence; polling ends at the call's deadline.
    max_polls, when set, is an additional hard cap on the number of status reads.
    HTTP transport retries use the default of zero:
    https://www.python-httpx.org/advanced/transports/
    """

    max_attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 8
    poll_interval: float = 1
    max_polls: int | None = None

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for name in ("initial_delay", "max_delay", "poll_interval"):
            positive(getattr(self, name), name)
        if self.initial_delay > self.max_delay:
            raise ValueError("delays must satisfy initial_delay <= max_delay")
        if self.max_polls is not None and (type(self.max_polls) is not int or self.max_polls < 1):
            raise ValueError("max_polls must be None or a positive integer")


FILE_UPLOAD_THRESHOLD_BYTES = 50 * 1024 * 1024
"""SDK selection point for file uploads, below the service multipart cap."""


@dataclass(frozen=True)
class Limits:
    """Encoded request-size limits in bytes; field declarations set defaults."""

    sync_inline_body_bytes: int = DEFAULT_SYNC_CAP_BYTES
    job_multipart_body_bytes: int = FILE_UPLOAD_THRESHOLD_BYTES
    descriptor_bytes: int = MAX_DESCRIPTOR_BYTES

    def __post_init__(self) -> None:
        for name in ("sync_inline_body_bytes", "job_multipart_body_bytes", "descriptor_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.sync_inline_body_bytes > self.job_multipart_body_bytes:
            raise ValueError("sync limit must not exceed job limit")


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    @model_validator(mode="wrap")
    @classmethod
    def retain_raw(
        cls, value: Any, handler: ModelWrapValidatorHandler[_ResponseModel]
    ) -> _ResponseModel:
        result = handler(value)
        if isinstance(value, dict):
            # Keep SDK metadata separate from an ordinary wire field named raw.
            result._raw = value.copy()
        return result


class TranscriptionWord(_ResponseModel):
    """A verbatim word and its timestamps in seconds."""

    word: StrictStr = Field(repr=False)
    start: float
    end: float
    confidence: float | None = None


class _Warning(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: StrictInt
    message: StrictStr | None = None


_WARNINGS = TypeAdapter(list[_Warning])


class _WarningsModel(_ResponseModel):
    warnings: Any = Field(default=None, repr=False)

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: Any) -> Any:
        if value is not None:
            _WARNINGS.validate_python(value)
        return value


class TranscriptionResult(_WarningsModel):
    """Exact returned text and metadata; elapsed_seconds is end-to-end call time."""

    text: StrictStr = Field(repr=False)
    request_id: str | None = None
    job_id: str | None = None
    elapsed_seconds: float = 0
    response_format: ResponseFormat = "json"
    task: str | None = None
    language: str | None = None
    duration: float | None = None
    inference_seconds: float | None = None
    words: list[TranscriptionWord] | None = Field(default=None, repr=False)
    segments: Any = Field(default=None, repr=False)
    usage: Any = Field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"text": self.text}
        if "usage" in self.raw:
            result["usage"] = self.raw["usage"]
        return result

    def to_text(self) -> str:
        return self.text

    def to_verbose_json(self) -> dict[str, Any]:
        return self.raw.copy()

    @property
    def output(self) -> str | dict[str, Any]:
        if self.response_format == "text":
            return self.to_text()
        return self.to_json() if self.response_format == "json" else self.to_verbose_json()


class JobError(_ResponseModel):
    """Error metadata returned in a job snapshot."""

    code: StrictInt | None = None
    message: StrictStr | None = Field(default=None, repr=False)
    type: StrictStr | None = None
    retryable: bool | None = None
    details: Any = Field(default=None, repr=False)


class JobSnapshot(_WarningsModel):
    """One job status response, retaining all original fields in raw.

    status is an open string so that statuses added by the service still parse;
    JobStatus lists the statuses known to this SDK version.
    """

    id: StrictStr
    status: StrictStr
    created_at: int | None = None
    updated_at: int | None = None
    eta_seconds: float | None = None
    error: JobError | None = Field(default=None, repr=False)
    result: TranscriptionResult | None = Field(default=None, repr=False)
