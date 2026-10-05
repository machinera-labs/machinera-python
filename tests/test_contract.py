from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.util
import io
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
import pytest
from support import (
    API,
    CREDENTIAL,
    MODEL,
    Clock,
    accepted,
    client,
    completed,
    job_api,
    queued,
    recorder,
)

import machinera as m
from machinera import _codes


@pytest.mark.parametrize(
    "key,environment",
    [(None, None), ("", None)]
    + [(key, CREDENTIAL) for key in ["has space", "\x00", "\x7f", "nonascii-é"]],
)
def test_invalid_credentials_are_rejected(
    monkeypatch: pytest.MonkeyPatch, key: str | None, environment: str | None
) -> None:
    if environment is None:
        monkeypatch.delenv("MACHINERA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MACHINERA_API_KEY", environment)
    with pytest.raises(ValueError, match="api_key"):
        m.Machinera(api_key=key)


def test_environment_and_explicit_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MACHINERA_API_KEY", CREDENTIAL)
    monkeypatch.delenv("MACHINERA_BASE_URL", raising=False)
    with m.Machinera() as sdk:
        assert sdk.base_url == API
    monkeypatch.setenv("MACHINERA_BASE_URL", "https://service.example/")
    with m.Machinera() as sdk:
        assert sdk.base_url == "https://service.example/v1"
        assert sdk._api_key == CREDENTIAL
    with m.Machinera(api_key="explicit", base_url=API + "/") as sdk:
        assert sdk._api_key == "explicit" and sdk.base_url == API
    for options in ({"api_key": ""}, {"base_url": ""}, {"base_url": "ftp://bad.example"}):
        with pytest.raises(ValueError):
            m.Machinera(**options)
    monkeypatch.setenv("MACHINERA_BASE_URL", "")
    with pytest.raises(ValueError):
        m.Machinera()
    monkeypatch.setenv("MACHINERA_API_KEY", "")
    with pytest.raises(ValueError):
        m.Machinera(base_url=API)


TIMEOUTS = [
    ({}, (5, 600, 600, 600), 30, 3600),
    ({"timeout": 7.0}, (7, 7, 7, 7), 30, 3600),
    ({"timeout": httpx.Timeout(None, connect=2, pool=4)}, (2, None, None, 4), 30, 3600),
    ({"timeout": None}, (None, None, None, None), 30, 3600),
    ({"timeout": m.TimeoutPolicy()}, (5, 600, 600, 600), 30, 3600),
    ({"timeout": m.TimeoutPolicy(1, 2, None, 4, 5, 6)}, (1, 2, None, 4), 5, 6),
]


@pytest.mark.parametrize("options,phases,poll,deadline", TIMEOUTS)
def test_constructor_timeout_mapping(
    options: dict[str, Any], phases: tuple[float | None, ...], poll: float, deadline: float
) -> None:
    with client(lambda _: completed(), **options) as sdk:
        policy = sdk.timeout
        assert (policy.connect, policy.write, policy.read, policy.pool) == phases
        assert (policy.poll_request, policy.deadline) == (poll, deadline)


@pytest.mark.parametrize("method", ["transcribe_file", "transcribe_url", "get_job", "resume"])
@pytest.mark.parametrize("options,phases,poll,deadline", TIMEOUTS)
def test_method_timeout_mapping(
    method: str,
    options: dict[str, Any],
    phases: tuple[float | None, ...],
    poll: float,
    deadline: float,
) -> None:
    seen = []

    handler = recorder(job_api(sync=lambda request: httpx.Response(200, json={"text": ""})), seen)

    inherited = m.TimeoutPolicy(10, 11, 12, 13, 17, 19)
    if not options:
        phases, poll, deadline = (10, 11, 12, 13), 17, 19
    elif not isinstance(options["timeout"], m.TimeoutPolicy):
        poll, deadline = 17, 19
    with client(handler, timeout=inherited) as sdk:
        if method == "transcribe_file":
            sdk.transcribe_file(b"fLaC", model=MODEL, **options)
        elif method == "transcribe_url":
            sdk.transcribe_url("https://audio.example/a", model=MODEL, **options)
        else:
            getattr(sdk, method)("job-1", **options)
        assert sdk.timeout is inherited
    for request in seen:
        cap = min(poll, deadline) if request.method == "GET" else deadline
        assert request.extensions["timeout"] == dict(
            zip(
                ("connect", "write", "read", "pool"),
                (None if v is None else min(v, cap) for v in phases),
                strict=True,
            )
        )


@pytest.mark.parametrize("method", ["transcribe_file", "transcribe_url", "resume"])
def test_deadline_override_with_disabled_phases(method: str) -> None:
    clock = Clock()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert all(v is None for v in request.extensions["timeout"].values())
        if request.method == "POST":
            return accepted()
        return queued()

    with client(handler, clock, timeout=None) as sdk:
        with pytest.raises(m.DeadlineExceededError) as caught:
            if method == "resume":
                sdk.resume("job-1", deadline=0.5)
            elif method == "transcribe_file":
                sdk.transcribe_file(b"fLaC", model=MODEL, deadline=0.5, idempotency_key="saved")
            else:
                sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=0.5)
    assert caught.value.job_id == "job-1"
    assert caught.value.retryable is False and calls


@pytest.mark.parametrize(
    "options,attempts",
    [
        ({}, 3),
        ({"max_retries": 0}, 1),
        ({"max_retries": 5}, 6),
        ({"retry_policy": m.RetryPolicy(max_attempts=7)}, 7),
    ],
)
def test_retry_expansion(options: dict[str, Any], attempts: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("unavailable")

    with client(handler, **options) as sdk:
        with pytest.raises(m.APIConnectionError) as caught:
            sdk.get_job("job-1")
        assert sdk.retry_policy.max_attempts == attempts
        if not options:
            assert sdk.retry_policy == m.RetryPolicy(3, 0.5, 8, 1, None)
        assert sdk.limits == m.Limits(25 * 1024**2, 50 * 1024**2, 64 * 1024)
    assert len(calls) == attempts and caught.value.retryable is True


def test_configuration_conflicts() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        client(lambda _: completed(), max_retries=2, retry_policy=m.RetryPolicy())
    with httpx.Client() as http:
        with pytest.raises(ValueError, match="either"):
            client(lambda _: completed(), http_client=http)
    with pytest.raises(TypeError, match="httpx.Client"):
        m.Machinera(api_key=CREDENTIAL, http_client=object())
    with pytest.raises(TypeError):
        m.Machinera(CREDENTIAL, API)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_durations(value: float) -> None:
    with pytest.raises(ValueError):
        client(lambda _: completed(), timeout=value)
    for phase in ("connect", "read", "write", "pool", "poll_request", "deadline"):
        with pytest.raises(ValueError):
            m.TimeoutPolicy(**{phase: value})
    with client(lambda _: pytest.fail("unexpected HTTP")) as sdk:
        for method in (sdk.resume, sdk.transcribe_url, sdk.transcribe_file):
            with pytest.raises(ValueError):
                if method == sdk.resume:
                    method("job-1", deadline=value)
                else:
                    method(b"fLaC", model=MODEL, deadline=value)


@pytest.mark.parametrize("jitter,expected", [(0, [0.375, 0.75, 0.75]), (1, [0.5, 1, 1])])
def test_jitter_and_backoff_cap(jitter: float, expected: list[float]) -> None:
    clock = Clock()
    with m.Machinera(
        api_key=CREDENTIAL,
        clock=clock,
        sleeper=clock.sleep,
        random_source=lambda: jitter,
        retry_policy=m.RetryPolicy(max_attempts=4, max_delay=1),
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    ) as sdk:
        with pytest.raises(m.InternalServerError):
            sdk.get_job("job-1")
    assert clock.sleeps == expected


@pytest.mark.parametrize(
    "name,value",
    [
        (name, "value")
        for name in [
            "AUTHORIZATION",
            "Host",
            "Content-Length",
            "Content-Type",
            "Idempotency-Key",
            "Transfer-Encoding",
            "X-cOnTeNt-Md5",
            "bad name",
            "X-\r\nInjected",
        ]
    ]
    + [("X-Note", value) for value in ["a\r\nb", "a\x00b", "a\x7fb", "é"]],
)
def test_invalid_default_headers(name: str, value: str) -> None:
    with pytest.raises(ValueError):
        client(lambda _: pytest.fail("unexpected HTTP"), default_headers={name: value})


def test_copied_headers_and_injected_client_defaults() -> None:
    seen = []
    headers = {"User-Agent": "custom", "X-Extra": "copied"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": ""})

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=0.01,
        headers={"X-Caller": "unchanged"},
        auth=("user", "pass"),
    ) as http:
        before = dict(http.headers)
        with m.Machinera(api_key=CREDENTIAL, http_client=http, default_headers=headers) as sdk:
            headers["X-Extra"] = "changed"
            sdk.transcribe_file(b"fLaC", model=MODEL)
            with pytest.raises(TypeError):
                sdk.default_headers["X-New"] = "bad"
        assert not http.is_closed and dict(http.headers) == before
    assert seen[0].extensions["timeout"] == {"connect": 5, "read": 600, "write": 600, "pool": 600}
    assert seen[0].headers["user-agent"] == "custom"
    assert seen[0].headers["x-extra"] == "copied"
    assert seen[0].headers["authorization"] == "Bearer " + CREDENTIAL
    assert "x-caller" not in seen[0].headers
    assert "transfer-encoding" not in seen[0].headers
    assert seen[0].headers.get_list("content-length") == [str(len(seen[0].content))]
    assert seen[0].headers.get_list("x-content-md5") == [
        hashlib.md5(seen[0].content, usedforsecurity=False).hexdigest()
    ]


STATUS_ERRORS = [
    (400, m.BadRequestError),
    (401, m.AuthenticationError),
    (403, m.PermissionDeniedError),
    (404, m.NotFoundError),
    (409, m.ConflictError),
    (413, m.PayloadTooLargeError),
    (422, m.UnprocessableEntityError),
    (429, m.RateLimitError),
    (500, m.InternalServerError),
    (503, m.InternalServerError),
    (418, m.APIStatusError),
]


@pytest.mark.parametrize("status,kind", STATUS_ERRORS)
def test_status_errors_and_safe_body(status: int, kind: type[m.APIStatusError]) -> None:
    detail = {
        "code": "test_code",
        "retryable": False,
        "job_id": "job-1",
        "upload_id": "https://secret.example/a",
        "message": "private transcript",
        "authorization": CREDENTIAL,
    }
    with client(
        lambda _: httpx.Response(
            status, json={"error": detail}, headers={"x-request-id": "request-1"}
        )
    ) as sdk:
        with pytest.raises(kind) as caught:
            sdk.get_job("job-1")
    error = caught.value
    assert type(error) is kind and error.status_code == error.status == status
    assert str(error) == error.message + " (request_id: request-1)" and error.retryable is False
    assert error.body == {"code": "test_code", "retryable": False, "job_id": "job-1"}
    assert error.request_id == "request-1" and error.job_id == "job-1"
    assert error.__context__ is error.__cause__ is None
    assert not hasattr(error, "request") and not hasattr(error, "response")
    with pytest.raises(AttributeError):
        error.status = 200


def owned(
    error: m.APIError, caller_key: bool | None, replay: bool = False, *, lost: bool = False
) -> m.APIError:
    error._caller_key = caller_key
    error._sync_replay = replay
    error._submission_lost = lost
    return error


@pytest.mark.parametrize(
    "error,transient",
    [
        (m.APIConnectionError("x"), True),
        (m.APITimeoutError("x", retryable=True), True),
        (m.APIConnectionError("x", retryable=False), False),
        (m.APIConnectionError("x", phase="sync_submit"), False),
        (m.APIConnectionError("x", retryable=True, phase="sync_submit"), True),
        (m.RateLimitError("x", status_code=429, retryable=True), True),
        (m.RateLimitError("x", status_code=429, retryable=True, phase="sync_submit"), True),
        (m.RateLimitError("x", status_code=429, retryable=False), False),
        (m.RateLimitError("x", status_code=429), False),
        (m.InternalServerError("x", status_code=503, retryable=True), True),
        (m.BadRequestError("x", status_code=400, retryable=True), True),
        (m.APIStatusError("x", status_code=418, retryable=True), True),
        (m.AuthenticationError("x", status_code=401, retryable=True), False),
        (m.PermissionDeniedError("x", status_code=403, retryable=True), False),
        (
            m.InternalServerError(
                "x",
                status_code=503,
                code="no_serving_capacity",
                retryable=True,
                phase="sync_submit",
            ),
            True,
        ),
        (
            m.InternalServerError(
                "x", status_code=503, code="input_busy", retryable=True, phase="sync_submit"
            ),
            True,
        ),
        (
            m.InternalServerError(
                "x",
                status_code=503,
                code="no_serving_capacity",
                retryable=False,
                phase="sync_submit",
            ),
            False,
        ),
        (m.InternalServerError("x", status_code=503, retryable=True, phase="sync_submit"), False),
        (m.InternalServerError("x", status_code=503, retryable=False), False),
        (m.InternalServerError("x", status_code=503), False),
        (m.DeadlineExceededError("x", job_id="job-1"), True),
        (m.DeadlineExceededError("x"), False),
        (m.TranscriptionInterrupted("x", job_id="job-1"), False),
        (m.TranscriptionInterrupted("x", ambiguous=True), False),
        (m.AmbiguousSubmissionError("x", job_id="job-1"), False),
        (m.TerminalJobError("x", retryable=True, job_id="job-1"), False),
        (m.TerminalIntegrityError("x", job_id="job-1"), False),
        (m.UploadError("x", retryable=True), False),
        (m.APIResponseValidationError("x"), False),
        (m.APIError("x", retryable=True), False),
        (m.MachineraError("x"), False),
        (owned(m.DeadlineExceededError("x", phase="prepare"), False), True),
        (owned(m.DeadlineExceededError("x", phase="upload_put"), False), False),
        (owned(m.DeadlineExceededError("x", phase="upload_put"), True), True),
        (owned(m.DeadlineExceededError("x", phase="poll", job_id="job-1"), None), True),
        (owned(m.TranscriptionInterrupted("x", phase="prepare"), False), False),
        (owned(m.TranscriptionInterrupted("x", phase="sync_submit"), False, True), False),
        (owned(m.TranscriptionInterrupted("x", phase="job_submit"), True), False),
        (owned(m.TranscriptionInterrupted("x", phase="poll", job_id="job-1"), None), False),
        (owned(m.APITimeoutError("x", retryable=True, phase="submit"), False, lost=True), False),
        (owned(m.APITimeoutError("x", retryable=True, phase="submit"), True, lost=True), True),
        (owned(m.TerminalJobError("x", retryable=True, job_id="job-1"), False, lost=True), True),
        (
            owned(
                m.InternalServerError(
                    "x", status_code=503, retryable=True, phase="poll", job_id="job-1"
                ),
                None,
            ),
            True,
        ),
        (
            owned(
                m.InternalServerError(
                    "x",
                    status_code=503,
                    code="inline_claim_timeout",
                    retryable=True,
                    phase="sync_submit",
                ),
                False,
                replay=True,
            ),
            False,
        ),
    ],
)
def test_is_transient_matrix(error: m.MachineraError, transient: bool) -> None:
    assert error.is_transient is transient


FAILED = {"retryable": True, "job_id": "job-1"}


@pytest.mark.parametrize(
    "cause,transient",
    [
        (FileNotFoundError(2, "missing"), False),
        (PermissionError(13, "denied"), False),
        (httpx.ReadError("reset"), True),
    ],
)
def test_local_failures_are_not_transient(cause: Exception, transient: bool) -> None:
    from machinera._core import local_failure

    error = local_failure(cause)  # type: ignore[arg-type]
    assert isinstance(error, m.APIConnectionError) and error.is_transient is transient


@pytest.mark.parametrize("asynchronous", [False, True])
def test_closed_client_is_not_transient(asynchronous: bool) -> None:
    with client(lambda _: completed(), asynchronous=asynchronous) as sdk:
        sdk.aclose() if asynchronous else sdk.close()
        with pytest.raises(m.APIConnectionError, match="closed") as caught:
            sdk.get_job("job-1")
    assert caught.value.is_transient is False


def test_recoverable_job_errors_share_one_marker() -> None:
    recoverable = (m.DeadlineExceededError, m.TranscriptionInterrupted)
    for cls in recoverable:
        assert issubclass(cls, m.RecoverableJobError) and issubclass(cls, m.APIError)
        assert cls("x").job_id is None and cls("x", job_id="job-1").job_id == "job-1"
    assert issubclass(m.TranscriptionInterrupted, KeyboardInterrupt)
    assert issubclass(m.RecoverableJobError, m.MachineraError)
    for name in m.__all__:
        value = getattr(m, name)
        if isinstance(value, type) and issubclass(value, m.RecoverableJobError):
            assert value in (m.RecoverableJobError, *recoverable), name
    with pytest.raises(m.RecoverableJobError) as caught:
        raise m.TranscriptionInterrupted("stopped", job_id="job-1")
    assert caught.value.job_id == "job-1"


def test_request_id_suffix_only_when_known() -> None:
    error = m.APIStatusError("Request failed")
    assert str(error) == "Request failed" == error.message
    error.request_id = "request-2"
    assert str(error) == "Request failed (request_id: request-2)"
    assert error.message == "Request failed" and error.args == ("Request failed",)
    interrupted = m.TranscriptionInterrupted("stopped", request_id="request-3")
    assert str(interrupted) == "stopped (request_id: request-3)"


def test_non_json_and_local_size_metadata() -> None:
    with client(lambda _: httpx.Response(413, text="<html>private</html>")) as sdk:
        with pytest.raises(m.PayloadTooLargeError) as caught:
            sdk.get_job("job-1")
    assert caught.value.body is None and caught.value.code is None
    with client(
        lambda _: pytest.fail("unexpected HTTP"), limits=m.Limits(descriptor_bytes=1)
    ) as sdk:
        with pytest.raises(m.PayloadTooLargeError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert caught.value.status_code is None and caught.value.body is None


PARENTS = {
    m.APIError: m.MachineraError,
    m.APIStatusError: m.APIError,
    **{cls: m.APIStatusError for _, cls in STATUS_ERRORS if cls is not m.APIStatusError},
    m.APIConnectionError: m.APIError,
    m.APITimeoutError: m.APIConnectionError,
    m.DeadlineExceededError: m.APIError,
    m.AmbiguousSubmissionError: m.APIError,
    m.TerminalJobError: m.APIError,
    m.UploadError: m.APIError,
    m.IntegrityError: m.UploadError,
    m.TranscriptionInterrupted: m.APIError,
}


@pytest.mark.parametrize("kind,parent", PARENTS.items())
def test_exported_error_hierarchy_and_attributes(
    kind: type[m.APIError], parent: type[Exception]
) -> None:
    assert kind.__bases__[0] is parent and kind.__module__ == "machinera._exceptions"
    error = kind(
        "safe",
        status_code=409,
        code="test",
        request_id="r",
        job_id="j",
        upload_id="u",
        operation_key="k",
        phase="poll",
        last_status="queued",
        body={"code": "test"},
        retryable=True,
    )
    assert (error.status_code, error.request_id, error.code) == (409, "r", "test")
    assert (error.job_id, error.upload_id, error.operation_key) == ("j", "u", "k")
    assert (error.phase, error.last_status, error.message) == ("poll", "queued", "safe")
    assert error.retryable is (kind not in (m.DeadlineExceededError, m.AmbiguousSubmissionError))
    default = kind("safe")
    assert default.status is default.body is default.request_id is default.code is None
    assert default.retryable is (
        False if kind in (m.DeadlineExceededError, m.AmbiguousSubmissionError) else None
    )
    assert error.wait_for_file_release(0)


def test_exports_and_exception_module() -> None:
    assert m.MachineraError.__bases__ == (Exception,)
    assert issubclass(m.TranscriptionInterrupted, KeyboardInterrupt)
    assert importlib.util.find_spec("machinera._exceptions") is not None
    for name in m.__all__:
        assert hasattr(m, name) and name not in vars(builtins)
    assert "ValidationError" not in m.__all__ and "TransportError" not in m.__all__
    for cls in (m.DeadlineExceededError, m.AmbiguousSubmissionError):
        assert not issubclass(cls, m.APIConnectionError)
        assert "resume" in (cls.__doc__ or "")


@pytest.mark.parametrize(
    "failure", [httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ReadTimeout, httpx.WriteTimeout]
)
def test_phase_timeout_mapping(failure: type[httpx.TimeoutException]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise failure("private detail")

    with client(handler, max_retries=0) as sdk:
        with pytest.raises(m.APITimeoutError) as caught:
            sdk.get_job("job-1")
    assert caught.value.retryable is True and caught.value.status_code is None


SIGNATURES = [
    (b"RIFF\x00\x00\x00\x00WAVE", "wav"),
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"ID3", "mp3"),
    (b"\xff\xfb\x90\x00", "mp3"),
    (b"\x00\x00\x00\x10ftypM4A \x00\x00\x00\x00", "m4a"),
    (b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00isom", "mp4"),
    (b"\x1a\x45\xdf\xa3\x87\x42\x82\x84webm", "webm"),
]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("data,suffix", SIGNATURES)
@pytest.mark.parametrize("form", ["bytes", "handle"])
def test_signature_selects_the_media_suffix(
    data: bytes, suffix: str, form: str, asynchronous: bool
) -> None:
    handle = io.BytesIO(b"skip" + data)
    handle.seek(4)
    source = data if form == "bytes" else handle

    def handler(request: httpx.Request) -> httpx.Response:
        assert f'filename="upload.{suffix}"'.encode() in request.content
        assert b"Content-Type: application/octet-stream" in request.content
        assert b"\r\n\r\n" + data + b"\r\n--" in request.content
        assert b"skip" not in request.content
        assert int(request.headers["content-length"]) == len(request.content)
        return httpx.Response(200, json={"text": ""})

    with client(handler, asynchronous=asynchronous) as sdk:
        assert sdk.transcribe_file(source, model=MODEL).text == ""
    assert handle.tell() == 4 and not handle.closed


@pytest.mark.parametrize(
    "mime,suffix",
    [
        ("audio/wav", "wav"),
        ("audio/x-wav", "wav"),
        ("audio/flac", "flac"),
        ("audio/x-flac", "flac"),
        ("audio/ogg", "ogg"),
        ("application/ogg", "ogg"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "m4a"),
        ("audio/x-m4a", "m4a"),
        ("video/mp4", "mp4"),
        ("audio/webm", "webm"),
        ("video/webm", "webm"),
        ("Audio/WAV; charset=binary", "wav"),
    ],
)
def test_content_type_selects_the_media_suffix(mime: str, suffix: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert f'filename="upload.{suffix}"'.encode() in request.content
        assert f"Content-Type: {mime}".encode() in request.content
        return httpx.Response(200, json={"text": ""})

    with client(handler) as sdk:
        sdk.transcribe_file((None, b"unrecognized", mime), model=MODEL)
        sdk.transcribe_file(b"unrecognized", model=MODEL, content_type=mime)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "form",
    [
        "unnamed_pair",
        "unnamed_typed",
        "str",
        "path",
        "bytes",
        "handle",
        "pair",
        "triple",
        "quad",
        "tuple_path",
        "tuple_handle",
        "handle_name",
        "numeric_name",
    ],
)
def test_every_file_form_names_the_part(tmp_path: Path, form: str, asynchronous: bool) -> None:
    path = tmp_path / "clip.wav"
    path.write_bytes(b"fLaC")
    handle = io.BytesIO(b"fLaC")
    forms: dict[str, Any] = {
        "unnamed_pair": (None, b"fLaC"),
        "unnamed_typed": (None, b"fLaC", "audio/flac"),
        "str": str(path),
        "path": path,
        "bytes": b"fLaC",
        "handle": handle,
        "pair": ("clip.wav", b"fLaC"),
        "triple": ("clip.wav", b"fLaC", "audio/wav"),
        "quad": ("clip.wav", b"fLaC", "audio/wav", {"X-Part": "value"}),
        "tuple_path": ("clip.wav", path),
        "tuple_handle": ("clip.wav", handle),
        "handle_name": handle,
        "numeric_name": handle,
    }
    if form in ("handle_name", "numeric_name"):
        handle.name = path if form == "handle_name" else 5
    expected = (
        "upload.flac"
        if form in ("handle", "numeric_name", "unnamed_pair", "unnamed_typed")
        else "clip.wav"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert f'filename="{expected}"'.encode() in request.content
        assert b"fLaC" in request.content
        if form == "quad":
            assert b"x-part: value\r\n" in request.content and "x-part" not in request.headers
        return httpx.Response(200, json={"text": ""})

    with client(handler, asynchronous=asynchronous) as sdk:
        sdk.transcribe_file(
            forms[form], model=MODEL, **({"filename": "clip.wav"} if form == "bytes" else {})
        )
    assert handle.tell() == 0 and not handle.closed


@pytest.mark.parametrize("suffix", sorted(m.SUPPORTED_MEDIA_SUFFIXES))
def test_supported_explicit_suffixes(suffix: str) -> None:
    with client(lambda _: httpx.Response(200, json={"text": ""})) as sdk:
        sdk.transcribe_file(b"raw", model=MODEL, filename=f"clip.{suffix.upper()}")


@pytest.mark.parametrize("handle", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "data",
    [
        b"unknown content",
        b"",
        b"raw",
        b"RIF",
        b"RIFFxxxxAVI ",
        b"fLa",
        b"Ogg",
        b"ID",
        b"\xff\xff\xff\xff",
        b"\xff\xe8\x90\x00",
        b"\xff\xfb\x9c\x00",
        b"\x00\x00\x00\x14ftypavif\x00\x00\x00\x00isom",
        b"\x00\x00\x00\x10ftypqt 0000",
        b"\x00\x00\x00\x20ftypmp42",
        b"\x1a\x45\xdf\xa3",
        b"\x1a\x45\xdf\xa3\x8b\x42\x82\x88matroska",
        b"\x1a\x45\xdf\xa3\x8bgarbagewebm",
    ],
)
def test_unrecognized_signature_before_http(data: bytes, asynchronous: bool, handle: bool) -> None:
    source = io.BytesIO(b"skip " + data) if handle else data
    if handle:
        source.seek(5)
    with client(lambda _: pytest.fail("unexpected HTTP"), asynchronous=asynchronous) as sdk:
        with pytest.raises(ValueError) as caught:
            sdk.transcribe_file(source, model=MODEL)
    assert "filename=" in str(caught.value) and "content_type=" in str(caught.value)
    assert all(s in str(caught.value) for s in m.SUPPORTED_MEDIA_SUFFIXES)

    if handle:
        assert source.tell() == 5 and not source.closed


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "source,options",
    [
        (b"unknown", {}),
        (b"RIFF", {}),
        (b"audio", {"filename": "bad.txt"}),
        (b"audio", {"content_type": "unknown/type"}),
        (("a.wav", b"audio"), {"filename": "b.wav"}),
        (b"fLaC", {"filename": "clip.bin"}),
        (b"fLaC", {"content_type": "application/octet-stream"}),
        (("a.wav", b"fLaC"), {"filename": "b.wav"}),
        ((None, b"fLaC", "audio/wav"), {"content_type": "audio/flac"}),
        (b"fLaC", {"filename": 'a".wav'}),
        (b"fLaC", {"filename": "a\r\n.wav"}),
        (b"fLaC", {"content_type": "audio/wav\r\nX: bad"}),
    ],
)
def test_invalid_metadata_before_http(
    source: Any, options: dict[str, Any], asynchronous: bool
) -> None:
    with client(lambda _: pytest.fail("unexpected HTTP"), asynchronous=asynchronous) as sdk:
        with pytest.raises(ValueError):
            sdk.transcribe_file(source, model=MODEL, **options)


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "Cookie",
        "Content-Type",
        "Content-Length",
        "Content-Disposition",
        "Transfer-Encoding",
        "X-\nBad",
    ],
)
def test_reject_part_framing_and_sensitive_headers(header: str) -> None:
    with client(lambda _: pytest.fail("unexpected HTTP")) as sdk:
        with pytest.raises(ValueError):
            sdk.transcribe_file(("clip.wav", b"data", None, {header: "bad"}), model=MODEL)


def test_metadata_precedence_and_matching_tuple_values(tmp_path: Path) -> None:
    path = tmp_path / "original.bin"
    path.write_bytes(b"fLaC")
    names = []

    def handler(request: httpx.Request) -> httpx.Response:
        names.append(request.content)
        return httpx.Response(200, json={"text": ""})

    with client(handler) as sdk:
        sdk.transcribe_file(path, model=MODEL, filename="chosen.wav", content_type="audio/flac")
        sdk.transcribe_file(
            ("chosen.wav", path, "audio/flac"),
            model=MODEL,
            filename="chosen.wav",
            content_type="audio/flac",
        )
    assert all(b'filename="chosen.wav"' in body for body in names)
    assert all(b"Content-Type: audio/flac" in body for body in names)


@pytest.mark.parametrize("timeout", [httpx.Timeout(-1), httpx.Timeout(float("inf")), True, "10"])
def test_invalid_timeout_forms(timeout: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        client(lambda _: completed(), timeout=timeout)
    with client(lambda _: pytest.fail("unexpected HTTP")) as sdk:
        with pytest.raises((ValueError, TypeError)):
            sdk.resume("job-1", timeout=timeout)


@pytest.mark.parametrize("form", ["path", "handle"])
@pytest.mark.parametrize("suffix", ["", ".bin"])
@pytest.mark.parametrize("content_type", [None, "audio/wav"])
def test_unsupported_derived_names_use_metadata_or_signature(
    tmp_path: Path, form: str, suffix: str, content_type: str | None
) -> None:
    data = b"RIFF\x00\x00\x00\x00WAVE" if content_type is None else b"opaque media"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert b'filename="upload.wav"' in request.content
        assert b"\r\n\r\n" + data + b"\r\n--" in request.content
        assert int(request.headers["content-length"]) == len(request.content)
        return httpx.Response(200, json={"text": ""})

    with NamedTemporaryFile(dir=tmp_path, suffix=suffix) as handle:
        handle.write(data)
        handle.flush()
        handle.seek(0)
        source = Path(handle.name) if form == "path" else handle
        with client(handler) as sdk:
            sdk.transcribe_file(source, model=MODEL, content_type=content_type)
        assert not handle.closed and handle.tell() == 0
    assert len(requests) == 1


@pytest.mark.parametrize("name", ["clip", "clip.bin"])
@pytest.mark.parametrize("form", ["keyword", "tuple"])
@pytest.mark.parametrize("content_type", [None, "audio/wav"])
def test_unsupported_explicit_names_never_fall_back(
    name: str, form: str, content_type: str | None
) -> None:
    data = b"RIFF\x00\x00\x00\x00WAVE"
    with client(lambda _: pytest.fail("unexpected HTTP")) as sdk:
        with pytest.raises(ValueError, match="accepted suffixes"):
            sdk.transcribe_file(
                (name, data) if form == "tuple" else data,
                model=MODEL,
                filename=name if form == "keyword" else None,
                content_type=content_type,
            )


def test_every_referenced_error_code_exists() -> None:
    for path in Path(m.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "_codes"
            ):
                assert hasattr(_codes, node.attr), f"{path.name}:{node.lineno}: {node.attr}"


def test_unrecognized_content_type_is_rejected_only_for_unnamed_input() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "hi"})

    WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(64)
    with m.Machinera(api_key="key", transport=httpx.MockTransport(handler)) as client:
        unknown = "application/octet-stream"
        named = client.transcribe_file(
            ("clip.wav", WAV), model="transcribe-v1", content_type=unknown
        )
        assert named.text == "hi"
        assert client.transcribe_file(WAV, model="transcribe-v1").text == "hi"
        assert len(requests) == 2
        # The same WAV bytes, unnamed: the unknown type is not replaced by signature inspection.
        with pytest.raises(ValueError):
            client.transcribe_file(WAV, model="transcribe-v1", content_type=unknown)
        assert len(requests) == 2


INVALID_CONFIG = [
    lambda: m.RetryPolicy(max_attempts=0),
    lambda: m.RetryPolicy(max_delay=0.1),
    lambda: m.TimeoutPolicy(deadline=float("inf")),
    lambda: m.TimeoutPolicy(read=0),
    lambda: m.Limits(2, 1),
    lambda: m.Machinera(api_key=CREDENTIAL, base_url="https://api.example/v2"),
    lambda: m.Machinera(api_key=CREDENTIAL, base_url="https://api.example/v1?key=x"),
    lambda: m.Machinera(api_key=CREDENTIAL, base_url="https://user:pass@api.example/v1"),
    lambda: m.Machinera(api_key=CREDENTIAL, base_url="ftp://api.example/v1"),
    lambda: client(lambda _: completed(), max_retries=-1),
    lambda: client(lambda _: completed(), max_retries=1.5),
    lambda: client(lambda _: completed(), max_retries=True),
    lambda: client(lambda _: completed(), max_retries=None),
    lambda: m.RetryPolicy(**{"max_attempts": True}),
    lambda: m.RetryPolicy(**{"max_attempts": 1.5}),
    lambda: m.RetryPolicy(**{"max_polls": 0}),
    lambda: m.RetryPolicy(**{"max_polls": True}),
    lambda: m.RetryPolicy(**{"initial_delay": 0}),
    lambda: m.RetryPolicy(**{"max_delay": float("inf")}),
    lambda: m.RetryPolicy(**{"poll_interval": -1}),
    lambda: m.RetryPolicy(**{"initial_delay": 2, "max_delay": 1}),
]


@pytest.mark.parametrize("factory", INVALID_CONFIG)
def test_invalid_configuration_is_rejected(factory: Any) -> None:
    with pytest.raises(ValueError):
        factory()
