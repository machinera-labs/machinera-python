from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import io
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncMachinera,
    AuthenticationError,
    ConflictError,
    DeadlineExceededError,
    IntegrityError,
    InternalServerError,
    Limits,
    Machinera,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    RecoverableJobError,
    RetryPolicy,
    TerminalJobError,
    TimeoutPolicy,
    TranscriptionInterrupted,
    UnprocessableEntityError,
    UploadError,
    __version__,
)

API = "https://api.machinera.com/v1"
MODEL = "transcribe-v1"
CREDENTIAL = "test-credential"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def result(text: str = "  exact\ntext  ") -> dict[str, Any]:
    return {
        "task": "transcribe",
        "language": "en",
        "duration": 2.5,
        "inference_seconds": 0.2,
        "text": text,
        "words": [],
        "segments": [],
        "warnings": [{"code": "quality_notice", "extra": {"keep": [None, ""]}}],
        "usage": {"type": "duration", "seconds": 2.5},
    }


def completed(text: str = "  exact\ntext  ", job: str = "job-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": job, "status": "completed", "result": result(text)},
        headers={"x-request-id": "request-1"},
    )


class Blocking:
    """Drive an AsyncMachinera with the blocking client's call shape on a private loop."""

    def __init__(self, sdk: AsyncMachinera) -> None:
        self.sdk = sdk
        self.loop = asyncio.new_event_loop()

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.sdk, name)
        if not inspect.iscoroutinefunction(value):
            return value
        return lambda *args, **kwargs: self.run(value(*args, **kwargs))

    def run(self, call: Awaitable[Any]) -> Any:
        async def outcome() -> tuple[Any, BaseException | None]:
            try:
                return await call, None
            except BaseException as error:
                return None, error

        value, error = self.loop.run_until_complete(outcome())
        if error is not None:
            raise error
        return value

    def __enter__(self) -> Blocking:
        return self

    def __exit__(self, *args: object) -> None:
        try:
            self.run(self.sdk.aclose())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
        finally:
            self.loop.close()


def open_client(
    asynchronous: bool, sleeper: Callable[[float], None] | None = None, **kwargs: Any
) -> Any:
    if sleeper is not None and asynchronous:

        async def sleep(delay: float) -> None:
            sleeper(delay)

        kwargs["sleeper"] = sleep
    elif sleeper is not None:
        kwargs["sleeper"] = sleeper
    return Blocking(AsyncMachinera(**kwargs)) if asynchronous else Machinera(**kwargs)


def client(
    handler: Any, clock: Clock | None = None, *, asynchronous: bool = False, **kwargs: Any
) -> Any:
    clock = clock or Clock()
    return open_client(
        asynchronous,
        clock.sleep,
        api_key=CREDENTIAL,
        base_url=API,
        transport=httpx.MockTransport(handler),
        clock=clock,
        wall_clock=lambda: 1_700_000_000,
        random_source=lambda: 0.5,
        **kwargs,
    )


@pytest.mark.parametrize("text", ["  exact\ntext  ", ""])
@pytest.mark.parametrize("fmt", ["json", "text", "verbose_json"])
def test_sync_preserves_text_and_headers(text: str, fmt: Any) -> None:
    audio = io.BytesIO(b"skip-audio-bytes")
    audio.seek(5)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url == API + "/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer " + CREDENTIAL
        assert request.headers["user-agent"] == "machinera-python/" + __version__
        assert int(request.headers["content-length"]) == len(request.content)
        assert request.headers["x-content-md5"] == hashlib.md5(request.content).hexdigest()
        assert b"audio-bytes" in request.content and b"skip-" not in request.content
        if fmt == "text":
            return httpx.Response(200, text=text, headers={"x-request-id": "request-1"})
        return httpx.Response(200, json=result(text), headers={"x-request-id": "request-1"})

    with client(handler) as sdk:
        output = sdk.transcribe_file(
            audio, model=MODEL, response_format=fmt, content_type="audio/wav"
        )
    assert output.text == text
    assert output.to_text() == text
    assert output.request_id == "request-1"
    assert output.job_id is None
    assert len(calls) == 1
    assert not audio.closed and audio.tell() == 5
    if fmt != "text":
        assert output.warnings == result(text)["warnings"]
        assert output.warnings is output.raw["warnings"]
        assert output.to_json() == {"text": text, "usage": result()["usage"]}
        assert output.to_verbose_json() == result(text)
    assert (output.output == text) if fmt == "text" else isinstance(output.output, dict)


@pytest.mark.parametrize("source", ["file", "url"])
@pytest.mark.parametrize("fmt", ["json", "text", "verbose_json"])
def test_job_submission_poll_and_projection(source: str, fmt: Any) -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/v1/transcription_jobs"
            assert request.headers["idempotency-key"] == "saved-operation"
            if source == "url":
                assert (
                    json.loads(request.content)["url"] == "https://audio.example/sample?token=value"
                )
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if len(requests) == 2:
            return httpx.Response(
                200, json={"id": "job-1", "status": "processing"}, headers={"Retry-After": "3"}
            )
        return completed("")

    with client(handler, clock) as sdk:
        submit = sdk.transcribe_file if source == "file" else sdk.transcribe_url
        value = (
            io.BytesIO(b"audio") if source == "file" else "https://audio.example/sample?token=value"
        )
        output = submit(
            value,
            model=MODEL,
            response_format=fmt,
            idempotency_key="saved-operation",
            **({"content_type": "audio/wav"} if source == "file" else {}),
        )
    assert output.text == "" and output.job_id == "job-1"
    assert output.warnings == result()["warnings"]
    assert output.elapsed_seconds == 3
    assert clock.sleeps == [3]
    assert len(requests) == 3
    assert output.output == (
        "" if fmt == "text" else output.to_json() if fmt == "json" else result("")
    )


def test_get_job_and_resume_only_read() -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        paths.append(request.url.path)
        return completed()

    with client(handler) as sdk:
        assert sdk.get_job("job-1").status == "completed"
        assert sdk.resume("job-1").text == result()["text"]
    assert paths == ["/v1/transcription_jobs/job-1"] * 2


def test_encoded_length_selects_inline_path() -> None:
    lengths = []
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "GET":
            return completed()
        lengths.append(len(request.content))
        if request.url.path.endswith("jobs"):
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return httpx.Response(200, json={"text": ""})

    with client(handler) as sdk:
        sdk.transcribe_file(io.BytesIO(b"a"), model=MODEL, content_type="audio/wav")
    length = lengths[0]
    for sync_cap, job_cap, expected in [
        (length, length, "/v1/audio/transcriptions"),
        (length - 1, length, "/v1/transcription_jobs"),
    ]:
        paths.clear()
        with client(handler, limits=Limits(sync_cap, job_cap)) as sdk:
            sdk.transcribe_file(io.BytesIO(b"a"), model=MODEL, content_type="audio/wav")
        assert paths[0] == expected


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_lost_admission_reuses_body_and_key(failure: Any) -> None:
    submissions = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return completed()
        submissions.append((request.content, request.headers["idempotency-key"]))
        if len(submissions) == 1:
            raise failure("lost response https://private.example/?token=" + CREDENTIAL)
        return httpx.Response(202, json={"id": "job-1", "status": "queued"})

    with client(handler, clock) as sdk:
        assert sdk.transcribe_file(
            io.BytesIO(b"audio"), model=MODEL, idempotency_key="saved", content_type="audio/wav"
        ).job_id
    assert len(submissions) == 2 and submissions[0] == submissions[1]
    assert clock.sleeps == [0.4375]


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError]
)
def test_sync_ambiguous_never_resubmitted(failure: Any) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise failure("Authorization: Bearer " + CREDENTIAL + " https://sensitive.example/")

    with client(handler) as sdk:
        with pytest.raises(AmbiguousSubmissionError) as caught:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    error = caught.value
    assert len(calls) == 1
    assert error.operation_key and error.phase == "sync_submit"
    assert error.__cause__ is None and error.__context__ is None
    assert CREDENTIAL not in str(error) and "https://" not in str(error)


POST_SEND = [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError]
UNADMITTED = ["inline_claim_timeout", "inline_admission_refused", "no_serving_capacity"]


def sync_upload(sdk: Any, **kwargs: Any) -> Any:
    return sdk.transcribe_file(
        io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav", **kwargs
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", POST_SEND)
def test_sync_replay_always_replays_identical_request(failure: Any, asynchronous: bool) -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise failure("dropped after send")
        return httpx.Response(200, json={"text": "again"})

    with client(handler, clock, asynchronous=asynchronous, sync_replay="always") as sdk:
        assert sync_upload(sdk).text == "again"
    first, second = requests
    assert first.url.path == second.url.path == "/v1/audio/transcriptions"
    assert first.content == second.content and first.headers == second.headers
    assert clock.sleeps == [0.4375]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", POST_SEND)
def test_sync_replay_default_keeps_ambiguity(failure: Any, asynchronous: bool) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise failure("dropped after send")
        return httpx.Response(200, json={"text": "again"})

    with client(handler, asynchronous=asynchronous) as sdk:
        assert sdk.sync_replay == "never"
        with pytest.raises(AmbiguousSubmissionError) as caught:
            sync_upload(sdk)
    assert len(requests) == 1 and caught.value.is_transient is False


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "failure,kind", [(httpx.ReadTimeout, APITimeoutError), (httpx.WriteError, APIConnectionError)]
)
def test_sync_replay_exhaustion_raises_transient_error(
    failure: Any, kind: type[APIConnectionError], asynchronous: bool
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise failure("dropped after send")

    with client(handler, asynchronous=asynchronous, sync_replay="always") as sdk:
        with pytest.raises(kind) as caught:
            sync_upload(sdk)
    error = caught.value
    assert type(error) is kind and not isinstance(error, AmbiguousSubmissionError)
    assert error.phase == "sync_submit" and error.retryable is True and error.is_transient
    assert len(requests) == RetryPolicy().max_attempts


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sync_replay_deadline_is_not_ambiguous(asynchronous: bool) -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 20
        raise httpx.ReadTimeout("dropped after send")

    with client(handler, clock, asynchronous=asynchronous, sync_replay="always") as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sync_upload(sdk, deadline=10)
    assert caught.value.phase == "sync_submit" and caught.value.job_id is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("policy", ["always", "never"])
@pytest.mark.parametrize("status", [502, 503, 504])
def test_sync_replay_replays_retryable_responses(
    status: int, policy: str, asynchronous: bool
) -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, json={"error": {"retryable": True}})
        return httpx.Response(200, json={"text": "again"})

    with client(handler, clock, asynchronous=asynchronous, sync_replay=policy) as sdk:
        if policy == "always":
            assert sync_upload(sdk).text == "again"
            assert len(requests) == 2 and requests[0].content == requests[1].content
            assert clock.sleeps == [0.4375]
        else:
            with pytest.raises(InternalServerError) as caught:
                sync_upload(sdk)
            assert len(requests) == 1 and caught.value.is_transient is False


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sync_replay_exhausted_responses_are_transient(asynchronous: bool) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": {"retryable": True}})

    with client(handler, asynchronous=asynchronous, sync_replay="always") as sdk:
        with pytest.raises(InternalServerError) as caught:
            sync_upload(sdk)
    assert len(requests) == RetryPolicy().max_attempts
    assert caught.value.phase == "sync_submit" and caught.value.is_transient is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("error", [{"retryable": False}, {"code": "service_unavailable"}])
def test_sync_replay_never_replays_non_retryable_responses(
    error: dict[str, Any], asynchronous: bool
) -> None:
    requests = []
    if "code" in error:
        error = {**error, "retryable": False}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": error})

    with client(handler, asynchronous=asynchronous, sync_replay="always") as sdk:
        with pytest.raises(InternalServerError) as caught:
            sync_upload(sdk)
    assert len(requests) == 1 and caught.value.is_transient is False


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("policy", ["always", "never"])
@pytest.mark.parametrize("code", UNADMITTED)
def test_sync_replay_leaves_fallback_attempts_unchanged(
    code: str, policy: str, asynchronous: bool
) -> None:
    sync = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            sync.append(request)
            return refused(code)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with client(handler, asynchronous=asynchronous, sync_replay=policy) as sdk:
        assert sync_upload(sdk).job_id == "job-1"
    assert len(sync) == (1 if code == "inline_claim_timeout" else RetryPolicy().max_attempts)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sync_replay_keeps_the_job_fallback(asynchronous: bool) -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/audio/transcriptions":
            return refused("inline_claim_timeout")
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with client(handler, asynchronous=asynchronous, sync_replay="always") as sdk:
        assert sync_upload(sdk).job_id == "job-1"
    assert paths == [
        "/v1/audio/transcriptions",
        "/v1/transcription_jobs",
        "/v1/transcription_jobs/job-1",
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("value", ["never", "always"])
def test_sync_replay_is_a_constructor_setting(value: str, asynchronous: bool) -> None:
    with client(lambda _: completed(), asynchronous=asynchronous) as sdk:
        assert sdk.sync_replay == "never"
    with client(lambda _: completed(), asynchronous=asynchronous, sync_replay=value) as sdk:
        assert sdk.sync_replay == value


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("value", ["sometimes", "Always", "", None, True])
def test_sync_replay_rejects_other_values(value: Any, asynchronous: bool) -> None:
    with pytest.raises(ValueError, match="sync_replay"):
        client(lambda _: completed(), asynchronous=asynchronous, sync_replay=value)


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("hint", [None, "4", "Tue, 14 Nov 2023 22:13:24 GMT"])
def test_retry_after_and_backoff(status: int, hint: str | None) -> None:
    clock = Clock()
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.method == "GET":
            return completed()
        count += 1
        if count == 1:
            return httpx.Response(
                status,
                json={"error": {"code": "input_busy", "retryable": True}},
                headers={"Retry-After": hint} if hint else {},
            )
        return httpx.Response(202, json={"id": "job-1", "status": "queued"})

    with client(handler, clock) as sdk:
        sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert count == 2 and clock.sleeps == [4 if hint else 0.4375]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_retry_after_exceeds_deadline(asynchronous: bool) -> None:
    clock = Clock()
    response = httpx.Response(429, headers={"Retry-After": "60"})
    with client(lambda _: response, clock, asynchronous=asynchronous) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=10)
    assert clock.sleeps == [] and caught.value.phase == "job_submit"


@pytest.mark.parametrize(
    ("status", "code", "retryable", "kind"),
    [
        (401, "invalid_api_key", True, AuthenticationError),
        (403, "api_key_forbidden", True, PermissionDeniedError),
        (429, "clip_exceeds_tier_capacity", None, RateLimitError),
        (503, "queue_operation_rejected", None, InternalServerError),
        (503, "input_busy", False, InternalServerError),
        (409, "idempotency_replay_unavailable", None, ConflictError),
        (409, "upload_already_bound", None, UploadError),
        (410, "upload_expired", None, UploadError),
        (429, "upload_limit_exceeded", False, RateLimitError),
        (422, "idempotency_payload_mismatch", None, UnprocessableEntityError),
    ],
)
def test_terminal_http_failures_never_retried(
    status: int, code: str, retryable: bool | None, kind: Any
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code, "retryable": retryable}})

    with client(handler) as sdk:
        with pytest.raises(kind) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert len(calls) == 1 and caught.value.code == code and caught.value.status == status


def test_html_413_discards_body() -> None:
    with client(lambda _: httpx.Response(413, text="<html>https://private.example/</html>")) as sdk:
        with pytest.raises(PayloadTooLargeError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert caught.value.status == 413 and caught.value.code is None
    assert "html" not in str(caught.value) and "https://" not in str(caught.value)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sync_size_refusal_falls_back_once(asynchronous: bool) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(413, text="<html>Too large</html>")
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with client(handler, asynchronous=asynchronous) as sdk:
        sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert requests[0].content == requests[1].content
    assert [r.url.path for r in requests] == [
        "/v1/audio/transcriptions",
        "/v1/transcription_jobs",
        "/v1/transcription_jobs/job-1",
    ]


def test_terminal_job_error_keeps_context() -> None:
    with client(
        lambda _: httpx.Response(
            200,
            json={
                "id": "job-1",
                "status": "error",
                "error": {
                    "code": "result_unreadable",
                    "retryable": False,
                    "message": "https://sensitive.example/ " + CREDENTIAL,
                },
            },
        )
    ) as sdk:
        with pytest.raises(TerminalJobError) as caught:
            sdk.resume("job-1")
    error = caught.value
    assert error.job_id == "job-1" and error.last_status == "error"
    assert error.code == "result_unreadable" and error.retryable is False
    assert error.status == 200 and error.phase == "poll"
    assert "https://" not in str(error) and CREDENTIAL not in str(error)


def test_deadline_after_admission_preserves_id() -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        clock.now = 3
        return httpx.Response(
            200, json={"id": "job-1", "status": "queued"}, headers={"Retry-After": "9"}
        )

    with client(handler, clock) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=5)
    assert caught.value.job_id == "job-1" and caught.value.last_status == "queued"
    assert caught.value.operation_key and clock.sleeps == []


def test_semaphore_wait_counts_toward_deadline() -> None:
    clock = Clock()
    calls = []
    with client(lambda r: calls.append(r), clock, max_concurrency=1) as sdk:
        semaphore = sdk._lifecycle.semaphore
        assert semaphore is not None
        semaphore.acquire()
        try:
            with pytest.raises(DeadlineExceededError) as caught:
                sdk.resume("job-1", deadline=0.1)
        finally:
            semaphore.release()
    assert caught.value.phase == "poll"
    assert caught.value.job_id == "job-1"
    assert clock.now == pytest.approx(0.1) and calls == []


def test_concurrent_calls_are_independent() -> None:
    barrier = threading.Barrier(4)
    keys: list[str] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            key = request.headers["idempotency-key"]
            with lock:
                keys.append(key)
            barrier.wait(timeout=5)
            return httpx.Response(202, json={"id": key, "status": "queued"})
        return completed(job=request.url.path.rsplit("/", 1)[1])

    with client(handler) as sdk, ThreadPoolExecutor(4) as pool:
        outputs = list(
            pool.map(lambda _: sdk.transcribe_url("https://audio.example/a", model=MODEL), range(4))
        )
    assert len(set(keys)) == 4
    assert {output.job_id for output in outputs} == set(keys)


@pytest.mark.parametrize("location", ["https://other.example/private", API + "/elsewhere"])
def test_redirects_do_not_forward_credentials(location: str) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": location})

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        auth=("user", "password"),
        headers={"X-Unwanted": "value"},
    ) as http:
        with Machinera(api_key=CREDENTIAL, base_url=API, http_client=http) as sdk:
            with pytest.raises(APIStatusError) as caught:
                sdk.get_job("job-1")
        assert not http.is_closed
    assert caught.value.status == 307 and len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer " + CREDENTIAL
    assert "X-Unwanted" not in requests[0].headers


def test_nonseekable_rejected_before_http() -> None:
    class Unseekable(io.BytesIO):
        def seekable(self) -> bool:
            return False

    requests = []
    source = Unseekable(b"audio")
    with client(lambda r: requests.append(r)) as sdk:
        with pytest.raises(ValueError):
            sdk.transcribe_file(source, model=MODEL, content_type="audio/wav")
    assert not source.closed and requests == []


@pytest.mark.parametrize("change", ["size", "content"])
def test_file_mutation_detected_before_changed_bytes_sent(change: str) -> None:
    class ChangingFile(io.BytesIO):
        rewinds = 0

        def seek(self, offset: int, whence: int = 0) -> int:
            if offset == 0 and whence == 0:
                self.rewinds += 1
                if self.rewinds == 3:
                    super().seek(0)
                    self.write(b"changed" if change == "size" else b"other")
            return super().seek(offset, whence)

    source = ChangingFile(b"audio")
    calls = []
    with client(lambda r: calls.append(r)) as sdk:
        with pytest.raises(IntegrityError):
            sdk.transcribe_file(source, model=MODEL, content_type="audio/wav")
    assert not source.closed and source.tell() == 0 and calls == []


def test_retries_bounded_and_poll_failures_have_separate_budget() -> None:
    clock = Clock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls in (1, 3, 5):
            return httpx.Response(503, json={"error": {"retryable": True}})
        if calls in (2, 4):
            return httpx.Response(200, json={"id": "job-1", "status": "queued"})
        return completed()

    with client(handler, clock, retry_policy=RetryPolicy(max_attempts=2)) as sdk:
        assert sdk.resume("job-1").job_id == "job-1"
    assert calls == 6 and clock.sleeps == [0.4375, 1, 0.4375, 1, 0.4375]


def test_retry_exhaustion_and_fresh_operation_keys() -> None:
    requests = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadError("lost")

    with client(handler, clock) as sdk:
        for _ in range(2):
            with pytest.raises(APIConnectionError):
                sdk.transcribe_url("https://audio.example/a", model=MODEL)
    keys = [request.headers["idempotency-key"] for request in requests]
    assert len(requests) == 6 and len(set(keys[:3])) == len(set(keys[3:])) == 1
    assert keys[0] != keys[3]
    assert clock.sleeps == [0.4375, 0.875] * 2


def test_interrupt_preserves_accepted_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        raise KeyboardInterrupt

    with client(handler) as sdk:
        with pytest.raises(TranscriptionInterrupted) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert isinstance(caught.value, KeyboardInterrupt)
    assert caught.value.job_id == "job-1" and caught.value.phase == "poll"
    assert caught.value.__context__ is None


def test_request_timeouts_capped_by_remaining_deadline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {"connect": 2, "read": 2, "write": 2, "pool": 2}
        return completed()

    with client(handler) as sdk:
        sdk.resume("job-1", deadline=2)


def test_config_immutable_and_base_url_normalized() -> None:
    with Machinera(
        api_key=CREDENTIAL,
        base_url=API + "///",
        transport=httpx.MockTransport(lambda _: completed()),
    ) as sdk:
        assert sdk.base_url == API
        with pytest.raises(FrozenInstanceError):
            sdk.base_url = "https://other.example/v1"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            sdk.timeout.read = 1  # type: ignore[misc]
    assert CREDENTIAL not in repr(sdk)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.example/v2",
        "https://api.example/v1?key=x",
        "https://user:pass@api.example/v1",
        "ftp://api.example/v1",
    ],
)
def test_invalid_endpoint_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError):
        Machinera(api_key=CREDENTIAL, base_url=endpoint)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RetryPolicy(max_attempts=0),
        lambda: RetryPolicy(max_delay=0.1),
        lambda: TimeoutPolicy(deadline=float("inf")),
        lambda: TimeoutPolicy(read=0),
        lambda: Limits(2, 1),
    ],
)
def test_invalid_policy_rejected(factory: Any) -> None:
    with pytest.raises(ValueError):
        factory()


def test_sync_connect_failure_is_safe_to_retry() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={"text": ""})

    with client(handler) as sdk:
        assert (
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav").text
            == ""
        )
    assert len(calls) == 2


def test_retry_after_http_date_uses_wall_clock() -> None:
    clock = Clock()
    value = format_datetime(datetime.fromtimestamp(1_700_000_007, timezone.utc), usegmt=True)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": value})
        return completed()

    with client(handler, clock) as sdk:
        sdk.resume("job-1")
    assert clock.sleeps == [7]


@pytest.mark.parametrize("suffix", ["wav", "flac", "mp3"])
def test_path_input_preserves_suffix(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / ("private-name." + suffix)
    path.write_bytes(b"audio")

    def handler(request: httpx.Request) -> httpx.Response:
        assert f'filename="private-name.{suffix}"'.encode() in request.content
        assert str(tmp_path).encode() not in request.content
        return httpx.Response(200, json={"text": ""})

    with client(handler) as sdk:
        sdk.transcribe_file(path, model=MODEL, content_type="audio/wav")
    assert path.read_bytes() == b"audio"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sanitizes_local_file_error(asynchronous: bool) -> None:
    with client(lambda _: completed(), asynchronous=asynchronous) as sdk:
        with pytest.raises(APIConnectionError) as caught:
            sdk.transcribe_file("/missing/private-file.wav", model=MODEL, content_type="audio/wav")
    assert "private-file" not in str(caught.value) and "/missing" not in str(caught.value)
    assert str(caught.value).endswith(f"(FileNotFoundError, errno {errno.ENOENT})")
    assert caught.value.is_transient is False
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unreadable_handle_is_not_transient(asynchronous: bool) -> None:
    class Unreadable(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError(errno.EIO, "private detail")

    with client(lambda _: completed(), asynchronous=asynchronous) as sdk:
        with pytest.raises(APIConnectionError) as caught:
            sdk.transcribe_file(Unreadable(b"audio"), model=MODEL, content_type="audio/wav")
    assert str(caught.value).endswith(f"(OSError, errno {errno.EIO})")
    assert "private" not in str(caught.value) and caught.value.is_transient is False


@pytest.mark.parametrize("asynchronous", [False, True])
def test_exhausted_connection_retries_are_transient(asynchronous: bool) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable")

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(APIConnectionError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert caught.value.retryable is True and caught.value.is_transient is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "status,error,kind,transient",
    [
        (429, {"retryable": False}, RateLimitError, False),
        (429, {"retryable": True}, RateLimitError, True),
        (401, {"code": "invalid_api_key", "retryable": True}, AuthenticationError, False),
        (403, {"code": "api_key_forbidden", "retryable": True}, PermissionDeniedError, False),
        (503, {"retryable": True}, InternalServerError, True),
        (503, {"retryable": False}, InternalServerError, False),
    ],
)
def test_is_transient_matches_the_retry_decision(
    status: int,
    error: dict[str, Any],
    kind: type[APIStatusError],
    transient: bool,
    asynchronous: bool,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": error})

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(kind) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    retried = len(requests) == RetryPolicy().max_attempts
    assert caught.value.is_transient is transient is retried
    assert len(requests) == (RetryPolicy().max_attempts if transient else 1)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code,transient", [("input_busy", True), ("service_unavailable", False)])
def test_sync_phase_refusal_transience_follows_replay_safety(
    code: str, transient: bool, asynchronous: bool
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": {"code": code, "retryable": True}})

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(InternalServerError) as caught:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert caught.value.phase == "sync_submit" and caught.value.is_transient is transient
    assert len(requests) == (RetryPolicy().max_attempts if transient else 1)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deadline_before_job_id_is_recovered_with_the_same_key(asynchronous: bool) -> None:
    clock = Clock()
    lost = True
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            keys.append(request.headers["idempotency-key"])
            if lost:
                clock.now += 6
                raise httpx.ReadTimeout("response lost after admission")
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=10)
        error = caught.value
        assert isinstance(error, RecoverableJobError) and error.job_id is None
        assert error.phase == "job_submit" and error.is_transient is False
        lost = False
        output = sdk.transcribe_url(
            "https://audio.example/a", model=MODEL, idempotency_key=error.operation_key
        )
    assert output.job_id == "job-1" and set(keys) == {error.operation_key}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_interrupt_before_job_id_keeps_the_operation_key(asynchronous: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(TranscriptionInterrupted) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL, idempotency_key="key-1")
    error = caught.value
    assert isinstance(error, RecoverableJobError) and error.job_id is None
    assert error.operation_key == "key-1" and error.phase == "job_submit"
    assert error.ambiguous is False and error.is_transient is False


def test_late_admission_response_keeps_recovery_id() -> None:
    clock = Clock()

    def handler(_: httpx.Request) -> httpx.Response:
        clock.now = 6
        return httpx.Response(
            202, json={"id": "job-1", "status": "queued"}, headers={"x-request-id": "request-late"}
        )

    with client(handler, clock) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=5)
    assert caught.value.job_id == "job-1"
    assert caught.value.request_id == "request-late"


def test_preparation_counts_toward_deadline() -> None:
    clock = Clock()

    class SlowFile(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            clock.now += 2
            return super().read(size)

    source = SlowFile(b"skip-audio")
    source.seek(5)
    requests = []
    with client(lambda r: requests.append(r), clock) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_file(source, model=MODEL, deadline=1, content_type="audio/wav")
    assert caught.value.phase == "prepare" and requests == []
    assert source.tell() == 5 and not source.closed


def test_forced_job_transport_with_injected_client() -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Machinera(api_key=CREDENTIAL, base_url=API, transport="job", http_client=http) as sdk:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert paths[0] == "/v1/transcription_jobs"


def refused(code: str, retryable: bool = True) -> httpx.Response:
    return httpx.Response(
        503, json={"error": {"code": code, "retryable": retryable}}, headers={"x-request-id": "r-1"}
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code", UNADMITTED)
def test_auto_sync_admission_refusal_falls_back_to_job(code: str, asynchronous: bool) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/audio/transcriptions":
            return refused(code)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with client(handler, asynchronous=asynchronous) as sdk:
        output = sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    sync = [r for r in requests if r.url.path == "/v1/audio/transcriptions"]
    submit = [r for r in requests if r.url.path == "/v1/transcription_jobs"]
    assert output.job_id == "job-1" and len(submit) == 1
    assert len(sync) == (1 if code == "inline_claim_timeout" else RetryPolicy().max_attempts)
    assert all(r.content == submit[0].content for r in sync)
    assert submit[0].headers["idempotency-key"]
    assert requests[-1].url.path == "/v1/transcription_jobs/job-1"


@pytest.mark.parametrize("code", UNADMITTED)
def test_job_fallback_keeps_the_call_deadline(code: str) -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/audio/transcriptions":
            clock.now += 4
            return refused(code)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return httpx.Response(200, json={"id": "job-1", "status": "queued"})

    retry = RetryPolicy(max_attempts=1)
    with client(handler, clock, retry_policy=retry) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_file(
                io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav", deadline=10
            )
    assert caught.value.job_id == "job-1" and caught.value.phase == "poll"
    assert clock.now < 10 and caught.value.operation_key == requests[1].headers["idempotency-key"]


def lost(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("lost")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "handler,kind",
    [
        (lambda _: refused("inline_claim_timeout", retryable=False), InternalServerError),
        (lambda _: refused("service_unavailable"), InternalServerError),
        (lost, AmbiguousSubmissionError),
    ],
)
def test_no_job_fallback_unless_admission_refused(
    handler: Callable[[httpx.Request], httpx.Response], kind: type[Exception], asynchronous: bool
) -> None:
    paths = []

    def record(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return handler(request)

    with client(record, asynchronous=asynchronous) as sdk:
        with pytest.raises(kind) as caught:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert caught.value.job_id is None  # type: ignore[attr-defined]
    assert paths and set(paths) == {"/v1/audio/transcriptions"}


@pytest.mark.parametrize("code", UNADMITTED)
def test_job_transport_never_tries_sync(code: str) -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/audio/transcriptions":
            return refused(code)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed()

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Machinera(api_key=CREDENTIAL, base_url=API, transport="job", http_client=http) as sdk:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert paths == ["/v1/transcription_jobs", "/v1/transcription_jobs/job-1"]


@pytest.mark.parametrize("status,code", [(429, None), (503, "input_busy")])
def test_sync_definitive_refusal_retries_same_transport(status: int, code: str | None) -> None:
    requests = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                status,
                json={"error": {"code": code, "retryable": True}},
                headers={"Retry-After": "2"},
            )
        return httpx.Response(200, json={"text": ""})

    with client(handler, clock) as sdk:
        sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert len(requests) == 2 and clock.sleeps == [2]
    assert all(r.url.path == "/v1/audio/transcriptions" for r in requests)
    assert requests[0].content == requests[1].content


def test_poll_budget_exhaustion_keeps_job() -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "job-1", "status": "queued"})

    with client(handler, clock, retry_policy=RetryPolicy(max_polls=2)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.resume("job-1")
    assert len(requests) == 2 and caught.value.job_id == "job-1"


def queued(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "job-1", "status": "queued"})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deadline_alone_ends_polling(asynchronous: bool) -> None:
    clock = Clock()
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        polls += 1
        return completed() if polls == 5000 else queued(request)

    seconds = RetryPolicy(poll_interval=1)
    with client(handler, clock, asynchronous=asynchronous, retry_policy=seconds) as sdk:
        assert sdk.resume("job-1", deadline=4 * 3600).job_id == "job-1"
    assert polls == 5000 and clock.now == 4999


def test_long_deadline_polls_until_it_expires() -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queued(request)

    with client(handler, clock, retry_policy=RetryPolicy(poll_interval=10)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.resume("job-1", deadline=4 * 3600)
    assert len(requests) == 4 * 360 and clock.now < 4 * 3600
    assert caught.value.job_id == "job-1" and caught.value.is_transient


def test_default_deadline_ends_polling_without_explicit_deadline() -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queued(request)

    with client(handler, clock, retry_policy=RetryPolicy(poll_interval=1)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.resume("job-1")
    budget = TimeoutPolicy().deadline
    assert len(requests) == budget and budget - 1 <= clock.now < budget
    assert caught.value.job_id == "job-1"


def test_close_waits_for_active_calls() -> None:
    entered = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    closed = threading.Event()

    def handler(_: httpx.Request) -> httpx.Response:
        entered.set()
        assert release.wait(timeout=5)
        return completed()

    sdk = client(handler)

    def close_client() -> None:
        closing.set()
        sdk.close()
        closed.set()

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(sdk.resume, "job-1")
        assert entered.wait(timeout=5)
        closer = pool.submit(close_client)
        assert closing.wait(timeout=5)
        assert not closed.is_set()
        release.set()
        assert running.result(timeout=5).job_id == "job-1"
        closer.result(timeout=5)
    assert closed.is_set() and sdk._http.is_closed
    with pytest.raises(APIConnectionError, match="closed") as caught:
        sdk.resume("job-1")
    assert caught.value.is_transient is False


def test_streaming_reads_are_bounded_and_offset_restored() -> None:
    class BoundedRead(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 65536
            return super().read(size)

    source = BoundedRead(b"prefix" + b"a" * 200_000)
    source.seek(6)
    with client(lambda _: httpx.Response(200, json={"text": ""})) as sdk:
        sdk.transcribe_file(source, model=MODEL, content_type="audio/wav")
    assert source.tell() == 6 and not source.closed


@pytest.mark.parametrize(
    "kind",
    ["missing_text", "invalid_json", "bad_status", "wrong_id", "no_result", "admission", "sync"],
)
def test_malformed_responses_are_typed(kind: str) -> None:
    responses = {
        "missing_text": httpx.Response(
            200, json={"id": "job-1", "status": "completed", "result": {}}
        ),
        "invalid_json": httpx.Response(200, text="<html>invalid</html>"),
        "bad_status": httpx.Response(200, json={"id": "job-1", "status": 7}),
        "wrong_id": httpx.Response(200, json={"id": "job-2", "status": "queued"}),
        "no_result": httpx.Response(200, json={"id": "job-1", "status": "completed"}),
        "admission": httpx.Response(202, json={"status": "queued"}),
        "sync": httpx.Response(200, json={"text": 7}),
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[kind]

    with client(handler) as sdk:
        with pytest.raises(APIResponseValidationError) as caught:
            if kind == "admission":
                sdk.transcribe_url("https://audio.example/a", model=MODEL)
            elif kind == "sync":
                sdk.transcribe_file(b"audio", model=MODEL, content_type="audio/wav")
            else:
                sdk.resume("job-1")
    assert not isinstance(caught.value, APIConnectionError)
    assert caught.value.retryable is False and len(calls) == 1
    assert caught.value.status_code == responses[kind].status_code


@pytest.mark.parametrize(
    ("change", "valid"),
    [
        ({"created_at": 1_700_000_000.0}, True),
        ({"error": {"retryable": "false", "details": ["a"]}}, True),
        ({"words": [{"word": "a", "start": 0, "end": 1, "confidence": "0.9"}]}, True),
        ({"created_at": 1.5}, False),
        ({"text": 7}, False),
        ({"words": [{"word": 7, "start": 0, "end": 1}]}, False),
    ],
)
def test_metadata_validation_is_lax_and_text_strict(change: dict[str, Any], valid: bool) -> None:
    snapshot: dict[str, Any] = {"id": "job-1", "status": "completed", "result": result()}
    for name, value in change.items():
        if name in ("text", "words"):
            snapshot["result"][name] = value
        else:
            snapshot[name] = value
    with client(lambda _: httpx.Response(200, json=snapshot)) as sdk:
        if valid:
            assert sdk.resume("job-1").text == "  exact\ntext  "
        else:
            with pytest.raises(APIResponseValidationError):
                sdk.resume("job-1")


def test_unknown_job_status_is_returned_by_get_job() -> None:
    with client(lambda _: httpx.Response(200, json={"id": "job-1", "status": "cancelled"})) as sdk:
        snapshot = sdk.get_job("job-1")
    assert snapshot.status == "cancelled" and snapshot.raw["status"] == "cancelled"


@pytest.mark.parametrize(("status", "token"), [("cancelled", "cancelled"), ("x y/z", None)])
def test_unknown_job_status_while_polling_is_terminal(status: str, token: str | None) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "job-1", "status": status})

    with client(handler) as sdk:
        with pytest.raises(TerminalJobError) as caught:
            sdk.resume("job-1")
    error = caught.value
    assert len(calls) == 1 and error.retryable is False and error.status_code == 200
    assert error.last_status == token and error.job_id == "job-1" and error.phase == "poll"
    assert str(error).endswith(token or "an unrecognized status")
    assert "/" not in str(error)


def test_known_job_statuses_poll_until_completed() -> None:
    clock = Clock()
    statuses = iter(["queued", "processing", "processing"])

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(statuses, None)
        if status is None:
            return completed()
        return httpx.Response(200, json={"id": "job-1", "status": status})

    with client(handler, clock) as sdk:
        assert sdk.resume("job-1").text == result()["text"]
    assert len(clock.sleeps) == 3


@pytest.mark.parametrize(
    ("headers", "delay"),
    [
        ({"retry-after-ms": "2500"}, 2.5),
        ({"retry-after-ms": "2500", "Retry-After": "9"}, 2.5),
        ({"retry-after-ms": "soon", "Retry-After": "3"}, 3),
        ({"Retry-After": "1.5"}, 1.5),
        ({"Retry-After": "4"}, 4),
        ({"Retry-After": "nan"}, 0.4375),
        ({"Retry-After": "inf"}, 0.4375),
        ({"Retry-After": "-1"}, 0.4375),
        ({"Retry-After": "1e3"}, 0.4375),
        ({"Retry-After": "9" * 400}, 0.4375),
        ({"retry-after-ms": "-5"}, 0.4375),
    ],
)
def test_retry_after_forms(headers: dict[str, str], delay: float) -> None:
    clock = Clock()
    responses = iter([httpx.Response(503, headers=headers)])

    with client(lambda _: next(responses, None) or completed(), clock) as sdk:
        sdk.resume("job-1")
    assert clock.sleeps == [delay]


def test_retry_after_ms_beyond_deadline_raises() -> None:
    clock = Clock()
    with client(lambda _: httpx.Response(503, headers={"retry-after-ms": "60000"}), clock) as sdk:
        with pytest.raises(DeadlineExceededError):
            sdk.resume("job-1", deadline=10)
    assert clock.sleeps == []


@pytest.fixture
def sdk_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("machinera")
    handlers, level = logger.handlers[:], logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)


def test_retry_and_status_logs_are_sanitized(
    caplog: pytest.LogCaptureFixture, sdk_logger: logging.Logger
) -> None:
    caplog.set_level("DEBUG", logger="machinera")
    url = "https://audio.example/private?signature=private-signature"
    posts = gets = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, gets
        if request.method == "POST":
            posts += 1
            if posts == 1:
                return httpx.Response(
                    503,
                    json={"error": {"code": "input_busy", "retryable": True}},
                    headers={"x-request-id": "request-9"},
                )
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        gets += 1
        if gets == 1:
            return httpx.Response(200, json={"id": "job-1", "status": "processing"})
        return completed("private transcript")

    with client(handler) as sdk:
        sdk.transcribe_url(url, model=MODEL)
    records = [r for r in caplog.records if r.name == "machinera"]
    retry, *transitions = records
    assert retry.levelno == logging.DEBUG
    assert retry.getMessage() == (
        "Retrying POST /transcription_jobs in 0.438s after attempt 1 of 3 "
        "(status=503, code=input_busy, request_id=request-9)"
    )
    assert [(r.levelno, r.getMessage()) for r in transitions] == [
        (logging.INFO, "Job job-1 status processing"),
        (logging.INFO, "Job job-1 status completed"),
    ]
    text = "\n".join(r.getMessage() for r in records)
    for value in (url, "audio.example", "private-signature", CREDENTIAL, "private transcript"):
        assert value not in text


def test_job_poll_retry_logs_path_template(
    caplog: pytest.LogCaptureFixture, sdk_logger: logging.Logger
) -> None:
    caplog.set_level("DEBUG", logger="machinera")
    responses = iter([httpx.Response(502)])
    with client(lambda _: next(responses, None) or completed()) as sdk:
        sdk.resume("job-1")
    retry = next(r for r in caplog.records if r.name == "machinera")
    assert "GET /transcription_jobs/{job_id} " in retry.getMessage()


@pytest.mark.parametrize(("value", "level"), [("debug", logging.DEBUG), ("INFO", logging.INFO)])
def test_log_env_sets_level_and_handler(
    monkeypatch: pytest.MonkeyPatch, sdk_logger: logging.Logger, value: str, level: int
) -> None:
    sdk_logger.handlers.clear()
    sdk_logger.setLevel(logging.NOTSET)
    monkeypatch.setenv("MACHINERA_LOG", value)
    client(completed).close()
    client(completed).close()
    assert sdk_logger.level == level and len(sdk_logger.handlers) == 1
    assert isinstance(sdk_logger.handlers[0], logging.StreamHandler)


def test_log_env_keeps_existing_handler(
    monkeypatch: pytest.MonkeyPatch, sdk_logger: logging.Logger
) -> None:
    existing = logging.NullHandler()
    sdk_logger.handlers[:] = [existing]
    monkeypatch.setenv("MACHINERA_LOG", "debug")
    client(completed).close()
    assert sdk_logger.handlers == [existing] and sdk_logger.level == logging.DEBUG


@pytest.mark.parametrize("value", [None, "", "verbose"])
def test_log_env_absent_or_unknown_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, sdk_logger: logging.Logger, value: str | None
) -> None:
    sdk_logger.handlers.clear()
    sdk_logger.setLevel(logging.WARNING)
    if value is None:
        monkeypatch.delenv("MACHINERA_LOG", raising=False)
    else:
        monkeypatch.setenv("MACHINERA_LOG", value)
    client(completed).close()
    assert sdk_logger.level == logging.WARNING and sdk_logger.handlers == []


def test_descriptor_cap_is_local() -> None:
    calls = []
    with client(lambda r: calls.append(r), limits=Limits(descriptor_bytes=10)) as sdk:
        with pytest.raises(PayloadTooLargeError):
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert calls == []


def test_no_sensitive_values_in_logs_or_result_repr(caplog: Any) -> None:
    caplog.set_level("DEBUG")
    url = "https://audio.example/private?signature=private-signature"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed("private transcript")

    with client(handler) as sdk:
        output = sdk.transcribe_url(url, model=MODEL)
    for value in (url, "private-signature", CREDENTIAL, "private transcript"):
        assert value not in caplog.text and value not in repr(output)


def test_network_exception_chain_is_removed_after_poll_retries() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        try:
            raise OSError("https://private.example/ " + CREDENTIAL)
        except OSError as error:
            raise httpx.ReadError("Authorization: " + CREDENTIAL) from error

    with client(handler) as sdk:
        with pytest.raises(APIConnectionError) as caught:
            sdk.resume("job-1")
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert CREDENTIAL not in str(caught.value) and "https://" not in str(caught.value)


def test_poll_request_budget_is_separate_from_overall_deadline() -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            clock.now += 3
        return completed()

    with client(handler, clock, timeout=TimeoutPolicy(poll_request=2)) as sdk:
        output = sdk.resume("job-1", deadline=10)
    assert len(requests) == 2 and output.elapsed_seconds == 3.4375


@pytest.mark.parametrize("filename", [None, "recording.wav"])
@pytest.mark.parametrize("text", ["text", ""])
def test_text_handle_is_a_type_error(filename: str | None, text: str) -> None:
    with client(lambda _: pytest.fail("unexpected HTTP")) as sdk:
        with pytest.raises(TypeError, match="A binary file is required"):
            sdk.transcribe_file(io.StringIO(text), model=MODEL, filename=filename)  # type: ignore[arg-type]
