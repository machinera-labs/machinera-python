from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import math
import os
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from functools import wraps
from types import MappingProxyType
from typing import Any, BinaryIO, ClassVar, Literal, ParamSpec, TypeVar, cast, get_args
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from ._contract import (
    DEFAULT_MULTIPART_CAP_BYTES,
    ERROR_CODES,
    IDEMPOTENCY_KEY_HEADER,
    PENDING_JOB_STATUSES,
    RESPONSE_FORMATS,
    RETRY_AFTER_HEADER,
    RETRYABLE_CODES,
    SERVED_LANGUAGE,
    SYNC_ACCEPTANCE_AMBIGUOUS_CODES,
    SYNC_CAP_FALLBACK_CODES,
    SYNC_FALLBACK_CODES,
    UPLOAD_ERROR_CODES,
    UPLOAD_EXPIRED_CODES,
    UPLOAD_INCOMPLETE_CODES,
    UPLOAD_INTEGRITY_CODES,
    UPLOADS_UNAVAILABLE_CODES,
)
from ._exceptions import (
    AmbiguousSubmissionError,
    APIConnectionError,
    APIError,
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
    TranscriptionInterrupted,
    UnprocessableEntityError,
    UploadError,
    connection_replayable,
    expired_upload,
    explicit_guidance,
    invalid_response,
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
from ._uploads import (
    Grant,
    UploadPhase,
    descriptor,
    initialization_key,
    replacement_key,
    storage_code,
)
from ._version import __version__

_SIZE_REFUSAL_CODES = {None, *SYNC_CAP_FALLBACK_CODES}
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


class _Interrupt(Exception):
    """Carry keyboard interruption through child tasks without stopping the event loop."""


@contextmanager
def _sanitize_errors() -> Iterator[None]:
    failure: APIError | ValueError | TypeError
    try:
        yield
    except (APIError, ValueError, TypeError) as error:
        failure = error
    else:
        return
    failure.__context__ = None
    failure.__cause__ = None
    raise failure from None


def _sanitized(function: Callable[_P, _T]) -> Callable[_P, _T]:
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def invoke_async(*args: _P.args, **kwargs: _P.kwargs) -> Any:
            with _sanitize_errors():
                return await function(*args, **kwargs)

        return cast(Callable[_P, _T], invoke_async)

    @wraps(function)
    def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        with _sanitize_errors():
            return function(*args, **kwargs)

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
    observed_status: str | None = None
    observed_since: float | None = None
    file_released: threading.Event | None = None
    read_idle: threading.Event | None = None
    # True when the caller supplied the key of a submitting call, False when the SDK
    # generated it, None for calls that never submit under it.
    caller_key: bool | None = None
    # True once a job submission was sent and its response lost: the service may have
    # accepted a job whose ID the SDK never saw.
    submission_lost: bool = False
    key_rotated: bool = False
    interrupted: Callable[[], None] = lambda: None

    def remaining(self) -> float:
        self.interrupted()
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
# keeps no idle connection that could be reused afterwards. Both pools are shared by
# every client in the process, so neither caps total connections; max_concurrency
# bounds each client.
EXCHANGE_POOL = httpx.Limits(max_connections=None, max_keepalive_connections=0, keepalive_expiry=5)
# Job status polls are small GETs that reuse connections; keep-alive values are httpx's
# DEFAULT_LIMITS.
POLL_POOL = httpx.Limits(max_connections=None, max_keepalive_connections=20, keepalive_expiry=5)


@dataclass(frozen=True, init=False, repr=False, eq=False)
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
        transport: Literal["auto", "job"] | httpx.BaseTransport | httpx.AsyncBaseTransport = "auto",
        sync_replay: str = "never",
        http_client: httpx.Client | httpx.AsyncClient | None = None,
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
            raise ValueError("base_url must be an HTTP(S) API address, optionally followed by /v1")
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

        def owned(role: str) -> Any:
            # Cheap wrappers: the shared pool and its TLS context are created on first use.
            return self._client_type(
                transport=transport if injected else self._shared_transport(role),
                timeout=None,
                follow_redirects=False,
                trust_env=False,
            )

        http = http_client or owned("exchange")
        object.__setattr__(self, "_http", http)
        object.__setattr__(
            self,
            "_poll_http",
            http if http_client is not None or injected else owned("poll"),
        )
        object.__setattr__(self, "_owns_http", http_client is None)

    def _shared_transport(self, role: str) -> Any:
        raise NotImplementedError

    def _safe_token(self, value: Any) -> str | None:
        token = _token(value)
        return token if token is not None and self._api_key not in token else None

    def _retry_after(self, response: httpx.Response) -> float:
        for name, scale in (("retry-after-ms", 1000.0), (RETRY_AFTER_HEADER, 1.0)):
            seconds = _seconds(response.headers.get(name), scale)
            if seconds is not None:
                return seconds
        value = response.headers.get(RETRY_AFTER_HEADER, "")
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
        value = detail.get("code")
        if value is not None and type(value) is not int:
            raise invalid_response("Invalid error code", status_code=response.status_code)
        code = value if type(value) is int else None
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
        elif code in UPLOAD_ERROR_CODES and status != 429:
            cls = UploadError
        if code in UPLOAD_INTEGRITY_CODES:
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
        if code in SYNC_ACCEPTANCE_AMBIGUOUS_CODES:
            message = (
                "Synchronous processing may have started; contact support before resubmitting. "
                "Resubmitting can duplicate processing and charges"
            )
        multipart_cap: int | None = None
        if code in UPLOADS_UNAVAILABLE_CODES:
            message = (
                "File uploads are unavailable, and the body cannot be sent as multipart: it "
                "exceeds the service multipart limit or an upload was already granted"
            )
            limits = detail.get("limits")
            if not isinstance(limits, dict) and isinstance(data, dict):
                limits = data.get("limits")
            cap = limits.get("async_inline_body_bytes") if isinstance(limits, dict) else None
            multipart_cap = cap if type(cap) is int and cap > 0 else DEFAULT_MULTIPART_CAP_BYTES
        error = cls(
            message,
            status_code=status,
            body=body,
            code=code,
            retryable=retryable,
            request_id=self._safe_token(response.headers.get("x-request-id")),
        )
        error._multipart_cap = multipart_cap
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
                call.interrupted()
                if self._clock() >= call.end:
                    raise expired()
                if method == "GET" and self._clock() >= request_end:
                    raise httpx.ReadTimeout("Poll request timeout exceeded")

            exchange = Send(request, body, min(cap, call.remaining()), expired, check, storage)
            started = self._clock()
            phase = call.phase
            try:
                response = yield exchange
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                # Nothing was sent before a connect or pool failure, so it is always replay-safe.
                unsent = isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                )
                if not unsent and method == "POST" and path == "/transcription_jobs":
                    call.submission_lost = True
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
                size = (
                    body.body.size
                    if isinstance(body, UploadBody)
                    and response is not None
                    and 200 <= response.status_code < 300
                    else None
                )
                self._log_timing(call, phase, started, size=size)
            if error is None:
                assert response is not None
                if response.status_code < 300 or (storage and response.status_code == 412):
                    return response
                if storage:
                    code = storage_code(response.content)
                    eligible = (
                        response.status_code == 403
                        and call.grant is not None
                        and self._wall_clock() >= call.grant.expires_at
                    )
                    error = UploadError(
                        "Upload storage request failed",
                        status_code=response.status_code,
                        storage_code=self._safe_token(code),
                        retryable=eligible or response.status_code in (429, 502, 503, 504),
                    )
                else:
                    error = self._error(response)
                eligible = eligible or retry_eligible(
                    response.status_code,
                    error.code,
                    error.retryable,
                    replay_safe,
                    replay_after_send,
                    explicit_guidance(error.body),
                )
            if isinstance(error, AmbiguousSubmissionError):
                raise error
            # Keep the definitive refusal available even at the operation deadline.
            if (
                call.phase == "submit"
                and error.status_code == 410
                and error.code in UPLOAD_EXPIRED_CODES
            ):
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

    def _log_timing(
        self, call: _Call, phase: str, started: float, *, size: int | None = None
    ) -> None:
        seconds = self._clock() - started
        transfer = ""
        if size is not None:
            megabytes = size / 1_000_000
            rate = f"{megabytes / seconds:.2f} MB/s" if seconds > 0 else "rate unavailable"
            transfer = f" {size} bytes {megabytes:.1f} MB ({rate})"
        logger.info(
            "%s %.1fs%s request_id=%s job_id=%s",
            phase,
            seconds,
            transfer,
            call.request_id,
            call.job_id,
        )

    def _observe_job(self, call: _Call, status: str) -> None:
        if status == call.observed_status:
            return
        if call.observed_status in PENDING_JOB_STATUSES and call.observed_since is not None:
            self._log_timing(call, "job " + call.observed_status, call.observed_since)
        call.observed_status = status
        call.observed_since = self._clock()

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
                headers={"Content-Type": "application/json", IDEMPOTENCY_KEY_HEADER: key},
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
            raise invalid_response(
                "Invalid upload initialization response",
                status_code=response.status_code,
            )
        if call.upload_id is not None and upload_id != call.upload_id:
            raise UploadError("Upload replay returned a different identifier", retryable=False)
        call.upload_id = upload_id
        grant = Grant.parse(data, expected, response.status_code)
        if call.grant is not None and grant.upload_deadline != call.grant.upload_deadline:
            raise UploadError("Upload replay changed the fixed upload deadline", retryable=False)
        if (
            call.grant is not None
            and call.grant.submit_expires_at is not None
            and grant.submit_expires_at != call.grant.submit_expires_at
        ):
            raise UploadError("Upload replay changed the submission deadline", retryable=False)
        call.grant = grant
        if grant.state == "bound" or (grant.state == "pending" and grant.put_url is None):
            self._queued(call, data.get("job_id"))
        if grant.state == "expired":
            raise expired_upload()
        size = expected["size_bytes"]
        assert isinstance(size, int)
        if grant.put_url is not None and size > grant.limits["max_upload_bytes"]:
            raise PayloadTooLargeError("File exceeds the service upload limit", retryable=False)

    def _put(self, call: _Call, body: Multipart, expected: dict[str, object]) -> Flow[None]:

        def target() -> Flow[tuple[str, dict[str, str]] | None]:
            grant = call.grant
            assert grant is not None
            call.phase = "upload_put"
            if grant.state != "pending" or grant.put_url is None:
                return None
            if self._wall_clock() > grant.upload_deadline:
                raise expired_upload()
            if self._wall_clock() >= grant.expires_at:
                yield from self._initialize(call, expected)
                grant = call.grant
                assert grant is not None
                call.phase = "upload_put"
                if grant.state != "pending" or grant.put_url is None:
                    return None
                if (
                    self._wall_clock() >= grant.expires_at
                    or self._wall_clock() > grant.upload_deadline
                ):
                    raise expired_upload("Upload grant has expired")
            assert grant.put_url is not None
            return (grant.put_url, grant.headers)

        yield from self._request(
            call, "PUT", "", body=UploadBody(body), storage=True, before_attempt=target
        )

    def _file_upload(
        self, call: _Call, body: Multipart, fields: dict[str, str]
    ) -> Flow[Multipart | None]:
        expected = descriptor(body)
        confirm_only = False
        try:
            yield from self._initialize(call, expected)
        except APIError as error:
            if error.code in UPLOAD_EXPIRED_CODES and call.upload_id is not None:
                # Initialization expiry cannot resolve a lost submission response.
                confirm_only = True
            else:
                cap = error._multipart_cap
                if (
                    cap is None
                    or call.grant is not None
                    or call.upload_id is not None
                    or len(body.prefix) + body.size + len(body.suffix) > cap
                ):
                    raise
                # Nothing was accepted, so the same operation key can submit the body as multipart.
                multipart = Multipart(
                    body.source,
                    fields,
                    body.filename,
                    call.remaining,
                    cap,
                    body.content_type,
                    body.part_headers,
                )
                call.read_idle = multipart.read_idle
                yield Prepare(multipart, call)
                if multipart.file_upload or multipart.sha256 != body.sha256:
                    raise IntegrityError("File content changed during the operation") from None
                return multipart
        upload_seconds = (
            0.0 if confirm_only else (yield from self._finish_upload(call, body, expected))
        )
        replacements = 0
        incomplete_retry = False
        while True:
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
                    error.status_code == 410
                    and error.code in UPLOAD_EXPIRED_CODES
                    and call.job_id is None
                ):
                    # A completed expiry refusal resolves any earlier lost response.
                    call.submission_lost = False
                    upload_seconds = yield from self._replace_upload(
                        call, body, expected, replacements, upload_seconds
                    )
                    replacements += 1
                    incomplete_retry = False
                    continue
                if (
                    error.status_code != 409
                    or error.code not in UPLOAD_INCOMPLETE_CODES
                    or incomplete_retry
                ):
                    raise
                incomplete_retry = True
            else:
                self._accept(call, response)
                return None
            upload_seconds = yield from self._finish_upload(call, body, expected, refresh=True)

    def _finish_upload(
        self, call: _Call, body: Multipart, expected: dict[str, object], *, refresh: bool = False
    ) -> Flow[float]:
        started = self._clock()
        try:
            if refresh:
                yield from self._initialize(call, expected)
            yield from self._put(call, body, expected)
        except APIError as error:
            if error.code not in UPLOAD_EXPIRED_CODES or call.upload_id is None:
                raise
            # Confirm acceptance with the original submission before replacing expired input.
        return self._clock() - started

    def _replace_upload(
        self,
        call: _Call,
        body: Multipart,
        expected: dict[str, object],
        replacements: int,
        upload_seconds: float,
    ) -> Flow[float]:
        advice = (
            "Start a new call with a fresh operation key and a longer deadline "
            "or lower upload concurrency."
        )
        policy = self.retry_policy
        if replacements + 1 >= policy.max_attempts:
            raise expired_upload("Upload expiry retry budget exhausted. " + advice, status_code=410)
        delay = (0.75 + 0.25 * self._random()) * min(
            policy.max_delay, policy.initial_delay * 2.0 ** min(replacements, 1023)
        )
        try:
            if delay + upload_seconds >= call.remaining():
                raise DeadlineExceededError("Insufficient time for another upload")
            yield from self._wait(call, delay)
            assert call.upload_id is not None
            call.operation_key = replacement_key(call.operation_key, call.upload_id)
            call.key_rotated = True
            call.upload_id = None
            call.grant = None
            yield from self._initialize(call, expected)
            return (yield from self._finish_upload(call, body, expected))
        except DeadlineExceededError:
            if call.job_id is not None:
                raise
            raise expired_upload(
                "Upload expiry recovery cannot fit the operation deadline. " + advice
            ) from None

    def _options(
        self, model: str, language: str | None, response_format: ResponseFormat
    ) -> dict[str, str]:
        if response_format not in RESPONSE_FORMATS:
            raise ValueError(
                "response_format must be one of: " + ", ".join(sorted(RESPONSE_FORMATS))
            )
        fields = {"model": model, "response_format": response_format}
        if language is not None:
            if language.lower().split("-", 1)[0] != SERVED_LANGUAGE:
                raise ValueError(
                    f"language must be {SERVED_LANGUAGE!r} or a {SERVED_LANGUAGE}-* tag"
                )
            fields["language"] = language
        return fields

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        data = _body(response)
        if not isinstance(data, dict):
            raise invalid_response("Invalid JSON response", status_code=response.status_code)
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
            raise invalid_response(
                "Invalid transcription result response", status_code=status_code
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
            raise invalid_response(
                "Invalid job acceptance response", status_code=response.status_code
            )
        call.remaining()

    def _read_job(self, call: _Call) -> Flow[tuple[JobSnapshot, httpx.Response]]:
        call.phase = "poll"
        assert call.job_id is not None
        response = yield from self._request(
            call, "GET", "/transcription_jobs/" + quote(call.job_id, safe="")
        )
        data = self._json(response)
        invalid = invalid_response("Invalid job status response", status_code=response.status_code)
        try:
            snapshot = JobSnapshot.model_validate(data)
        except ValidationError:
            raise invalid from None
        if snapshot.id != call.job_id:
            raise invalid
        call.last_status = self._safe_token(snapshot.status)
        self._observe_job(call, snapshot.status)
        call.remaining()
        if snapshot.status == "error":
            raise self._error(response, terminal=True)
        return (snapshot, response)

    def _poll(self, call: _Call, response_format: ResponseFormat) -> Flow[TranscriptionResult]:
        """Poll with the stopping and recovery rules in api.md#retrypolicy."""
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
                    raise invalid_response(
                        "Job response did not contain a result",
                        status_code=response.status_code,
                    )
                return self._result(call, result, response_format, response.status_code)
            if data.status not in PENDING_JOB_STATUSES:
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
        force_file_upload: bool = False,
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
                self.limits.job_multipart_body_bytes,
                content_type,
                headers,
                lambda: setattr(call, "phase", "upload_init"),
                force_file_upload,
            )
            call.read_idle = body.read_idle
            yield Prepare(body, call)
            multipart = (
                (yield from self._file_upload(call, body, fields)) if body.file_upload else body
            )
            if multipart is None:
                return (yield from self._poll(call, response_format))
            size = int(multipart.headers["Content-Length"])
            asynchronous = (
                self.transport == "job"
                or keyed
                or multipart is not body
                or size > self.limits.sync_inline_body_bytes
            )
            body = multipart
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
                unaccepted = refusal.code in SYNC_FALLBACK_CODES
                if not (sized or unaccepted):
                    raise refusal
                logger.info(
                    "sync_submit fallback to job reason_code=%s request_id=%s job_id=%s",
                    refusal.code,
                    call.request_id,
                    call.job_id,
                )
            call.phase = "job_submit"
            response = yield from self._request(
                call,
                "POST",
                "/transcription_jobs",
                body=body,
                headers={**body.headers, IDEMPOTENCY_KEY_HEADER: call.operation_key},
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
            raise ValueError("Recovery before acceptance requires file, model and operation_key")
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

    def _closed_error(self, call: _Call) -> APIConnectionError:
        error = APIConnectionError("Client is closed")
        error._local = True
        self._attach(error, call)
        return error

    def _classify(
        self, error: BaseException, call: _Call, key: str | None, *, blocking: bool = False
    ) -> BaseException | None:
        if isinstance(error, APIError):
            return error
        if isinstance(error, (ValueError, TypeError)):
            if call.phase not in get_args(UploadPhase):
                return None
            if blocking:
                for name, value in (
                    ("operation_key", call.operation_key),
                    ("upload_id", call.upload_id),
                    ("phase", call.phase),
                    ("job_id", call.job_id),
                ):
                    setattr(error, name, value)
                return None
            return error
        if isinstance(error, KeyboardInterrupt) or (not blocking and isinstance(error, _Interrupt)):
            return TranscriptionInterrupted(
                "Transcription interrupted; use recovery context",
                ambiguous=key is None and call.phase == "sync_submit",
            )
        if isinstance(error, (OSError, httpx.HTTPError)):
            return local_failure(error)
        if not blocking and isinstance(error, asyncio.CancelledError):
            return error
        return None

    def _new_call(
        self,
        timeout: Timeout,
        deadline: float | None,
        key: str | None = None,
        *,
        phase: str = "prepare",
        upload_id: str | None = None,
        job_id: str | None = None,
        caller_key: bool | None | Unset = UNSET,
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
            caller_key=key is not None if isinstance(caller_key, Unset) else caller_key,
        )

    def _attach(self, error: BaseException, call: _Call) -> None:
        for name in ("operation_key", "phase", "job_id", "upload_id", "last_status"):
            setattr(error, name, getattr(call, name))
        error.__context__ = None
        error.__cause__ = None
        if isinstance(error, APIError):
            error._sync_replay = self.sync_replay == "always"
            error._caller_key = call.caller_key
            error._submission_lost = call.submission_lost
            error._key_rotated = call.key_rotated
            error.request_id = error.request_id or call.request_id
            error._file_released = call.file_released
