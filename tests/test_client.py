from __future__ import annotations

import errno
import hashlib
import io
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
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
    error_code,
    failed_job,
    held_slot,
    job_api,
    queued,
    recorder,
    refused,
    refused_sync,
    result,
    submit,
    sync_upload,
    unavailable,
)

from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    ConflictError,
    DeadlineExceededError,
    IntegrityError,
    InternalServerError,
    JobSnapshot,
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


def test_client_identity_equality_and_hashing() -> None:
    with (
        Machinera(api_key=CREDENTIAL, base_url=API) as first,
        Machinera(api_key=CREDENTIAL, base_url=API) as second,
    ):
        assert first is not second
        assert first != second
        assert first == first
        assert second == second
        assert isinstance(hash(first), int)
        assert isinstance(hash(second), int)
        assert len({first, second}) == 2
        clients = {first: "first", second: "second"}
        assert len(clients) == 2
        assert clients[first] == "first"
        assert clients[second] == "second"
    assert first._lifecycle.closed and second._lifecycle.closed


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("text", ["  exact\ntext  ", ""])
@pytest.mark.parametrize("fmt", ["json", "text", "verbose_json"])
def test_sync_preserves_text_and_headers(text: str, fmt: Any, asynchronous: bool) -> None:
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

    with client(handler, asynchronous=asynchronous) as sdk:
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["file", "url"])
@pytest.mark.parametrize("fmt", ["json", "text", "verbose_json"])
def test_job_submission_poll_and_projection(source: str, fmt: Any, asynchronous: bool) -> None:
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
            return accepted()
        if len(requests) == 2:
            return httpx.Response(
                200, json={"id": "job-1", "status": "processing"}, headers={"Retry-After": "3"}
            )
        return completed("")

    with client(handler, clock, asynchronous=asynchronous) as sdk:
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


@pytest.mark.parametrize("text", [result()["text"], ""])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_get_job_and_resume_only_read(asynchronous: bool, text: str) -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        paths.append(request.url.path)
        return completed(text)

    with client(handler, asynchronous=asynchronous) as sdk:
        snapshot = sdk.get_job("job-1")
        assert isinstance(snapshot, JobSnapshot) and snapshot.status == "completed"
        assert snapshot.result is not None and snapshot.result.text == text
        assert sdk.resume("job-1").text == text
    assert paths == ["/v1/transcription_jobs/job-1"] * 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_lost_acceptance_reuses_body_and_key(failure: Any, asynchronous: bool) -> None:
    submissions = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return completed()
        submissions.append((request.content, request.headers["idempotency-key"]))
        if len(submissions) == 1:
            raise failure("lost response https://private.example/?token=" + CREDENTIAL)
        return accepted()

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        assert sdk.transcribe_file(
            io.BytesIO(b"audio"), model=MODEL, idempotency_key="saved", content_type="audio/wav"
        ).job_id
    assert len(submissions) == 2 and submissions[0] == submissions[1]
    assert clock.sleeps == [0.4375]


POST_SEND = [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError]
FALLBACK_REFUSALS = [4008]


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


BARE_5XX = [
    lambda: httpx.Response(500),
    lambda: httpx.Response(500, text="<html>error</html>"),
    lambda: httpx.Response(500, json={"error": {"code": 5999}}),
    lambda: httpx.Response(501, json={"error": {}}),
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "hint,delay",
    [
        (None, 0.4375),
        ("4", 4),
        ("Tue, 14 Nov 2023 22:13:24 GMT", 4),
        ("2", 2),
        ("Tue, 14 Nov 2023 22:13:22 GMT", 2),
    ],
)
def test_retry_after_and_backoff(
    status: int, hint: str | None, asynchronous: bool, delay: float
) -> None:
    clock = Clock()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return completed()
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                status,
                json={"error": {"code": 4001, "retryable": True}},
                headers={"Retry-After": hint} if hint else {},
            )
        return accepted()

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert len(requests) == 2 and clock.sleeps == [delay]

    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    assert requests[0].content == requests[1].content


@pytest.mark.parametrize(
    "headers,status", [({"Retry-After": "60"}, 429), ({"retry-after-ms": "60000"}, 503)]
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_retry_after_exceeds_deadline(
    asynchronous: bool, headers: dict[str, str], status: int
) -> None:
    clock = Clock()
    response = httpx.Response(status, headers=headers)
    with client(lambda _: response, clock, asynchronous=asynchronous) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            if status == 429:
                sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=10)
            else:
                sdk.resume("job-1", deadline=10)
    assert clock.sleeps == [] and caught.value.phase == ("job_submit" if status == 429 else "poll")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("status", "code", "retryable", "kind", "transient"),
    [
        (400, 5999, False, APIStatusError, False),
        (401, 2003, True, AuthenticationError, False),
        (403, 2004, True, PermissionDeniedError, False),
        (429, 3003, None, RateLimitError, False),
        (503, error_code(503, False), None, InternalServerError, False),
        (503, 4001, False, InternalServerError, False),
        (409, 1030, None, ConflictError, False),
        (409, 1005, None, UploadError, False),
        (410, 1003, None, UploadError, False),
        (429, 3001, False, RateLimitError, False),
        (422, 1031, None, UnprocessableEntityError, False),
        (429, None, False, RateLimitError, False),
        (429, None, True, RateLimitError, True),
        (401, 2003, True, AuthenticationError, False),
        (403, 2004, True, PermissionDeniedError, False),
        (503, None, True, InternalServerError, True),
        (503, None, False, InternalServerError, False),
    ],
)
def test_http_error_guidance_controls_retries_and_transience(
    status: int,
    code: int | None,
    retryable: bool | None,
    kind: Any,
    transient: bool,
    asynchronous: bool,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"code": code, "retryable": retryable, "message": CREDENTIAL}}
        )

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(kind) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL)
    assert len(calls) == (RetryPolicy().max_attempts if transient else 1)
    assert caught.value.is_transient is transient is (len(calls) == RetryPolicy().max_attempts)
    assert caught.value.code == code and caught.value.status == status

    assert CREDENTIAL not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None


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
            return accepted()
        return completed()

    with client(handler, asynchronous=asynchronous) as sdk:
        sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert requests[0].content == requests[1].content
    assert [r.url.path for r in requests] == [
        "/v1/audio/transcriptions",
        "/v1/transcription_jobs",
        "/v1/transcription_jobs/job-1",
    ]


@pytest.mark.parametrize("detailed", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_terminal_job_error_keeps_context(asynchronous: bool, detailed: bool) -> None:
    with client(
        lambda _: httpx.Response(
            200,
            json={
                "id": "job-1",
                "status": "error",
                "error": {
                    "code": 5011,
                    "retryable": False,
                    "message": "https://sensitive.example/ " + CREDENTIAL,
                }
                if detailed
                else {"message": "private"},
            },
        ),
        asynchronous=asynchronous,
    ) as sdk:
        with pytest.raises(TerminalJobError) as caught:
            sdk.resume("job-1")
    error = caught.value
    assert error.job_id == "job-1" and error.last_status == "error"
    assert error.code == (5011 if detailed else None)
    if detailed:
        assert error.retryable is False
    assert error.status == 200 and error.phase == "poll"
    assert "https://" not in str(error) and CREDENTIAL not in str(error)

    assert "private" not in str(error)


def test_deadline_after_acceptance_preserves_id() -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return accepted()
        clock.now = 3
        return httpx.Response(
            200, json={"id": "job-1", "status": "queued"}, headers={"Retry-After": "9"}
        )

    with client(handler, clock) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_url("https://audio.example/a", model=MODEL, deadline=5)
    assert caught.value.job_id == "job-1" and caught.value.last_status == "queued"
    assert caught.value.operation_key and clock.sleeps == []


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
            return queued()
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
            return accepted()
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
@pytest.mark.parametrize("code,transient", [(4001, True), (4009, False)])
def test_sync_phase_refusal_transience_follows_replay_safety(
    code: int, transient: bool, asynchronous: bool
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
                raise httpx.ReadTimeout("response lost after acceptance")
            return accepted()
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
@pytest.mark.parametrize("keyed", [False, True])
def test_interrupt_before_job_id_keeps_the_operation_key(asynchronous: bool, keyed: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(TranscriptionInterrupted) as caught:
            sdk.transcribe_url(
                "https://audio.example/a", model=MODEL, idempotency_key="key-1" if keyed else None
            )
    error = caught.value
    assert isinstance(error, RecoverableJobError) and error.job_id is None
    if keyed:
        assert error.operation_key == "key-1"
    else:
        assert error.operation_key and error.operation_key != "key-1"
    assert error.phase == "job_submit" and error.ambiguous is False
    assert error.is_transient is False


def test_late_acceptance_response_keeps_recovery_id() -> None:
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


@pytest.mark.parametrize("policy", ["never", "always"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code", FALLBACK_REFUSALS)
def test_auto_sync_acceptance_refusal_falls_back_to_job(
    code: int, asynchronous: bool, policy: str
) -> None:
    requests = []

    handler = recorder(job_api(sync=lambda request: refused(code)), requests)

    with client(handler, asynchronous=asynchronous, sync_replay=policy) as sdk:
        output = sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    sync = [r for r in requests if r.url.path == "/v1/audio/transcriptions"]
    submit = [r for r in requests if r.url.path == "/v1/transcription_jobs"]
    assert output.job_id == "job-1" and len(submit) == 1
    assert len(sync) == 1
    assert all(r.content == submit[0].content for r in sync)
    assert submit[0].headers["idempotency-key"]
    assert requests[-1].url.path == "/v1/transcription_jobs/job-1"

    assert [r.url.path for r in requests] == ["/v1/audio/transcriptions"] * len(sync) + [
        "/v1/transcription_jobs",
        "/v1/transcription_jobs/job-1",
    ]


@pytest.mark.parametrize("code", FALLBACK_REFUSALS)
def test_job_fallback_keeps_the_call_deadline(code: int) -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/audio/transcriptions":
            clock.now += 4
            return refused(code)
        if request.method == "POST":
            return accepted()
        return queued()

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
        (lambda _: refused(4007, retryable=False), InternalServerError),
        (lambda _: refused(4009), InternalServerError),
        (lost, AmbiguousSubmissionError),
    ],
)
def test_no_job_fallback_unless_acceptance_refused(
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


@pytest.mark.parametrize("code", FALLBACK_REFUSALS)
def test_job_transport_never_tries_sync(code: int) -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/audio/transcriptions":
            return refused(code)
        if request.method == "POST":
            return accepted()
        return completed()

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Machinera(api_key=CREDENTIAL, base_url=API, transport="job", http_client=http) as sdk:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert paths == ["/v1/transcription_jobs", "/v1/transcription_jobs/job-1"]


@pytest.mark.parametrize("status,code", [(429, None), (503, 4001)])
def test_sync_definitive_refusal_retries_same_transport(status: int, code: int | None) -> None:
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
        return queued()

    with client(handler, clock, retry_policy=RetryPolicy(max_polls=2)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.resume("job-1")
    assert len(requests) == 2 and caught.value.job_id == "job-1"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deadline_alone_ends_polling(asynchronous: bool) -> None:
    clock = Clock()
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        polls += 1
        return completed() if polls == 5000 else queued()

    seconds = RetryPolicy(poll_interval=1)
    with client(handler, clock, asynchronous=asynchronous, retry_policy=seconds) as sdk:
        assert sdk.resume("job-1", deadline=4 * 3600).job_id == "job-1"
    assert polls == 5000 and clock.now == 4999


@pytest.mark.parametrize("deadline,interval", [(4 * 3600, 10), (None, 1)])
def test_polling_ends_at_the_explicit_or_default_deadline(
    deadline: int | None, interval: int
) -> None:
    clock = Clock()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queued()

    with client(handler, clock, retry_policy=RetryPolicy(poll_interval=interval)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.resume("job-1", **({"deadline": deadline} if deadline is not None else {}))
    budget = deadline if deadline is not None else TimeoutPolicy().deadline
    assert len(requests) == budget / interval and budget - interval <= clock.now < budget
    assert caught.value.job_id == "job-1" and caught.value.is_transient


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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("status", "token"), [("cancelled", "cancelled"), ("x y/z", None)])
def test_unknown_job_status_while_polling_is_terminal(
    status: str, token: str | None, asynchronous: bool
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "job-1", "status": status})

    with client(handler, asynchronous=asynchronous) as sdk:
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
        ({"Retry-After": "Tue, 14 Nov 2023 22:13:27 GMT"}, 7),
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
def test_retry_after_header_selects_the_wait(headers: dict[str, str], delay: float) -> None:
    clock = Clock()
    responses = iter([httpx.Response(503, headers=headers)])

    with client(lambda _: next(responses, None) or completed(), clock) as sdk:
        sdk.resume("job-1")
    assert clock.sleeps == [delay]


def test_retry_and_status_logs_are_sanitized(
    caplog: pytest.LogCaptureFixture, sdk_logger: logging.Logger
) -> None:
    caplog.set_level(logging.DEBUG)
    url = "https://audio.example/private?signature=private-signature"
    posts = gets = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, gets
        if request.method == "POST":
            posts += 1
            if posts == 1:
                return httpx.Response(
                    503,
                    json={"error": {"code": 4001, "retryable": True}},
                    headers={"x-request-id": "request-9"},
                )
            return accepted()
        gets += 1
        if gets == 1:
            return httpx.Response(200, json={"id": "job-1", "status": "processing"})
        return completed("private transcript")

    with client(handler) as sdk:
        output = sdk.transcribe_url(url, model=MODEL)
    records = [r for r in caplog.records if r.name == "machinera"]
    retry, *transitions = records
    assert retry.levelno == logging.DEBUG
    assert retry.getMessage() == (
        "Retrying POST /transcription_jobs in 0.438s after attempt 1 of 3 "
        "(status=503, code=4001, request_id=request-9)"
    )
    assert [(r.levelno, r.getMessage()) for r in transitions] == [
        (logging.INFO, "Job job-1 status processing"),
        (logging.INFO, "Job job-1 status completed"),
    ]
    text = "\n".join(r.getMessage() for r in records)
    for value in (url, "audio.example", "private-signature", CREDENTIAL, "private transcript"):
        assert value not in text and value not in caplog.text and value not in repr(output)


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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("final", ["unsent", "refused", 3999])
def test_lost_submission_stays_non_transient_after_later_attempts(
    final: str, asynchronous: bool
) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        refusal = refused_sync(request)
        if refusal is not None:
            return refusal
        posts.append(request)
        if len(posts) == 1:
            raise httpx.ReadError("response lost after send")
        if final == "unsent":
            raise httpx.ConnectError("unreachable")
        if final == 3999:
            return httpx.Response(429, json={"error": {"code": 3999}})
        return httpx.Response(503, json={"error": {"code": 4009}})

    errors = (APIConnectionError, InternalServerError, RateLimitError)
    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(errors) as caught:
            submit(sdk, "file", None)
        error = caught.value
        assert error.phase == "job_submit" and error.job_id is None
        assert len(posts) == RetryPolicy().max_attempts
        assert error.is_transient is False
        assert failure_row(error) == 5
        # Documented recovery (failure row 5): repeat under the operation key, which
        # replays the possibly accepted job instead of submitting another.
        posts.clear()
        with pytest.raises(errors) as repeated:
            submit(sdk, "file", error.operation_key)
    assert {r.headers["idempotency-key"] for r in posts} == {error.operation_key}
    assert repeated.value.is_transient is True
    assert failure_row(repeated.value) == 6


def failure_row(error: Any) -> int:
    """Rows 5 to 7 of the documented failure table, for an error without job_id."""
    assert error.job_id is None
    if (
        isinstance(error, (APIConnectionError, APIStatusError))
        and error.retryable is True
        and error.phase in ("job_submit", "submit")
        and error.is_transient is False
    ):
        return 5
    return 6 if error.is_transient else 7


@pytest.mark.parametrize("asynchronous", [False, True])
def test_keyed_deadline_before_job_id_is_transient(asynchronous: bool) -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 6
        raise httpx.ReadTimeout("response lost after acceptance")

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_url(
                "https://audio.example/a", model=MODEL, deadline=10, idempotency_key="key-1"
            )
    error = caught.value
    assert error.job_id is None and error.phase == "job_submit"
    # Repeating the identical keyed call replays the submission under the same key.
    assert error.operation_key == "key-1" and error.is_transient is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("failure", ["deadline", "poll_error"])
def test_accepted_job_transience_follows_key_ownership(
    failure: str, keyed: bool, asynchronous: bool
) -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return accepted()
        if failure == "deadline":
            clock.now += 3
            return queued()
        return httpx.Response(503, json={"error": {"code": 4009, "retryable": True}})

    expected = DeadlineExceededError if failure == "deadline" else InternalServerError
    with client(handler, clock, asynchronous=asynchronous) as sdk:
        with pytest.raises(expected) as caught:
            sdk.transcribe_url(
                "https://audio.example/a",
                model=MODEL,
                deadline=5,
                idempotency_key="key-1" if keyed else None,
            )
    assert caught.value.job_id == "job-1"
    # An unkeyed repeat would submit a second job while job-1 may still run: resume it instead.
    assert caught.value.is_transient is keyed


@pytest.mark.parametrize("asynchronous", [False, True])
def test_file_upload_resume_deadline_before_acceptance_is_transient(asynchronous: bool) -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 6
        raise httpx.ReadTimeout("response lost")

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.resume(
                file=io.BytesIO(b"audio"),
                model=MODEL,
                operation_key="key-1",
                content_type="audio/wav",
                deadline=5,
            )
    assert caught.value.job_id is None and caught.value.phase == "upload_init"
    assert caught.value.is_transient is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("policy,transient", [("never", False), ("always", True)])
def test_sync_submit_deadline_transience_follows_replay_policy(
    policy: str, transient: bool, asynchronous: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"code": 4001, "retryable": True}},
            headers={"Retry-After": "100"},
        )

    with client(handler, asynchronous=asynchronous, sync_replay=policy) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_file(
                io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav", deadline=5
            )
    assert caught.value.phase == "sync_submit" and caught.value.job_id is None
    assert caught.value.is_transient is transient


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("policy", ["never", "always"])
@pytest.mark.parametrize("failure", POST_SEND)
def test_post_send_failure_follows_replay_policy(
    failure: Any, policy: str, asynchronous: bool
) -> None:
    clock = Clock()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise failure("Authorization: Bearer " + CREDENTIAL + " https://sensitive.example/")
        return httpx.Response(200, json={"text": "again"})

    options = {"sync_replay": policy} if policy == "always" else {}
    with client(handler, clock, asynchronous=asynchronous, **options) as sdk:
        assert sdk.sync_replay == policy
        if policy == "always":
            assert sync_upload(sdk).text == "again"
            first, second = requests
            assert first.url.path == second.url.path == "/v1/audio/transcriptions"
            assert first.content == second.content and first.headers == second.headers
            assert clock.sleeps == [0.4375]
        else:
            with pytest.raises(AmbiguousSubmissionError) as caught:
                sync_upload(sdk)
            error = caught.value
            assert len(requests) == 1 and error.is_transient is False
            assert error.operation_key and error.phase == "sync_submit"
            assert error.__cause__ is error.__context__ is None
            assert CREDENTIAL not in str(error) and "https://" not in str(error)


REPLAY_RESPONSES = (
    [
        pytest.param(
            lambda status=status: httpx.Response(status, json={"error": {"retryable": True}}),
            policy,
            False,
            policy == "always",
            id=f"guided-{status}-{policy}",
        )
        for status in (502, 503, 504)
        for policy in ("never", "always")
    ]
    + [
        pytest.param(response, policy, keyed, replays, id=f"bare-{i}-{policy}-{keyed}")
        for i, response in enumerate(BARE_5XX)
        for policy, keyed, replays in (
            ("always", False, True),
            ("never", False, False),
            ("always", True, False),
        )
    ]
    + [
        pytest.param(
            lambda status=status, error=error: httpx.Response(status, json={"error": error}),
            "always",
            False,
            False,
            id=f"permanent-{status}-{i}",
        )
        for status in (500, 503)
        for i, error in enumerate(({"retryable": False}, {"code": 4009, "retryable": False}))
    ]
)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("response,policy,keyed,replays", REPLAY_RESPONSES)
def test_server_response_follows_replay_policy(
    response: Any, policy: str, keyed: bool, replays: bool, asynchronous: bool
) -> None:
    clock = Clock()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response() if len(requests) == 1 else httpx.Response(200, json={"text": "again"})

    with client(handler, clock, asynchronous=asynchronous, sync_replay=policy) as sdk:
        if replays:
            assert sync_upload(sdk).text == "again"
            assert len(requests) == 2 and requests[0].content == requests[1].content
            assert clock.sleeps == [0.4375]
        else:
            with pytest.raises(InternalServerError) as caught:
                sync_upload(sdk, **({"idempotency_key": "key-1"} if keyed else {}))
            assert len(requests) == 1 and caught.value.is_transient is False
            assert caught.value.phase == ("job_submit" if keyed else "sync_submit")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("attempts", [1, 2, 3, 5])
@pytest.mark.parametrize(
    "failure,kind",
    [
        (httpx.ReadTimeout, APITimeoutError),
        (httpx.WriteError, APIConnectionError),
        (httpx.ReadError, APIConnectionError),
        (lambda: httpx.Response(503, json={"error": {"retryable": True}}), InternalServerError),
    ]
    + [(response, InternalServerError) for response in BARE_5XX],
)
def test_replay_exhaustion_is_bounded_and_transient(
    failure: Any, kind: Any, attempts: int, asynchronous: bool
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        if isinstance(failure, type):
            raise failure("dropped after send")
        return failure()

    with client(
        handler,
        asynchronous=asynchronous,
        sync_replay="always",
        retry_policy=RetryPolicy(max_attempts=attempts),
    ) as sdk:
        with pytest.raises(kind) as caught:
            sync_upload(sdk)
    error = caught.value
    assert type(error) is kind and not isinstance(error, AmbiguousSubmissionError)
    assert error.phase == "sync_submit" and error.is_transient
    assert error.retryable is (failure not in BARE_5XX)
    assert len(requests) == attempts


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["file", "url"])
@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("failure", ["lost", "timeout", "permanent", "unsent", "refused"])
def test_submission_failure_preserves_recovery_safety(
    failure: str, keyed: bool, source: str, asynchronous: bool
) -> None:
    posts: list[httpx.Request] = []
    failures = {
        "lost": httpx.ReadError,
        "timeout": httpx.ReadTimeout,
        "permanent": httpx.LocalProtocolError,
        "unsent": httpx.ConnectError,
    }

    def reject(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        if failure in failures:
            raise failures[failure]("submission failed")
        return unavailable()

    handler = job_api(submit=reject, sync=lambda _: refused(4008))
    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(
            InternalServerError if failure == "refused" else APIConnectionError
        ) as caught:
            submit(sdk, source, "key-1" if keyed else None)
    error = caught.value
    transient = failure != "permanent" and (keyed or failure in ("unsent", "refused"))
    assert error.phase == "job_submit" and error.job_id is None
    assert len(posts) == (1 if failure == "permanent" else RetryPolicy().max_attempts)
    assert {r.headers["idempotency-key"] for r in posts} == {error.operation_key}
    assert error.is_transient is transient
    assert error.retryable is (failure != "permanent")
    if failure == "permanent":
        assert error.retryable is False and failure_row(error) == 7


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "source,keyed,retryable",
    [(source, keyed, True) for source in ("file", "url") for keyed in (False, True)]
    + [("resume", False, True), ("url", False, False)],
)
def test_failed_job_transience_follows_recovery_entry_point(
    source: str, keyed: bool, retryable: bool, asynchronous: bool
) -> None:
    code = 5006 if retryable else 5011
    handler = job_api(
        poll=lambda _: failed_job(code, retryable),
        sync=lambda _: refused(4008),
    )
    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(TerminalJobError) as caught:
            if source == "resume":
                sdk.resume("job-1")
            else:
                submit(sdk, source, "key-1" if keyed else None)
    error = caught.value
    assert error.code == code and error.retryable is retryable and error.job_id == "job-1"
    assert error.is_transient is (retryable and not keyed and source != "resume")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "operation,key",
    [("resume", None), ("url", None), ("url", "key-1"), ("resume-upload", "saved-key")],
)
def test_occupied_slot_deadline_preserves_recovery_context(
    operation: str, key: str | None, asynchronous: bool
) -> None:
    clock = Clock()
    calls: list[httpx.Request] = []
    with (
        client(
            lambda r: calls.append(r), clock, max_concurrency=1, asynchronous=asynchronous
        ) as sdk,
        held_slot(sdk),
    ):
        with pytest.raises(DeadlineExceededError) as caught:
            if operation == "url":
                sdk.transcribe_url(
                    "https://audio.example/a", model=MODEL, deadline=0.1, idempotency_key=key
                )
            else:
                sdk.resume(
                    "job-2" if key else "job-1",
                    deadline=0.1,
                    **({"operation_key": key, "upload_id": "upload-1"} if key else {}),
                )
    error = caught.value
    assert error.phase == ("concurrency_wait" if operation == "url" else "poll")
    assert error.job_id == (None if operation == "url" else "job-2" if key else "job-1")
    assert error.is_transient is True and calls == []
    assert clock.now == pytest.approx(0 if asynchronous else 0.1)
    if operation == "resume-upload":
        assert error.operation_key == key and error.upload_id == "upload-1"
        assert error.__cause__ is error.__context__ is None
