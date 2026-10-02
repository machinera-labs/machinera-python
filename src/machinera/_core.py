from __future__ import annotations

import itertools
import json
import math
import os
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from functools import wraps
from types import MappingProxyType
from typing import Any, BinaryIO, ClassVar, Literal, ParamSpec, TypeVar
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from . import _codes
from ._contract import (
    DEFAULT_INLINE_CAP_BYTES,
    ERROR_CODES,
    RESPONSE_FORMATS,
    RETRYABLE_CODES,
    SERVED_LANGUAGE,
)
from ._exceptions import (
    SYNC_FALLBACK_CODES,
    AmbiguousSubmissionError,
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    DeadlineExceededError,
    IntegrityError,
    InternalServerError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    TerminalIntegrityError,
    TerminalJobError,
    UnprocessableEntityError,
    UploadError,
    connection_replayable,
    retry_eligible,
)
from ._files import FileContent, FileInput, unpack_file, validate_headers
from ._logs import configure_from_env, logger
from ._multipart import Multipart, UploadBody
from ._types import (
    UNSET,
    JobSnapshot,
    Limits,
    ResponseFormat,
    RetryPolicy,
    Timeout,
    TimeoutPolicy,
    TranscriptionResult,
    Unset,
    positive,
    resolve_timeout,
)
from ._uploads import Grant, descriptor, initialization_key, storage_code
from ._version import __version__

_SIZE_REFUSAL_CODES = (None, _codes.sync_size_cap.code, _codes.inline_body_over_cap.code)
_PENDING_STATUSES = ("queued", "processing")
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+")


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _token(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return None


def _seconds(value: str | None, scale: float) -> float | None:
    if value is None or not _SECONDS.fullmatch(value.strip()):
        return None
    seconds = float(value) / scale
    return seconds if math.isfinite(seconds) else None


def local_failure(error: OSError | httpx.HTTPError) -> APIConnectionError:
    """Name the failure's class and errno, never its message, which may hold a path or URL."""
    number = error.errno if isinstance(error, OSError) else None
    detail = type(error).__name__ + (f", errno {number}" if type(number) is int else "")
    failure = APIConnectionError(f"Local I/O or HTTP operation failed ({detail})")
    failure._local = isinstance(error, OSError)
    return failure


def _path_template(path: str, storage: bool) -> str:
    if storage:
        return "upload storage"
    return "/transcription_jobs/{job_id}" if path.startswith("/transcription_jobs/") else path


_P = ParamSpec("_P")
_T = TypeVar("_T")


def _sanitized(function: Callable[_P, _T]) -> Callable[_P, _T]:
    @wraps(function)
    def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        failure: APIError | ValueError | TypeError
        try:
            return function(*args, **kwargs)
        except (APIError, ValueError, TypeError) as error:
            failure = error
        failure.__context__ = None
        failure.__cause__ = None
        raise failure from None

    return invoke


@dataclass
class _Call:
    start: float
    end: float
    timeout: TimeoutPolicy
    operation_key: str
    clock: Callable[[], float]
    phase: str = "prepare"
    upload_id: str | None = None
    grant: Grant | None = field(default=None, repr=False)
    job_id: str | None = None
    request_id: str | None = None
    last_status: str | None = None
    file_released: threading.Event | None = None
    read_idle: threading.Event | None = None

    def remaining(self) -> float:
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise DeadlineExceededError("Operation deadline exceeded")
        return remaining


@dataclass
class Send:
    request: httpx.Request
    body: bytes | Multipart | UploadBody
    budget: float
    expired: Callable[[], BaseException]
    check: Callable[[], None]
    storage: bool
    response: httpx.Response | None = None
    content: bytearray = field(default_factory=bytearray)


@dataclass
class Sleep:
    delay: float


@dataclass
class OpenFile:
    content: FileContent
    call: _Call


@dataclass
class Prepare:
    body: Multipart
    call: _Call


@dataclass
class CloseFile:
    source: BinaryIO


Effect = Send | Sleep | OpenFile | Prepare | CloseFile
Flow = Generator[Effect, Any, _T]


# A deadline cancels an upload or submission by shutting down its socket, so that pool
# keeps no idle connection that could be reused afterwards.
EXCHANGE_POOL = httpx.Limits(max_connections=100, max_keepalive_connections=0, keepalive_expiry=5)
# Job status polls are small GETs that reuse connections; httpx's DEFAULT_LIMITS values.
POLL_POOL = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5)


@dataclass(frozen=True, init=False, repr=False)
class Core:
    """Shared protocol state machine; drivers execute yielded I/O effects."""

    default_headers: Mapping[str, str]
    base_url: str
    timeout: TimeoutPolicy
    retry_policy: RetryPolicy
    limits: Limits
    max_concurrency: int | None
    transport: Literal["auto", "job"]
    sync_replay: Literal["never", "always"]
    _api_key: str = field(repr=False)
    _owns_http: bool
    _clock: Callable[[], float]
    _wall_clock: Callable[[], float]
    _random: Callable[[], float]
    _transport_type: ClassVar[type]
    _client_type: ClassVar[type]

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int | Unset = UNSET,
        default_headers: Mapping[str, str] | None = None,
        timeout: Timeout = UNSET,
        retry_policy: RetryPolicy | None = None,
        limits: Limits | None = None,
        max_concurrency: int | None = None,
        transport: Any = "auto",
        sync_replay: str = "never",
        http_client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        injected = isinstance(transport, self._transport_type)
        if not injected and transport not in ("auto", "job"):
            raise ValueError(
                f"transport must be auto, job, or an httpx.{self._transport_type.__name__}"
            )
        if injected and http_client is not None:
            raise ValueError("Supply either a transport or an HTTP client")
        if http_client is not None and not isinstance(http_client, self._client_type):
            raise TypeError(f"http_client must be an httpx.{self._client_type.__name__}")
        configure_from_env()
        api_key = api_key if api_key is not None else os.environ.get("MACHINERA_API_KEY")
        base_url = (
            base_url
            if base_url is not None
            else os.environ.get("MACHINERA_BASE_URL", "https://api.machinera.com/v1")
        )
        try:
            url = httpx.URL(base_url)
        except httpx.InvalidURL:
            raise ValueError("Invalid base_url") from None
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path.rstrip("/") not in ("", "/v1")
        ):
            raise ValueError("base_url must be an HTTP(S) API origin, optionally followed by /v1")
        if sync_replay not in ("never", "always"):
            raise ValueError("sync_replay must be never or always")
        if not isinstance(api_key, str) or not re.fullmatch(r"[!-~]+", api_key):
            raise ValueError("api_key must be a nonempty ASCII credential without whitespace")
        if max_concurrency is not None and (
            not isinstance(max_concurrency, int) or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if not isinstance(max_retries, Unset):
            if type(max_retries) is not int or max_retries < 0:
                raise ValueError("max_retries must be a nonnegative integer")
            if retry_policy is not None:
                raise ValueError("Supply either max_retries or retry_policy")
            retry_policy = RetryPolicy(max_attempts=max_retries + 1)
        settings: dict[str, Any] = {
            "default_headers": MappingProxyType(validate_headers(default_headers or {})),
            "base_url": str(url.copy_with(path="/v1")),
            "timeout": resolve_timeout(timeout, TimeoutPolicy()),
            "retry_policy": retry_policy or RetryPolicy(),
            "limits": limits or Limits(),
            "max_concurrency": max_concurrency,
            "transport": "auto" if injected else transport,
            "sync_replay": sync_replay,
            "_api_key": api_key,
            "_clock": clock,
            "_wall_clock": wall_clock,
            "_random": random_source,
        }
        for name, value in settings.items():
            object.__setattr__(self, name, value)

        def owned(limits: httpx.Limits, pool: Any = None) -> Any:
            return self._client_type(
                transport=transport if injected else pool,
                timeout=None,
                follow_redirects=False,
                trust_env=False,
                limits=limits,
            )

        http = http_client or owned(EXCHANGE_POOL)
        object.__setattr__(self, "_http", http)
        object.__setattr__(
            self,
            "_poll_http",
            http
            if http_client is not None or injected
            else owned(POLL_POOL, self._poll_transport(POLL_POOL)),
        )
        object.__setattr__(self, "_owns_http", http_client is None)

    def _poll_transport(self, limits: httpx.Limits) -> Any:
        return None

    def _safe_token(self, value: Any) -> str | None:
        token = _token(value)
        return token if token is not None and self._api_key not in token else None

    def _retry_after(self, response: httpx.Response) -> float:
        for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
            seconds = _seconds(response.headers.get(name), scale)
            if seconds is not None:
                return seconds
        value = response.headers.get("retry-after", "")
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - self._wall_clock())
        except (ValueError, TypeError, OverflowError):
            return 0

    def _wait(self, call: _Call, delay: float) -> Flow[None]:
        if delay >= call.remaining():
            raise DeadlineExceededError("Required wait exceeds the operation deadline")
        yield Sleep(delay)
        call.remaining()

    def _error(self, response: httpx.Response, *, terminal: bool = False) -> APIError:
        data = _body(response)
        detail = data.get("error", {}) if isinstance(data, dict) else {}
        if not isinstance(detail, dict):
            detail = {}
        code = self._safe_token(detail.get("code"))
        guidance = detail.get("retryable")
        retryable = guidance if isinstance(guidance, bool) else None
        if retryable is None:
            retryable = (
                code in RETRYABLE_CODES
                if code in ERROR_CODES
                else response.status_code in (429, 502, 503, 504)
            )
        status = response.status_code
        cls: type[APIError] = {
            400: BadRequestError,
            401: AuthenticationError,
            403: PermissionDeniedError,
            404: NotFoundError,
            409: ConflictError,
            413: PayloadTooLargeError,
            422: UnprocessableEntityError,
            429: RateLimitError,
        }.get(status, InternalServerError if 500 <= status < 600 else APIStatusError)
        if terminal:
            cls = TerminalJobError
        elif code is not None and code.startswith("upload_") and status != 429:
            cls = UploadError
        if code == _codes.upload_integrity_mismatch.code:
            cls = TerminalIntegrityError if terminal else IntegrityError
        body: dict[str, object] | None = None
        if isinstance(data, dict):
            body = {}
            if code is not None:
                body["code"] = code
            if isinstance(guidance, bool):
                body["retryable"] = guidance
            for name in ("job_id", "upload_id", "operation_key", "phase", "last_status"):
                value = self._safe_token(detail.get(name))
                if value is not None:
                    body[name] = value
        message = "Transcription job failed" if terminal else "Machinera API request failed"
        inline_cap: int | None = None
        if code == _codes.staged_uploads_unavailable.code:
            message = (
                "Staged uploads are unavailable, and the body cannot be sent inline: it "
                "exceeds the service inline limit or an upload was already granted"
            )
            limits = detail.get("limits")
            if not isinstance(limits, dict) and isinstance(data, dict):
                limits = data.get("limits")
            cap = limits.get("async_inline_body_bytes") if isinstance(limits, dict) else None
            inline_cap = cap if type(cap) is int and cap > 0 else DEFAULT_INLINE_CAP_BYTES
        error = cls(
            message,
            status_code=status,
            body=body,
            code=code,
            retryable=retryable,
            request_id=self._safe_token(response.headers.get("x-request-id")),
        )
        error._inline_cap = inline_cap
        return error

    def _request(
        self,
        call: _Call,
        method: str,
        path: str,
        *,
        body: bytes | Multipart | UploadBody = b"",
        headers: dict[str, str] | None = None,
        replay_safe: bool = True,
        replay_after_send: bool = False,
        storage: bool = False,
        before_attempt: Callable[[], Flow[tuple[str, dict[str, str]] | None]] | None = None,
    ) -> Flow[httpx.Response]:
        policy = self.retry_policy
        for attempt in range(policy.max_attempts):
            if before_attempt is not None:
                target = yield from before_attempt()
                if target is None:
                    return httpx.Response(204)
                path, headers = target
            remaining = call.remaining()
            request_end = self._clock() + call.timeout.poll_request
            cap = min(remaining, call.timeout.poll_request) if method == "GET" else remaining
            timeout = {
                name: None if value is None else min(value, cap)
                for name, value in (
                    (name, getattr(call.timeout, name))
                    for name in ("connect", "write", "read", "pool")
                )
            }
            request = httpx.Request(
                method,
                path if storage else self.base_url + path,
                headers=headers or {}
                if storage
                else {
                    "user-agent": f"machinera-python/{__version__}",
                    **self.default_headers,
                    "Authorization": f"Bearer {self._api_key}",
                    **(headers or {}),
                },
                content=body,
                extensions={"timeout": timeout},
            )
            response: httpx.Response | None = None
            error: APIError | None = None
            eligible = False

            def expired(request_end: float = request_end) -> BaseException:
                if not (replay_safe or replay_after_send):
                    return AmbiguousSubmissionError(
                        "Submission deadline exceeded; synchronous execution may have started",
                        retryable=False,
                    )
                if method == "GET" and request_end < call.end:
                    return httpx.ReadTimeout("Poll request timeout exceeded")
                return DeadlineExceededError("Operation deadline exceeded")

            def check(
                request_end: float = request_end, expired: Callable[[], BaseException] = expired
            ) -> None:
                if self._clock() >= call.end:
                    raise expired()
                if method == "GET" and self._clock() >= request_end:
                    raise httpx.ReadTimeout("Poll request timeout exceeded")

            exchange = Send(request, body, min(cap, call.remaining()), expired, check, storage)
            try:
                response = yield exchange
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                # Nothing was sent before a connect or pool failure, so it is always replay-safe.
                unsent = isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                )
                if connection_replayable(unsent, replay_safe, replay_after_send):
                    cls = (
                        APITimeoutError
                        if isinstance(exc, httpx.TimeoutException)
                        else APIConnectionError
                    )
                    error = cls(
                        "Could not connect to the Machinera API"
                        if unsent
                        else "HTTP exchange failed",
                        retryable=True,
                    )
                    eligible = True
                else:
                    error = AmbiguousSubmissionError(
                        "Synchronous submission may have executed; automatic replay is unsafe",
                        retryable=False,
                    )
            except httpx.HTTPError:
                error = APIConnectionError("HTTP exchange failed", retryable=False)
            finally:
                received = exchange.response
                if received is not None and not storage:
                    call.request_id = self._safe_token(received.headers.get("x-request-id"))
                    status = received.status_code
                    if status == 202 or (path == "/uploads" and status in (200, 201)):
                        try:
                            data = json.loads(bytes(exchange.content))
                        except ValueError:
                            data = None
                        if isinstance(data, dict) and status == 202:
                            self._queued(call, data.get("id"))
                        elif isinstance(data, dict):
                            upload_id = self._safe_token(data.get("upload_id"))
                            if call.upload_id is None:
                                call.upload_id = upload_id
                            if data.get("state") == "bound" and upload_id == call.upload_id:
                                self._queued(call, data.get("job_id"))
            if error is None:
                assert response is not None
                if response.status_code < 300 or (storage and response.status_code == 412):
                    return response
                if storage:
                    code = storage_code(response.content)
                    error = UploadError(
                        "Upload storage request failed",
                        status_code=response.status_code,
                        storage_code=self._safe_token(code),
                        retryable=response.status_code in (429, 502, 503, 504),
                    )
                else:
                    error = self._error(response)
                eligible = retry_eligible(
                    response.status_code,
                    error.code,
                    error.retryable,
                    replay_safe,
                    replay_after_send,
                )
            if isinstance(error, AmbiguousSubmissionError):
                raise error
            call.remaining()
            if not eligible or attempt + 1 == policy.max_attempts:
                raise error
            delay = (0.75 + 0.25 * self._random()) * min(
                policy.max_delay, policy.initial_delay * 2.0 ** min(attempt, 1023)
            )
            if response is not None:
                delay = max(delay, self._retry_after(response))
            logger.debug(
                "Retrying %s %s in %.3fs after attempt %d of %d "
                "(status=%s, code=%s, request_id=%s)",
                method,
                _path_template(path, storage),
                delay,
                attempt + 1,
                policy.max_attempts,
                None if response is None else response.status_code,
                error.code,
                error.request_id or (None if storage else call.request_id),
            )
            yield from self._wait(call, delay)
        raise AssertionError("Unreachable retry state")

    def _descriptor(self, data: dict[str, object], label: str) -> bytes:
        body = json.dumps(data, separators=(",", ":")).encode()
        if len(body) > self.limits.descriptor_bytes:
            raise PayloadTooLargeError(f"Encoded {label} descriptor exceeds the configured limit")
        return body

    def _post_json(self, call: _Call, path: str, body: bytes, key: str) -> Flow[httpx.Response]:
        return (
            yield from self._request(
                call,
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json", "Idempotency-Key": key},
            )
        )

    def _upload_json(
        self, call: _Call, path: str, data: dict[str, object], key: str
    ) -> Flow[httpx.Response]:
        return (yield from self._post_json(call, path, self._descriptor(data, "upload"), key))

    def _initialize(self, call: _Call, expected: dict[str, object]) -> Flow[None]:
        call.phase = "upload_init"
        response = yield from self._upload_json(
            call, "/uploads", expected, initialization_key(call.operation_key)
        )
        data = self._json(response)
        upload_id = self._safe_token(data.get("upload_id"))
        if response.status_code not in (200, 201) or upload_id is None:
            raise APIResponseValidationError(
                "Invalid upload initialization response",
                status_code=response.status_code,
                retryable=False,
            )
        if call.upload_id is not None and upload_id != call.upload_id:
            raise UploadError("Upload replay returned a different identifier", retryable=False)
        call.upload_id = upload_id
        grant = Grant.parse(data, expected, response.status_code)
        if call.grant is not None and grant.upload_expires_at != call.grant.upload_expires_at:
            raise UploadError("Upload replay changed the fixed upload window", retryable=False)
        call.grant = grant
        if grant.state == "bound":
            self._queued(call, data.get("job_id"))
        if grant.state in ("expired", "reclaimed"):
            raise UploadError(
                "Upload has expired; recover any accepted job",
                code=_codes.upload_expired.code,
                retryable=False,
            )
        size = expected["size_bytes"]
        assert isinstance(size, int)
        if grant.state == "pending" and size > grant.limits["max_upload_bytes"]:
            raise PayloadTooLargeError("File exceeds the service upload limit", retryable=False)

    def _put(self, call: _Call, body: Multipart, expected: dict[str, object]) -> Flow[None]:

        def target() -> Flow[tuple[str, dict[str, str]] | None]:
            grant = call.grant
            assert grant is not None
            call.phase = "upload_put"
            if grant.state != "pending":
                return None
            if self._wall_clock() >= grant.upload_expires_at:
                raise UploadError(
                    "Upload has expired; recover any accepted job",
                    code=_codes.upload_expired.code,
                    retryable=False,
                )
            if self._wall_clock() >= grant.expires_at:
                yield from self._initialize(call, expected)
                grant = call.grant
                assert grant is not None
                call.phase = "upload_put"
                if grant.state != "pending":
                    return None
                if self._wall_clock() >= min(grant.expires_at, grant.upload_expires_at):
                    raise UploadError(
                        "Upload grant has expired", code=_codes.upload_expired.code, retryable=False
                    )
            assert grant.put_url is not None
            return (grant.put_url, grant.headers)

        yield from self._request(
            call, "PUT", "", body=UploadBody(body), storage=True, before_attempt=target
        )

    def _staged(
        self, call: _Call, body: Multipart, fields: dict[str, str]
    ) -> Flow[Multipart | None]:
        expected = descriptor(body)
        try:
            yield from self._initialize(call, expected)
        except APIError as error:
            cap = error._inline_cap
            if (
                cap is None
                or call.grant is not None
                or call.upload_id is not None
                or len(body.prefix) + body.size + len(body.suffix) > cap
            ):
                raise
            # Nothing was admitted, so the same operation key can submit the body inline.
            inline = Multipart(
                body.source,
                fields,
                body.filename,
                call.remaining,
                cap,
                body.content_type,
                body.part_headers,
            )
            call.read_idle = inline.read_idle
            yield Prepare(inline, call)
            if inline.staged or inline.sha256 != body.sha256:
                raise IntegrityError("File content changed during the operation") from None
            return inline
        yield from self._put(call, body, expected)
        for attempt in range(2):
            call.phase = "submit"
            try:
                response = yield from self._upload_json(
                    call,
                    "/transcription_jobs",
                    {**fields, "upload_id": call.upload_id},
                    call.operation_key,
                )
            except APIError as error:
                if (
                    error.status_code != 409
                    or error.code != _codes.upload_incomplete.code
                    or attempt
                ):
                    raise
            else:
                self._accept(call, response)
                return None
            yield from self._put(call, body, expected)
        return None

    def _options(
        self, model: str, language: str | None, response_format: ResponseFormat
    ) -> dict[str, str]:
        if response_format not in RESPONSE_FORMATS:
            raise ValueError("response_format must be json, text, or verbose_json")
        fields = {"model": model, "response_format": response_format}
        if language is not None:
            if language.lower().split("-", 1)[0] != SERVED_LANGUAGE:
                raise ValueError("Only English language hints are supported")
            fields["language"] = language
        return fields

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        data = _body(response)
        if not isinstance(data, dict):
            raise APIResponseValidationError(
                "Invalid JSON response", status_code=response.status_code, retryable=False
            )
        return data

    def _result(
        self,
        call: _Call,
        data: dict[str, Any] | TranscriptionResult,
        response_format: ResponseFormat,
        status_code: int,
    ) -> TranscriptionResult:
        call.remaining()
        try:
            result = (
                data
                if isinstance(data, TranscriptionResult)
                else TranscriptionResult.model_validate(data)
            )
        except ValidationError:
            raise APIResponseValidationError(
                "Invalid transcription result response", status_code=status_code, retryable=False
            ) from None
        return result.model_copy(
            update={
                "request_id": call.request_id,
                "job_id": call.job_id,
                "elapsed_seconds": self._clock() - call.start,
                "response_format": response_format,
            }
        )

    def _queued(self, call: _Call, job_id: Any) -> None:
        call.job_id = self._safe_token(job_id)
        if call.job_id is not None:
            call.last_status = "queued"

    def _accept(self, call: _Call, response: httpx.Response) -> None:
        data = self._json(response)
        call.job_id = self._safe_token(data.get("id"))
        call.last_status = "queued"
        if response.status_code != 202 or call.job_id is None:
            raise APIResponseValidationError(
                "Invalid job admission response", status_code=response.status_code, retryable=False
            )
        call.remaining()

    def _read_job(self, call: _Call) -> Flow[tuple[JobSnapshot, httpx.Response]]:
        call.phase = "poll"
        assert call.job_id is not None
        response = yield from self._request(
            call, "GET", "/transcription_jobs/" + quote(call.job_id, safe="")
        )
        data = self._json(response)
        invalid = APIResponseValidationError(
            "Invalid job status response", status_code=response.status_code, retryable=False
        )
        try:
            snapshot = JobSnapshot.model_validate(data)
        except ValidationError:
            raise invalid from None
        if snapshot.id != call.job_id:
            raise invalid
        call.last_status = self._safe_token(snapshot.status)
        call.remaining()
        if snapshot.status == "error":
            raise self._error(response, terminal=True)
        return (snapshot, response)

    def _poll(self, call: _Call, response_format: ResponseFormat) -> Flow[TranscriptionResult]:
        """Poll until completion, treating every status other than queued or processing as final.

        Statuses the service adds later are terminal states, so polling stops instead of
        continuing until the deadline. Otherwise only the call deadline, enforced by
        _wait, or an explicit max_polls ends polling.
        """
        observed: str | None = None
        cap = self.retry_policy.max_polls
        for _ in itertools.count() if cap is None else range(cap):
            data, response = yield from self._read_job(call)
            if data.status != observed:
                observed = data.status
                logger.info("Job %s status %s", call.job_id, call.last_status or "unrecognized")
            if data.status == "completed":
                result = data.result
                if result is None:
                    raise APIResponseValidationError(
                        "Job response did not contain a result",
                        status_code=response.status_code,
                        retryable=False,
                    )
                return self._result(call, result, response_format, response.status_code)
            if data.status not in _PENDING_STATUSES:
                status = call.last_status
                raise TerminalJobError(
                    f"Transcription job ended with status {status}"
                    if status is not None
                    else "Transcription job ended with an unrecognized status",
                    status_code=response.status_code,
                    retryable=False,
                    request_id=call.request_id,
                )
            yield from self._wait(
                call, max(self.retry_policy.poll_interval, self._retry_after(response))
            )
        raise DeadlineExceededError("Polling budget exhausted; resume the accepted job")

    def _transcribe_file(
        self,
        call: _Call,
        file: FileInput,
        model: str,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        response_format: ResponseFormat,
        keyed: bool,
        force_staged: bool = False,
    ) -> Flow[TranscriptionResult]:
        fields = self._options(model, language, response_format)
        content, filename, content_type, headers = unpack_file(file, filename, content_type)
        source = yield OpenFile(content, call)
        failed = False
        try:
            body = Multipart(
                source,
                fields,
                filename,
                call.remaining,
                self.limits.job_inline_body_bytes,
                content_type,
                headers,
                lambda: setattr(call, "phase", "upload_init"),
                force_staged,
            )
            call.read_idle = body.read_idle
            yield Prepare(body, call)
            inline = (yield from self._staged(call, body, fields)) if body.staged else body
            if inline is None:
                return (yield from self._poll(call, response_format))
            size = int(inline.headers["Content-Length"])
            asynchronous = (
                self.transport == "job"
                or keyed
                or inline is not body
                or size > self.limits.sync_inline_body_bytes
            )
            body = inline
            if not asynchronous:
                call.phase = "sync_submit"
                refusal: APIStatusError | None = None
                try:
                    response = yield from self._request(
                        call,
                        "POST",
                        "/audio/transcriptions",
                        body=body,
                        headers=body.headers,
                        replay_safe=False,
                        replay_after_send=self.sync_replay == "always",
                    )
                except APIStatusError as error:
                    refusal = error
                if refusal is None:
                    data = (
                        {"text": response.text}
                        if response_format == "text"
                        else self._json(response)
                    )
                    return self._result(call, data, response_format, response.status_code)
                sized = (
                    isinstance(refusal, PayloadTooLargeError)
                    and refusal.code in _SIZE_REFUSAL_CODES
                )
                unadmitted = refusal.code in SYNC_FALLBACK_CODES and refusal.retryable is True
                if not (sized or unadmitted):
                    raise refusal
            call.phase = "job_submit"
            response = yield from self._request(
                call,
                "POST",
                "/transcription_jobs",
                body=body,
                headers={**body.headers, "Idempotency-Key": call.operation_key},
            )
            self._accept(call, response)
        except BaseException:
            failed = True
            raise
        finally:
            yield CloseFile(source)
            if not failed:
                call.remaining()
        return (yield from self._poll(call, response_format))

    def _transcribe_url(
        self,
        call: _Call,
        url: str,
        model: str,
        language: str | None,
        response_format: ResponseFormat,
    ) -> Flow[TranscriptionResult]:
        fields = self._options(model, language, response_format)
        body = self._descriptor({**fields, "url": url}, "URL")
        call.phase = "job_submit"
        response = yield from self._post_json(call, "/transcription_jobs", body, call.operation_key)
        self._accept(call, response)
        return (yield from self._poll(call, response_format))

    def _resume_plan(
        self,
        job_id: str | None,
        file: FileInput | None,
        operation_key: str | None,
        upload_id: str | None,
        model: str | None,
        language: str | None,
        response_format: ResponseFormat | None,
    ) -> tuple[ResponseFormat, dict[str, Any]]:
        if job_id is None and (file is None or model is None or operation_key is None):
            raise ValueError("Recovery before admission requires file, model and operation_key")
        selected: ResponseFormat = (
            response_format
            if response_format is not None
            else "verbose_json"
            if job_id is not None
            else "json"
        )
        self._options(model or "", language, selected)
        return selected, {
            "phase": "poll" if job_id is not None else "upload_init",
            "upload_id": self._job_id(upload_id) if upload_id is not None else None,
            "job_id": self._job_id(job_id) if job_id is not None else None,
        }

    def _resume(
        self,
        call: _Call,
        file: FileInput | None,
        model: str | None,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        response_format: ResponseFormat,
    ) -> Flow[TranscriptionResult]:
        if call.job_id is not None:
            return (yield from self._poll(call, response_format))
        assert file is not None and model is not None
        call.phase = "upload_init"
        return (
            yield from self._transcribe_file(
                call, file, model, filename, content_type, language, response_format, True, True
            )
        )

    def _job_id(self, value: str) -> str:
        token = self._safe_token(value)
        if token is None or token in (".", ".."):
            raise ValueError("Invalid job identifier")
        return token

    def _new_call(
        self,
        timeout: Timeout,
        deadline: float | None,
        key: str | None = None,
        *,
        phase: str = "prepare",
        upload_id: str | None = None,
        job_id: str | None = None,
    ) -> _Call:
        start = self._clock()
        policy = resolve_timeout(timeout, self.timeout)
        budget = policy.deadline if deadline is None else deadline
        positive(budget, "deadline")
        if key is not None and (not key or len(key) > 128 or not re.fullmatch(r"[!-~]+", key)):
            raise ValueError("idempotency_key must contain 1 to 128 visible ASCII characters")
        return _Call(
            start,
            start + budget,
            policy,
            key or uuid.uuid4().hex,
            self._clock,
            phase=phase,
            upload_id=upload_id,
            job_id=job_id,
        )

    def _attach(self, error: BaseException, call: _Call) -> None:
        for name in ("operation_key", "phase", "job_id", "upload_id", "last_status"):
            setattr(error, name, getattr(call, name))
        error.__context__ = None
        error.__cause__ = None
        if isinstance(error, APIError):
            error._sync_replay = self.sync_replay == "always"
            error.request_id = error.request_id or call.request_id
            error._file_released = call.file_released
