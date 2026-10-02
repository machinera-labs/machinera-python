from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_client import API, CREDENTIAL, MODEL, Clock, client, completed, result

from machinera import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    DeadlineExceededError,
    IntegrityError,
    Limits,
    Machinera,
    PayloadTooLargeError,
    TerminalIntegrityError,
    TerminalJobError,
    TranscriptionInterrupted,
    UploadError,
)

AUDIO = b"audio-bytes" * 15000
WALL = 1_700_000_000
SIGNED = "https://storage.example/object?signature=private-signature"


def grant(data: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "upload_id": "upload-1",
        "state": "pending",
        "put_url": SIGNED,
        "method": "PUT",
        "required_headers": {
            "Content-Length": str(data["size_bytes"]),
            "Content-Type": data["content_type"],
            "Content-MD5": data["content_md5"],
            "If-None-Match": "*",
        },
        "expires_at": WALL + 300,
        "upload_expires_at": WALL + 3600,
        "limits": {
            "max_upload_bytes": 2**31,
            "sync_inline_body_bytes": 100,
            "async_inline_body_bytes": 200,
            "put_ttl_seconds": 300,
            "upload_window_seconds": 3600,
            "retention_max_seconds": 7200,
            "policy_revision": "v1",
        },
        **changes,
    }


class Service:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.initializations: list[dict[str, Any]] = []
        self.puts: list[bytes] = []
        self.submissions: list[dict[str, Any]] = []
        self.descriptor: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/v1/uploads":
            self.descriptor = json.loads(request.content)
            self.initializations.append(self.descriptor)
            return httpx.Response(201, json=grant(self.descriptor))
        if request.method == "PUT":
            data = request.read()
            assert len(data) == int(request.headers["content-length"])
            assert (
                base64.b64encode(hashlib.md5(data).digest()).decode()
                == request.headers["content-md5"]
            )
            assert request.headers["if-none-match"] == "*"
            assert set(request.headers) == {
                "host",
                "content-length",
                "content-type",
                "content-md5",
                "if-none-match",
            }
            self.puts.append(data)
            return httpx.Response(200)
        if request.method == "POST":
            if request.headers["content-type"] == "application/json":
                self.submissions.append(json.loads(request.content))
            return httpx.Response(202, json={"id": "job-1"})
        return completed()


def transcribe(sdk: Machinera, **kwargs: Any) -> Any:
    return sdk.transcribe_file(
        kwargs.pop("file", AUDIO), model=MODEL, filename="recording.wav", **kwargs
    )


def context(error: APIError, phase: str, upload_id: str | None = "upload-1") -> None:
    assert error.operation_key == "saved-key"
    assert error.upload_id == upload_id
    assert error.phase == phase
    assert error.__cause__ is error.__context__ is None


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize("forced", [False, True])
def test_round_trip_checksums_headers_and_replay(status: int, forced: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path == "/v1/uploads":
            response.status_code = status
        return response

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=("private-user", "private-password"),
        cookies={"secret": "cookie-value"},
        headers={"user-agent": "private-agent"},
        follow_redirects=True,
    ) as http:
        with Machinera(
            api_key=CREDENTIAL,
            base_url=API,
            http_client=http,
            transport="job" if forced else "auto",
            limits=Limits(1, 2),
            wall_clock=lambda: WALL,
        ) as sdk:
            for _ in range(2):
                output = transcribe(sdk, idempotency_key="saved-key")
                assert output.text == result()["text"]
                assert output.warnings == result()["warnings"]
    expected = {
        "size_bytes": len(AUDIO),
        "content_type": "audio/wav",
        "content_md5": base64.b64encode(hashlib.md5(AUDIO).digest()).decode(),
        "sha256": hashlib.sha256(AUDIO).hexdigest(),
    }
    assert service.initializations == [expected, expected]
    assert service.puts == [AUDIO, AUDIO]
    assert (
        service.submissions
        == [
            {
                "upload_id": "upload-1",
                "model": MODEL,
                "response_format": "json",
            }
        ]
        * 2
    )
    assert [r.headers["idempotency-key"] for r in service.calls if r.url.path == "/v1/uploads"] == [
        hashlib.sha256(b"upload-init:saved-key").hexdigest()
    ] * 2
    assert all(
        r.headers["idempotency-key"] == "saved-key"
        for r in service.calls
        if r.url.path.endswith("jobs")
    )


@pytest.mark.parametrize("step", ["upload_init", "upload_put", "submit"])
def test_lost_response_replays_same_operation(step: str) -> None:
    service = Service()
    failed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        current = (
            "upload_put"
            if request.method == "PUT"
            else "upload_init"
            if request.url.path.endswith("uploads")
            else "submit"
        )
        response = service(request)
        if current == step and not failed:
            failed = True
            raise httpx.ReadError(SIGNED)
        if current == "upload_put" and step == "upload_put":
            return httpx.Response(412)
        if current == "upload_init" and step == "upload_init":
            response.status_code = 200
        return response

    with client(handler, limits=Limits(1, 2)) as sdk:
        assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.puts) == (2 if step == "upload_put" else 1)
    for path in ("/v1/uploads", "/v1/transcription_jobs"):
        requests = [r for r in service.calls if r.url.path == path]
        assert len({r.headers["idempotency-key"] for r in requests}) == 1
        assert len({r.content for r in requests}) == 1


def test_expired_grant_refreshes_same_subkey() -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("uploads"):
            return httpx.Response(
                201 if len(service.initializations) == 1 else 200,
                json=grant(
                    service.descriptor,
                    expires_at=WALL - 1 if len(service.initializations) == 1 else WALL + 10,
                ),
            )
        return response

    with client(handler, limits=Limits(1, 2)) as sdk:
        transcribe(sdk)
    requests = [r for r in service.calls if r.url.path.endswith("uploads")]
    assert len(requests) == 2
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    assert requests[0].content == requests[1].content
    assert len(service.puts) == 1


@pytest.mark.parametrize("status", [301, 307, 401, 403])
def test_storage_errors_are_sanitized_and_never_followed(status: int, caplog: Any) -> None:
    service = Service()
    caplog.set_level("DEBUG")

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.method == "PUT":
            return httpx.Response(
                status,
                text=f"<Error><Code>AccessDenied</Code><Message>{SIGNED}</Message></Error>",
                headers={"Location": SIGNED + "redirect"},
            )
        return response

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(UploadError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    error = caught.value
    context(error, "upload_put")
    assert error.status_code == status and error.storage_code == "AccessDenied"
    assert error.code is error.body is None
    assert len(service.puts) == 1 and not service.submissions
    assert SIGNED not in str(error) + caplog.text
    assert "private-signature" not in caplog.text


@pytest.mark.parametrize(
    "code",
    [
        "<Code>https://secret.example</Code>",
        "<Code>bad value</Code>",
        "<Message>secret</Message>",
        "broken",
    ],
)
def test_storage_code_allowlist(code: str) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(403, text=f"<Error>{code}</Error>")
        return service(request)

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(UploadError) as caught:
            transcribe(sdk)
    assert caught.value.storage_code is None


@pytest.mark.parametrize(
    "status,retryable",
    [(429, True), (502, True), (503, True), (504, True), (500, False), (403, False)],
)
def test_storage_status_retryability(status: int, retryable: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(status, text="<Error><Code>Busy</Code></Error>")
        return service(request)

    with client(handler, limits=Limits(1, 2), max_retries=0) as sdk:
        with pytest.raises(UploadError) as caught:
            transcribe(sdk)
    assert caught.value.status_code == status and caught.value.retryable is retryable


@pytest.mark.parametrize("deadline", [1, 10])
def test_upload_capacity_retry_after(deadline: int) -> None:
    service = Service()
    clock = Clock()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("uploads"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    429,
                    json={"error": {"code": "upload_limit_exceeded", "retryable": True}},
                    headers={"Retry-After": "3"},
                )
        return service(request)

    with client(handler, clock, limits=Limits(1, 2)) as sdk:
        if deadline == 1:
            with pytest.raises(DeadlineExceededError) as caught:
                transcribe(sdk, deadline=deadline, idempotency_key="saved-key")
            context(caught.value, "upload_init", None)
            assert attempts == 1 and clock.sleeps == []
        else:
            transcribe(sdk, deadline=deadline)
            assert attempts == 2 and clock.sleeps == [3]


@pytest.mark.parametrize("failures", [1, 2])
def test_incomplete_upload_reputs_only_once(failures: int) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("jobs") and len(service.submissions) <= failures:
            return httpx.Response(
                409, json={"error": {"code": "upload_incomplete", "retryable": False}}
            )
        return response

    with client(handler, limits=Limits(1, 2)) as sdk:
        if failures == 2:
            with pytest.raises(APIError) as caught:
                transcribe(sdk, idempotency_key="saved-key")
            context(caught.value, "submit")
        else:
            transcribe(sdk, idempotency_key="saved-key")
    assert len(service.puts) == len(service.submissions) == 2
    assert service.submissions[0] == service.submissions[1]


@pytest.mark.parametrize(
    ("phase", "status", "code"),
    [
        ("submit", 400, "upload_integrity_mismatch"),
        ("submit", 410, "upload_expired"),
        ("submit", 409, "upload_already_bound"),
        ("submit", 404, "upload_not_found"),
        ("submit", 422, "idempotency_payload_mismatch"),
        ("upload_init", 503, "staged_uploads_unavailable"),
        ("upload_init", 422, "idempotency_payload_mismatch"),
        ("upload_init", 413, "payload_too_large"),
    ],
)
def test_terminal_service_errors(phase: str, status: int, code: str) -> None:
    service = Service()
    errors = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal errors
        if request.url.path.endswith("uploads" if phase == "upload_init" else "jobs"):
            errors += 1
            limits = {"async_inline_body_bytes": 200}
            return httpx.Response(
                status, json={"error": {"code": code, "retryable": False, "limits": limits}}
            )
        return service(request)

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(
            IntegrityError
            if code == "upload_integrity_mismatch"
            else UploadError
            if code.startswith("upload_")
            else APIError
        ) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, phase, None if phase == "upload_init" else "upload-1")
    assert caught.value.code == code and caught.value.retryable is False and errors == 1
    assert caught.value.status_code == status
    assert isinstance(caught.value, UploadError) == code.startswith("upload_")
    assert not isinstance(caught.value, TerminalJobError)
    assert len(service.puts) == (0 if phase == "upload_init" else 1)
    if code == "staged_uploads_unavailable":
        assert "cannot be sent inline" in str(caught.value)
        assert "service inline limit" in str(caught.value)


@pytest.mark.parametrize("mutation", ["size", "content"])
def test_mutated_file_fails_before_put_completes(mutation: str) -> None:
    service = Service()
    source = io.BytesIO(AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("uploads"):
            source.seek(0)
            source.write(b"changed" if mutation == "content" else AUDIO + b"more")
            source.seek(0)
        return response

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(IntegrityError) as caught:
            transcribe(sdk, file=source, idempotency_key="saved-key")
    context(caught.value, "upload_put")
    assert not service.puts and not service.submissions
    assert not source.closed and source.tell() == 0


def test_nonseekable_rejected_before_http() -> None:
    class Nonseekable(io.BytesIO):
        def seekable(self) -> bool:
            return False

    with client(lambda _: pytest.fail("unexpected HTTP"), limits=Limits(1, 2)) as sdk:
        with pytest.raises(ValueError, match="seekable"):
            transcribe(sdk, file=Nonseekable(AUDIO))


@pytest.mark.parametrize("phase", ["upload_init", "upload_put", "submit", "poll"])
def test_resume_each_phase(phase: Any) -> None:
    service = Service()
    failed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        current = (
            "upload_put"
            if request.method == "PUT"
            else "poll"
            if request.method == "GET"
            else "upload_init"
            if request.url.path.endswith("uploads")
            else "submit"
        )
        if current == phase and not failed:
            failed = True
            raise httpx.ReadError(SIGNED)
        return service(request)

    source = io.BytesIO(b"skip" + AUDIO)
    source.seek(4)
    with client(handler, limits=Limits(1, 2), max_retries=0) as sdk:
        with pytest.raises(APIConnectionError) as caught:
            transcribe(sdk, file=source, idempotency_key="saved-key")
        error = caught.value
        context(error, phase, None if phase == "upload_init" else "upload-1")
        before = len(service.calls)
        output = sdk.resume(
            error.job_id,
            file=source,
            model=MODEL,
            filename="recording.wav",
            operation_key=error.operation_key,
            upload_id=error.upload_id,
        )
    assert output.job_id == "job-1" and source.tell() == 4
    if phase == "poll":
        assert all(r.method == "GET" for r in service.calls[before:])
    else:
        assert service.calls[before].url.path == "/v1/uploads"
    assert service.puts[-1] == AUDIO


@pytest.mark.parametrize("interrupt", [False, True])
def test_deadline_and_interrupt_during_put(interrupt: bool) -> None:
    service = Service()
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            if interrupt:
                raise KeyboardInterrupt(SIGNED)
            clock.now += 5
        return service(request)

    with client(handler, clock, limits=Limits(1, 2)) as sdk:
        with pytest.raises(
            TranscriptionInterrupted if interrupt else DeadlineExceededError
        ) as caught:
            transcribe(sdk, deadline=1, idempotency_key="saved-key")
    context(caught.value, "upload_put")
    assert not service.submissions


def test_stalled_put_cancelled_without_waiting_for_transport() -> None:
    service = Service()
    release = threading.Event()
    finished = threading.Event()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                try:
                    release.wait(5)
                    request.read()
                    return httpx.Response(200)
                finally:
                    finished.set()
            request.read()
            return service(request)

    try:
        with Machinera(
            api_key=CREDENTIAL, transport=Transport(), limits=Limits(1, 2), wall_clock=lambda: WALL
        ) as sdk:
            with pytest.raises(DeadlineExceededError) as caught:
                transcribe(sdk, deadline=0.1, idempotency_key="saved-key")
        context(caught.value, "upload_put")
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


def test_selection_at_exact_cap_and_one_byte_over() -> None:
    sizes: list[int] = []

    def measure(request: httpx.Request) -> httpx.Response:
        sizes.append(len(request.content))
        return httpx.Response(200, json={"text": ""})

    with client(measure) as sdk:
        transcribe(sdk, file=b"a")
    for delta in (0, 1):
        service = Service()
        with client(service, limits=Limits(1, sizes[0])) as sdk:
            transcribe(sdk, file=b"a" * (1 + delta))
        assert bool(service.initializations) == bool(delta)
        assert service.calls[0].url.path == ("/v1/uploads" if delta else "/v1/transcription_jobs")


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("mib", [20, 40, 60])
def test_default_selection_around_staged_threshold(mib: int, keyed: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json=result())
        return response

    with client(handler) as sdk:
        transcribe(sdk, file=b"a" * (mib * 1024 * 1024), idempotency_key="k" if keyed else None)
    first = service.calls[0]
    assert first.url.path == (
        "/v1/uploads"
        if mib == 60
        else "/v1/transcription_jobs"
        if keyed or mib == 40
        else "/v1/audio/transcriptions"
    )
    assert bool(service.puts) == (mib == 60)


def unavailable(limits: dict[str, Any] | None) -> httpx.Response:
    error: dict[str, Any] = {"code": "staged_uploads_unavailable", "retryable": False}
    if limits is not None:
        error["limits"] = limits
    return httpx.Response(503, json={"error": error})


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["envelope", "contract"])
@pytest.mark.parametrize("outcome", ["accepted", "refused"])
def test_staged_unavailable_falls_back_to_inline_job(
    asynchronous: bool, source: str, outcome: str
) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/uploads":
            service.calls.append(request)
            return unavailable(
                {"async_inline_body_bytes": 2 * len(AUDIO)} if source == "envelope" else None
            )
        if request.method == "POST" and outcome == "refused":
            service.calls.append(request)
            return httpx.Response(400, json={"error": {"code": "invalid_request"}})
        return service(request)

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        if outcome == "refused":
            with pytest.raises(APIError) as caught:
                transcribe(sdk, idempotency_key="saved-key")
            context(caught.value, "job_submit", None)
        else:
            assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    uploads, submission = service.calls[:2]
    assert uploads.url.path == "/v1/uploads"
    assert (
        uploads.headers["idempotency-key"] == hashlib.sha256(b"upload-init:saved-key").hexdigest()
    )
    assert submission.url.path == "/v1/transcription_jobs"
    assert submission.headers["idempotency-key"] == "saved-key"
    assert submission.headers["content-type"].startswith("multipart/form-data")
    assert AUDIO in submission.content
    assert service.puts == [] and service.submissions == []
    assert sum(r.method == "POST" for r in service.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["envelope", "contract"])
def test_staged_unavailable_above_service_cap_raises(
    asynchronous: bool, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from machinera import _core

    monkeypatch.setattr(_core, "DEFAULT_INLINE_CAP_BYTES", len(AUDIO))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return unavailable(
            {"async_inline_body_bytes": len(AUDIO)} if source == "envelope" else None
        )

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        with pytest.raises(APIError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init", None)
    assert caught.value.code == "staged_uploads_unavailable"
    assert "cannot be sent inline" in str(caught.value)
    assert [r.url.path for r in calls] == ["/v1/uploads"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_staged_unavailable_after_grant_does_not_fall_back(asynchronous: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("uploads"):
            if service.initializations:
                service.calls.append(request)
                return unavailable({"async_inline_body_bytes": 2**30})
            service(request)
            return httpx.Response(201, json=grant(service.descriptor, expires_at=WALL - 1))
        return service(request)

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        with pytest.raises(APIError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init")
    assert caught.value.code == "staged_uploads_unavailable"
    assert [r.url.path for r in service.calls] == ["/v1/uploads", "/v1/uploads"]
    assert service.puts == []


def test_concurrent_calls_keep_keys_uploads_and_jobs_separate() -> None:
    barrier = threading.Barrier(2)
    descriptors: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("uploads"):
            key = request.headers["idempotency-key"]
            data = json.loads(request.content)
            with lock:
                descriptors[key] = data
            barrier.wait(2)
            return httpx.Response(
                201, json=grant(data, upload_id=key, put_url=f"https://storage.example/{key}")
            )
        if request.method == "PUT":
            key = request.url.path[1:]
            assert hashlib.sha256(request.content).hexdigest() == descriptors[key]["sha256"]
            return httpx.Response(200)
        if request.method == "POST":
            data = json.loads(request.content)
            assert (
                data["upload_id"]
                == hashlib.sha256(
                    ("upload-init:" + request.headers["idempotency-key"]).encode()
                ).hexdigest()
            )
            return httpx.Response(202, json={"id": data["upload_id"]})
        return completed(job=request.url.path.split("/")[-1])

    with (
        client(handler, limits=Limits(1, 2), max_concurrency=2) as sdk,
        ThreadPoolExecutor(2) as pool,
    ):
        futures = [
            pool.submit(transcribe, sdk, file=bytes([i]) * 100, idempotency_key=f"key-{i}")
            for i in range(2)
        ]
        outputs = [future.result() for future in futures]
    assert outputs[0].job_id != outputs[1].job_id and len(descriptors) == 2


def test_no_file_path_or_signed_url_in_logs(tmp_path: Path, caplog: Any) -> None:
    source = tmp_path / "private-file.wav"
    source.write_bytes(AUDIO)
    caplog.set_level("DEBUG")
    with client(Service(), limits=Limits(1, 2)) as sdk:
        sdk.transcribe_file(source, model=MODEL)
    for value in (str(source), source.name, SIGNED, "private-signature", CREDENTIAL):
        assert value not in caplog.text


@pytest.mark.parametrize(
    ("file", "metadata", "expected_type"),
    [
        (b"fLaC-audio", {}, "audio/flac"),
        (b"audio", {"filename": "recording.mp3"}, "audio/mpeg"),
        (
            b"audio",
            {"filename": "recording.wav", "content_type": "application/octet-stream"},
            "application/octet-stream",
        ),
    ],
)
def test_content_type_resolution_and_options(
    file: bytes, metadata: dict[str, str], expected_type: str
) -> None:
    service = Service()
    with client(service, limits=Limits(1, 2)) as sdk:
        output = sdk.transcribe_file(
            file, model=MODEL, language="en", response_format="text", **metadata
        )
    assert service.descriptor["content_type"] == expected_type
    assert service.submissions[0]["language"] == "en"
    assert service.submissions[0]["response_format"] == "text"
    assert output.output == result()["text"]


@pytest.mark.parametrize("state", ["bound", "admitting"])
def test_recovery_without_a_write_grant(state: str) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("uploads"):
            data = json.loads(request.content)
            response = grant(data, state=state, expires_at=WALL - 100, upload_expires_at=WALL - 50)
            for field in ("put_url", "required_headers", "method"):
                response.pop(field)
            if state == "bound":
                response["job_id"] = "job-1"
                response["limits"]["max_upload_bytes"] = 1
            return httpx.Response(200, json=response)
        return service(request)

    with client(handler, limits=Limits(1, 2)) as sdk:
        assert transcribe(sdk).job_id == "job-1"
    assert not service.puts
    assert len(service.submissions) == 1


@pytest.mark.parametrize("changed_window", [False, True])
def test_fixed_window_cannot_be_reopened_or_extended(changed_window: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        service(request)
        return httpx.Response(
            200,
            json=grant(
                service.descriptor,
                expires_at=WALL - 1,
                upload_expires_at=(WALL + 100 + len(service.initializations))
                if changed_window
                else WALL - 1,
            ),
        )

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(UploadError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init" if changed_window else "upload_put")
    assert len(service.initializations) == (2 if changed_window else 1)
    assert not service.puts


def test_retry_refreshes_grant_after_backoff() -> None:
    service = Service()
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("uploads"):
            return httpx.Response(
                201 if len(service.initializations) == 1 else 200,
                json=grant(
                    service.descriptor,
                    expires_at=WALL + 1 if len(service.initializations) == 1 else WALL + 300,
                ),
            )
        if request.method == "PUT" and len(service.puts) == 1:
            return httpx.Response(503, headers={"Retry-After": "2"})
        return response

    with Machinera(
        api_key=CREDENTIAL,
        transport=httpx.MockTransport(handler),
        limits=Limits(1, 2),
        clock=clock,
        sleeper=clock.sleep,
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        transcribe(sdk)
    assert len(service.initializations) == len(service.puts) == 2
    assert clock.sleeps == [2]


@pytest.mark.parametrize(
    "change", ["limit", "headers", "method", "url", "state", "limits", "expiry"]
)
def test_invalid_or_restrictive_grants_stop_before_storage(change: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("uploads")
        data = grant(json.loads(request.content))
        if change == "limit":
            data["limits"]["max_upload_bytes"] = 1
        elif change == "headers":
            data["required_headers"]["Authorization"] = CREDENTIAL
        elif change == "method":
            data["method"] = "POST"
        elif change == "url":
            data["put_url"] = "https://private:password@storage.example/object"
        elif change == "state":
            data["state"] = "unknown"
        elif change == "limits":
            data["limits"] = {}
        else:
            data["expires_at"] = "invalid"
        return httpx.Response(201, json=data)

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(
            PayloadTooLargeError if change == "limit" else APIResponseValidationError
        ) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init")
    if change != "limit":
        assert caught.value.status_code == 201 and caught.value.retryable is False


def test_resume_rejects_different_upload_id() -> None:
    service = Service()
    with client(service) as sdk:
        with pytest.raises(UploadError) as caught:
            sdk.resume(
                file=AUDIO,
                model=MODEL,
                filename="recording.wav",
                operation_key="saved-key",
                upload_id="original-upload",
            )
    context(caught.value, "upload_init", "original-upload")
    assert not service.puts


def test_staged_hashing_and_put_use_bounded_reads_and_restore_offset() -> None:
    reads: list[int] = []

    class Source(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 65536
            reads.append(size)
            return super().read(size)

    source = Source(b"skip" + AUDIO)
    source.seek(4)
    service = Service()
    with client(service, limits=Limits(1, 2)) as sdk:
        transcribe(sdk, file=source)
    chunks = (len(AUDIO) + 65535) // 65536
    assert len(reads) == 2 * chunks + 2
    assert not source.closed and source.tell() == 4
    assert service.puts == [AUDIO]


def test_deadline_during_staged_hashing_retains_key() -> None:
    clock = Clock()

    class Slow(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            clock.now += 5
            return super().read(size)

    with client(lambda _: pytest.fail("unexpected HTTP"), clock, limits=Limits(1, 2)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            transcribe(sdk, file=Slow(AUDIO), deadline=1, idempotency_key="saved-key")
    context(caught.value, "upload_init", None)


def test_accepted_job_outlives_deadline_with_upload_context() -> None:
    service = Service()
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "job-1", "status": "processing"})
        return service(request)

    with client(handler, clock, limits=Limits(1, 2)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            transcribe(sdk, deadline=0.5, idempotency_key="saved-key")
    context(caught.value, "poll")
    assert caught.value.job_id == "job-1" and caught.value.last_status == "processing"


def test_late_bound_initialization_recovers_job_id() -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 5
        return httpx.Response(
            200, json=grant(json.loads(request.content), state="bound", job_id="job-1")
        )

    with client(handler, clock, limits=Limits(1, 2)) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            transcribe(sdk, deadline=1, idempotency_key="saved-key")
    context(caught.value, "upload_init")
    assert caught.value.job_id == "job-1"


def test_failed_job_integrity_error_does_not_reput() -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "job-1",
                    "status": "error",
                    "error": {"code": "upload_integrity_mismatch"},
                },
            )
        return service(request)

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(TerminalIntegrityError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "poll")
    assert caught.value.job_id == "job-1" and len(service.puts) == 1
    assert isinstance(caught.value, UploadError) and isinstance(caught.value, TerminalJobError)
    assert TerminalIntegrityError.__mro__[1:3] == (IntegrityError, UploadError)
    assert TerminalJobError in TerminalIntegrityError.__mro__


def test_default_operation_keys_are_fresh_for_identical_input() -> None:
    service = Service()
    with client(service, limits=Limits(1, 2)) as sdk:
        transcribe(sdk)
        transcribe(sdk)
    for path in ("/v1/uploads", "/v1/transcription_jobs"):
        keys = [r.headers["idempotency-key"] for r in service.calls if r.url.path == path]
        assert len(set(keys)) == 2


def test_stalled_input_read_during_put_preserves_file_ownership() -> None:
    service = Service()
    release = threading.Event()
    entered = threading.Event()
    putting = False

    class Source(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            if putting:
                entered.set()
                release.wait(5)
            return super().read(size)

    source = Source(AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal putting
        response = service(request)
        if request.url.path.endswith("uploads"):
            putting = True
        return response

    try:
        with client(handler, limits=Limits(1, 2)) as sdk:
            with pytest.raises(DeadlineExceededError) as caught:
                transcribe(sdk, file=source, deadline=0.1, idempotency_key="saved-key")
        context(caught.value, "upload_put")
        assert entered.is_set() and not source.closed
        assert not caught.value.wait_for_file_release(0)
    finally:
        release.set()
    assert caught.value.wait_for_file_release(2)
    assert not source.closed and not service.submissions


def test_resume_semaphore_deadline_preserves_known_identifiers() -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(5)
        return completed()

    with client(handler, max_concurrency=1) as sdk, ThreadPoolExecutor(1) as pool:
        running = pool.submit(sdk.resume, "job-1")
        assert entered.wait(2)
        try:
            with pytest.raises(DeadlineExceededError) as caught:
                sdk.resume("job-2", operation_key="saved-key", upload_id="upload-1", deadline=0.1)
            context(caught.value, "poll")
            assert caught.value.job_id == "job-2"
        finally:
            release.set()
        running.result()


def test_local_staged_input_failure_retains_context() -> None:
    class Invalid(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise ValueError("Invalid binary input")

    with client(lambda _: pytest.fail("unexpected HTTP"), limits=Limits(1, 2)) as sdk:
        with pytest.raises(ValueError) as caught:
            transcribe(sdk, file=Invalid(AUDIO), idempotency_key="saved-key")
    assert caught.value.operation_key == "saved-key"
    assert caught.value.phase == "upload_init"
    assert caught.value.upload_id is None
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.parametrize("mount", ["all://", "https://storage.example"])
@pytest.mark.parametrize("status", [200, 307])
def test_storage_uses_injected_mount_without_client_credentials(
    mount: str, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    service = Service()
    routed: list[httpx.Request] = []
    request_hooks: list[httpx.Request] = []
    response_hooks: list[httpx.Response] = []
    caplog.set_level("DEBUG")

    def default_transport(request: httpx.Request) -> httpx.Response:
        assert request.method != "PUT", "Storage bypassed the caller's mount"
        return service(request)

    def mounted_transport(request: httpx.Request) -> httpx.Response:
        routed.append(request)
        response = service(request)
        if request.method == "PUT":
            return httpx.Response(status, headers={"Location": SIGNED + "redirect"})
        return response

    with httpx.Client(
        transport=httpx.MockTransport(default_transport),
        mounts={mount: httpx.MockTransport(mounted_transport)},
        auth=("private-user", "private-password"),
        headers={
            "Authorization": "Bearer private-client-key",
            "Cookie": "private-default-cookie",
            "User-Agent": "private-client-agent",
            "X-Default": "private-header",
        },
        cookies={"session": "private-cookie"},
        event_hooks={"request": [request_hooks.append], "response": [response_hooks.append]},
        follow_redirects=True,
        trust_env=False,
    ) as http:
        with Machinera(
            api_key=CREDENTIAL, http_client=http, limits=Limits(1, 2), wall_clock=lambda: WALL
        ) as sdk:
            if status == 200:
                assert transcribe(sdk).job_id == "job-1"
            else:
                with pytest.raises(UploadError) as caught:
                    transcribe(sdk, idempotency_key="saved-key")
                context(caught.value, "upload_put")
                assert caught.value.status_code == status
                assert not service.submissions
        assert not http.is_closed
    puts = [request for request in routed if request.method == "PUT"]
    assert len(puts) == 1 and service.puts == [AUDIO]
    assert request_hooks == service.calls
    assert any(response.request is puts[0] for response in response_hooks)
    assert not any(response.history for response in response_hooks)
    assert dict(puts[0].headers) == {
        "host": "storage.example",
        "content-length": str(len(AUDIO)),
        "content-type": "audio/wav",
        "content-md5": base64.b64encode(hashlib.md5(AUDIO).digest()).decode(),
        "if-none-match": "*",
    }
    assert SIGNED not in caplog.text and "private-signature" not in caplog.text


def test_storage_log_filter_preserves_concurrent_api_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = Service()
    entered = threading.Event()
    release = threading.Event()
    caplog.set_level("INFO", logger="httpx")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            entered.set()
            assert release.wait(5)
            raise httpx.ReadError("Connection closed")
        return service(request)

    with client(handler, limits=Limits(1, 2), max_retries=0) as sdk:
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(transcribe, sdk)
            try:
                assert entered.wait(2)
                assert sdk.get_job("job-1").status == "completed"
                assert "GET " + API + "/transcription_jobs/job-1" in caplog.text
            finally:
                release.set()
            with pytest.raises(APIConnectionError):
                running.result()
        caplog.clear()
        sdk.get_job("job-1")
        assert "GET " + API + "/transcription_jobs/job-1" in caplog.text
    assert SIGNED not in caplog.text


def test_mounted_responses_preserve_decoded_content_and_headers() -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                content=gzip.compress(response.content),
                headers={"Content-Encoding": "gzip", "X-Request-ID": "request-compressed"},
            )
        return response

    with httpx.Client(mounts={"all://": httpx.MockTransport(handler)}, trust_env=False) as http:
        with Machinera(
            api_key=CREDENTIAL, http_client=http, limits=Limits(1, 2), wall_clock=lambda: WALL
        ) as sdk:
            output = transcribe(sdk)
    assert output.text == result()["text"]
    assert output.request_id == "request-compressed"


@pytest.mark.parametrize("binding_path", ["initialization", "refresh"])
@pytest.mark.parametrize("changed_option", [None, "model", "language", "response_format"])
def test_bound_replay_validates_job_options(binding_path: str, changed_option: str | None) -> None:
    service = Service()
    accepted: dict[str, Any] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal accepted
        response = service(request)
        if request.url.path.endswith("uploads") and accepted is not None:
            if binding_path == "refresh" and len(service.initializations) == 2:
                return httpx.Response(200, json=grant(service.descriptor, expires_at=WALL - 1))
            bound = grant(service.descriptor, state="bound", job_id="job-1")
            for field in ("put_url", "required_headers", "method"):
                bound.pop(field)
            return httpx.Response(200, json=bound)
        if request.url.path.endswith("jobs"):
            assert request.headers["idempotency-key"] == "saved-key"
            submitted = service.submissions[-1]
            if accepted is None:
                accepted = submitted
            elif submitted != accepted:
                return httpx.Response(
                    422,
                    json={"error": {"code": "idempotency_payload_mismatch", "retryable": False}},
                )
        return response

    options = {"model": MODEL, "language": "en", "response_format": "json"}
    replay_options = dict(options)
    if changed_option is not None:
        replay_options[changed_option] = {
            "model": "",
            "language": "en-US",
            "response_format": "text",
        }[changed_option]
    with client(handler, limits=Limits(1, 2)) as sdk:
        original = sdk.transcribe_file(
            AUDIO, filename="recording.wav", idempotency_key="saved-key", **options
        )
        assert original.job_id == "job-1"
        replay_start = len(service.calls)
        if changed_option is None:
            replay = sdk.transcribe_file(
                AUDIO, filename="recording.wav", idempotency_key="saved-key", **replay_options
            )
            assert replay.job_id == original.job_id and replay.text == original.text
        else:
            with pytest.raises(APIError) as caught:
                sdk.transcribe_file(
                    AUDIO, filename="recording.wav", idempotency_key="saved-key", **replay_options
                )
            context(caught.value, "submit")
            assert caught.value.job_id == original.job_id
            assert caught.value.status_code == 422
            assert caught.value.code == "idempotency_payload_mismatch"
            assert caught.value.retryable is False
        replay_calls = service.calls[replay_start:]
        assert [request.method for request in replay_calls] == (
            ["POST"] * (3 if binding_path == "refresh" else 2)
            + (["GET"] if changed_option is None else [])
        )
        assert len(service.submissions) == 2
        assert service.submissions[-1] == {"upload_id": "upload-1", **replay_options}
        assert service.puts == [AUDIO]
        assert all(data == service.initializations[0] for data in service.initializations)
        init_keys = {
            request.headers["idempotency-key"]
            for request in service.calls
            if request.url.path.endswith("uploads")
        }
        assert init_keys == {hashlib.sha256(b"upload-init:saved-key").hexdigest()}
        resume_start = len(service.calls)
        assert sdk.resume(original.job_id).job_id == original.job_id
        assert [request.method for request in service.calls[resume_start:]] == ["GET"]


class InterruptedFile(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise KeyboardInterrupt


def request_phase(request: httpx.Request) -> str:
    if request.url.path.endswith("/uploads"):
        return "upload_init"
    if request.method == "PUT":
        return "upload_put"
    if request.method == "GET":
        return "poll"
    if request.url.path.endswith("/audio/transcriptions"):
        return "sync_submit"
    return "submit" if request.headers["content-type"] == "application/json" else "job_submit"


INTERRUPT_CASES = [
    ("prepare", False, Limits()),
    ("sync_submit", False, Limits()),
    ("job_submit", True, Limits()),
    ("job_submit", False, Limits(1)),
    ("upload_init", True, Limits(1, 2)),
    ("upload_put", True, Limits(1, 2)),
    ("submit", True, Limits(1, 2)),
    ("poll", True, Limits(1, 2)),
]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("phase", "keyed", "limits"), INTERRUPT_CASES)
def test_interrupt_is_ambiguous_only_for_unkeyed_sync(
    phase: str, keyed: bool, limits: Limits, asynchronous: bool
) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request_phase(request) == phase:
            raise KeyboardInterrupt
        if request.method == "POST" and request.url.path.endswith("/transcription_jobs"):
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return service(request)

    source = InterruptedFile(AUDIO) if phase == "prepare" else io.BytesIO(AUDIO)
    with client(handler, limits=limits, asynchronous=asynchronous) as sdk:
        with pytest.raises(TranscriptionInterrupted) as caught:
            transcribe(sdk, file=source, idempotency_key="saved-key" if keyed else None)
    assert caught.value.phase == phase
    assert caught.value.ambiguous is (phase == "sync_submit")
    with pytest.raises(AttributeError):
        caught.value.ambiguous = False  # type: ignore[misc]
