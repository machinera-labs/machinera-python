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
from support import (
    API,
    AUDIO,
    CREDENTIAL,
    MODEL,
    SIGNED,
    WALL,
    Clock,
    InterruptedFile,
    ReplayService,
    Service,
    TrackedFile,
    accepted,
    client,
    completed,
    context,
    file_upload_unavailable,
    grant,
    mounted_storage_client,
    recorder,
    request_phase,
    result,
    transcribe,
)

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
from machinera._files import _SUFFIX_MIME_TYPES


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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "step,failure",
    [
        ("upload_init", "lost"),
        ("upload_put", "lost"),
        ("submit", "lost"),
        ("upload_put", "503"),
        ("upload_put", "412"),
    ],
)
def test_lost_response_replays_same_operation(step: str, failure: str, asynchronous: bool) -> None:
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
            if failure == "lost":
                raise httpx.ReadError(SIGNED)
            return httpx.Response(int(failure), text="<Error><Code>AccessDenied</Code></Error>")
        if current == "upload_put" and step == "upload_put" and failure == "lost":
            return httpx.Response(412)
        if current == "upload_init" and step == "upload_init":
            response.status_code = 200
        return response

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.puts) == (2 if step == "upload_put" and failure != "412" else 1)
    for path in ("/v1/uploads", "/v1/transcription_jobs"):
        requests = [r for r in service.calls if r.url.path == path]
        assert len({r.headers["idempotency-key"] for r in requests}) == 1
        assert len({r.content for r in requests}) == 1


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("step", ["upload_init", "submit"])
def test_lost_file_upload_responses_are_transient_unless_a_job_may_exist(
    step: str, keyed: bool
) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        path = "/v1/uploads" if step == "upload_init" else "/v1/transcription_jobs"
        if request.url.path == path:
            raise httpx.ReadError("response lost after send")
        return response

    with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(APIConnectionError) as caught:
            transcribe(sdk, idempotency_key="saved-key" if keyed else None)
    error = caught.value
    assert error.phase == step and error.job_id is None
    # Only a lost submission may have created a job that an unkeyed repeat would duplicate.
    assert error.is_transient is (keyed or step == "upload_init")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_expired_grant_refreshes_same_subkey(asynchronous: bool) -> None:
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

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        assert transcribe(sdk).job_id == "job-1"
    requests = [r for r in service.calls if r.url.path.endswith("uploads")]
    assert len(requests) == 2
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    assert requests[0].content == requests[1].content
    assert len(service.puts) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [301, 307, 401, 403])
def test_storage_errors_are_sanitized_and_never_followed(
    status: int, caplog: Any, asynchronous: bool
) -> None:
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

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
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
def test_storage_code_is_kept_only_when_well_formed(code: int) -> None:
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
                    json={"error": {"code": 3001, "retryable": True}},
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
            return httpx.Response(409, json={"error": {"code": 1002, "retryable": False}})
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
        ("submit", 400, 1004),
        ("submit", 410, 1003),
        ("submit", 409, 1005),
        ("submit", 404, 1001),
        ("submit", 422, 1031),
        ("upload_init", 503, 5001),
        ("upload_init", 422, 1031),
        ("upload_init", 413, 1014),
    ],
)
def test_terminal_service_errors(phase: str, status: int, code: int) -> None:
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

    with client(handler, limits=Limits(1, 2), max_retries=0) as sdk:
        with pytest.raises(
            IntegrityError
            if code == 1004
            else UploadError
            if code in {1001, 1002, 1003, 1004, 1005, 5016}
            else APIError
        ) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, phase, None if phase == "upload_init" else "upload-1")
    assert caught.value.code == code and caught.value.retryable is False and errors == 1
    assert caught.value.status_code == status
    assert isinstance(caught.value, UploadError) == (code in {1001, 1002, 1003, 1004, 1005, 5016})
    assert not isinstance(caught.value, TerminalJobError)
    assert len(service.puts) == (0 if phase == "upload_init" else 1)
    if code == 5001:
        assert "cannot be sent as multipart" in str(caught.value)
        assert "service multipart limit" in str(caught.value)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mutation", ["size", "content"])
def test_mutated_file_fails_before_put_completes(mutation: str, asynchronous: bool) -> None:
    service = Service()
    source = io.BytesIO(AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("uploads"):
            source.seek(0)
            source.write(b"changed" if mutation == "content" else AUDIO + b"more")
            source.seek(0)
        return response

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        with pytest.raises(IntegrityError) as caught:
            transcribe(sdk, file=source, idempotency_key="saved-key")
    context(caught.value, "upload_put")
    assert not service.puts and not service.submissions
    assert not source.closed and source.tell() == 0


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("phase", ["upload_init", "upload_put", "submit", "poll"])
def test_resume_continues_from_each_failed_phase(phase: Any, asynchronous: bool) -> None:
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
    with client(handler, limits=Limits(1, 2), max_retries=0, asynchronous=asynchronous) as sdk:
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

    assert service.calls[-2].headers["idempotency-key"] == "saved-key" if phase != "poll" else True


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
    entered = threading.Event()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                try:
                    entered.set()
                    assert release.wait(3)
                    request.read()
                    return httpx.Response(200)
                finally:
                    finished.set()
            request.read()
            return service(request)

    try:
        with Machinera(
            api_key=CREDENTIAL,
            transport=Transport(),
            limits=Limits(1, 2),
            wall_clock=lambda: WALL,
            clock=Clock(),
        ) as sdk:
            with pytest.raises(DeadlineExceededError) as caught:
                transcribe(sdk, deadline=0.3, idempotency_key="saved-key")
        context(caught.value, "upload_put")
        assert entered.is_set() and not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("mib", [20, 40, 60])
def test_default_selection_around_file_upload_threshold(mib: int, keyed: bool) -> None:
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["envelope", "contract"])
@pytest.mark.parametrize("outcome", ["accepted", "refused"])
def test_file_upload_unavailable_falls_back_to_multipart_job(
    asynchronous: bool, source: str, outcome: str
) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/uploads":
            service.calls.append(request)
            return file_upload_unavailable(
                {"async_inline_body_bytes": 2 * len(AUDIO)} if source == "envelope" else None
            )
        if request.method == "POST" and outcome == "refused":
            service.calls.append(request)
            return httpx.Response(400, json={"error": {"code": 1019}})
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
def test_file_upload_unavailable_above_service_cap_raises(
    asynchronous: bool, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from machinera import _core

    monkeypatch.setattr(_core, "DEFAULT_MULTIPART_CAP_BYTES", len(AUDIO))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return file_upload_unavailable(
            {"async_inline_body_bytes": len(AUDIO)} if source == "envelope" else None
        )

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        with pytest.raises(APIError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init", None)
    assert caught.value.code == 5001
    assert "cannot be sent as multipart" in str(caught.value)
    assert [r.url.path for r in calls] == ["/v1/uploads"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_file_upload_unavailable_after_grant_does_not_fall_back(asynchronous: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("uploads"):
            if service.initializations:
                service.calls.append(request)
                return file_upload_unavailable({"async_inline_body_bytes": 2**30})
            service(request)
            return httpx.Response(201, json=grant(service.descriptor, expires_at=WALL - 1))
        return service(request)

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        with pytest.raises(APIError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init")
    assert caught.value.code == 5001
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("state", ["pending", "bound"])
def test_recovery_without_a_write_grant(state: str, asynchronous: bool) -> None:
    service = Service()
    initializations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initializations
        if request.url.path.endswith("uploads"):
            initializations += 1
            data = json.loads(request.content)
            response = grant(data, state=state, expires_at=WALL - 100, upload_deadline=WALL - 50)
            for field in ("put_url", "required_headers", "method"):
                response.pop(field)
            if state == "bound":
                response["job_id"] = "job-1"
                response["limits"]["max_upload_bytes"] = 1
            return httpx.Response(200, json=response)
        return service(request)

    with client(handler, limits=Limits(1, 2), asynchronous=asynchronous) as sdk:
        assert transcribe(sdk).job_id == "job-1"
    assert not service.puts
    assert len(service.submissions) == 1

    assert initializations == 1


@pytest.mark.parametrize("changed_deadline", [False, True])
def test_fixed_deadline_cannot_be_reopened_or_extended(changed_deadline: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        service(request)
        if request.url.path.endswith("/transcription_jobs"):
            return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
        return httpx.Response(
            200,
            json=grant(
                service.descriptor,
                expires_at=WALL - 1,
                upload_deadline=(WALL + 100 + len(service.initializations))
                if changed_deadline
                else WALL - 1,
            ),
        )

    with client(handler, limits=Limits(1, 2), max_retries=0) as sdk:
        with pytest.raises(UploadError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    context(caught.value, "upload_init" if changed_deadline else "submit")
    assert len(service.initializations) == (2 if changed_deadline else 1)
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
                    "error": {"code": 1004},
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
                transcribe(sdk, file=source, deadline=0.3, idempotency_key="saved-key")
        context(caught.value, "upload_put")
        assert entered.is_set() and not source.closed
        assert not caught.value.wait_for_file_release(0)
    finally:
        release.set()
    assert caught.value.wait_for_file_release(2)
    assert not source.closed and not service.submissions


def test_local_file_upload_input_failure_retains_context() -> None:
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mount", ["all://", "https://storage.example"])
@pytest.mark.parametrize("status", [200, 307])
def test_storage_uses_injected_mount_without_client_credentials(
    mount: str, status: int, caplog: pytest.LogCaptureFixture, asynchronous: bool
) -> None:
    service = Service()
    caplog.set_level("DEBUG")
    with mounted_storage_client(service, mount, status, asynchronous) as setup:
        sdk, http, routed, request_hooks, response_hooks = setup
        if status == 200:
            assert transcribe(sdk).job_id == "job-1"
        else:
            with pytest.raises(UploadError) as caught:
                transcribe(sdk, idempotency_key="saved-key")
            context(caught.value, "upload_put")
            assert caught.value.status_code == status
            assert not service.submissions
        sdk.aclose() if asynchronous else sdk.close()
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

    if status == 200:
        assert "/transcription_jobs" in caplog.text


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


@pytest.mark.parametrize("grant_path", ["initialization", "refresh"])
@pytest.mark.parametrize("changed_option", [None, "model", "language", "response_format"])
def test_upload_replay_validates_job_options(grant_path: str, changed_option: str | None) -> None:
    service = ReplayService(grant_path)

    options = {"model": MODEL, "language": "en", "response_format": "json"}
    replay_options = dict(options)
    if changed_option is not None:
        replay_options[changed_option] = {
            "model": "",
            "language": "en-US",
            "response_format": "text",
        }[changed_option]
    with client(service, limits=Limits(1, 2)) as sdk:
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
            assert caught.value.code == 1031
            assert caught.value.retryable is False
        replay_calls = service.calls[replay_start:]
        assert [request.method for request in replay_calls] == (
            ["POST"] * (3 if grant_path == "refresh" else 2)
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
            return accepted()
        return service(request)

    source = InterruptedFile(AUDIO) if phase == "prepare" else io.BytesIO(AUDIO)
    with client(handler, limits=limits, asynchronous=asynchronous) as sdk:
        with pytest.raises(TranscriptionInterrupted) as caught:
            transcribe(sdk, file=source, idempotency_key="saved-key" if keyed else None)
    assert caught.value.phase == phase
    assert caught.value.ambiguous is (phase == "sync_submit")
    with pytest.raises(AttributeError):
        caught.value.ambiguous = False  # type: ignore[misc]

    assert caught.value.job_id == ("job-1" if phase == "poll" else None)
    assert caught.value.operation_key


@pytest.mark.parametrize("limits", [Limits(), Limits(1, 2)])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_nonseekable_input_is_rejected_before_http(limits: Limits, asynchronous: bool) -> None:
    source = TrackedFile(AUDIO, seekable=False)
    with client(
        lambda _: pytest.fail("unexpected HTTP"), limits=limits, asynchronous=asynchronous
    ) as sdk:
        with pytest.raises(ValueError, match="seekable"):
            transcribe(sdk, file=source)
    assert not source.closed


@pytest.mark.parametrize("mode", ["multipart", "sniff", "file_upload"])
def test_preparation_deadline_restores_input_and_context(mode: str) -> None:
    clock = Clock()
    source = TrackedFile(
        b"skip-fLaC" + AUDIO, on_read=lambda: clock.sleep(5 if mode == "file_upload" else 2)
    )
    source.seek(5)
    with client(
        lambda _: pytest.fail("unexpected HTTP"),
        clock,
        limits=Limits(1, 2) if mode == "file_upload" else Limits(),
    ) as sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_file(
                source,
                model=MODEL,
                deadline=1,
                **({} if mode == "sniff" else {"content_type": "audio/wav"}),
                idempotency_key="saved-key" if mode == "file_upload" else None,
            )
    assert caught.value.phase == ("upload_init" if mode == "file_upload" else "prepare")
    assert caught.value.wait_for_file_release(1)
    assert source.tell() == 5 and not source.closed
    if mode == "file_upload":
        context(caught.value, "upload_init", None)


@pytest.mark.parametrize("mode,offset", [("multipart", 6), ("job", 4), ("file_upload", 4)])
def test_file_reads_are_bounded_and_restore_the_offset(mode: str, offset: int) -> None:
    audio = AUDIO if mode == "file_upload" else b"fLaC" + b"a" * 200_000
    source = TrackedFile(b"x" * offset + audio)
    source.seek(offset)
    service = Service()
    posts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "file_upload":
            return service(request)
        if request.method == "GET":
            return completed()
        posts.append(request.content)
        if mode == "job":
            assert b'filename="upload.flac"' in request.content
            if len(posts) == 1:
                raise httpx.ReadTimeout("lost")
            return accepted()
        return httpx.Response(200, json={"text": ""})

    with client(handler, limits=Limits(1, 2) if mode == "file_upload" else Limits()) as sdk:
        sdk.transcribe_file(
            source,
            model=MODEL,
            **({"idempotency_key": "saved"} if mode == "job" else {"content_type": "audio/wav"}),
        )
    assert source.tell() == offset and not source.closed
    if mode == "job":
        assert posts[0] == posts[1]
    if mode == "file_upload":
        assert len(source.reads) == 2 * ((len(AUDIO) + 65535) // 65536) + 2
        assert service.puts == [AUDIO]


@pytest.mark.parametrize("boundary,delta", [("sync", 0), ("sync", 1), ("job", 0), ("job", 1)])
def test_encoded_size_selects_transport_at_each_boundary(boundary: str, delta: int) -> None:
    measured: list[httpx.Request] = []
    with client(recorder(lambda _: httpx.Response(200, json={"text": ""}), measured)) as sdk:
        sdk.transcribe_file(b"fLaC", model=MODEL)
    size = len(measured[0].content)
    limits = Limits(size - delta, size) if boundary == "sync" else Limits(1, size - delta)
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            service.calls.append(request)
            return httpx.Response(200, json={"text": ""})
        if request.headers.get("content-type", "").startswith("multipart/"):
            assert b'filename="upload.flac"' in request.content
            assert int(request.headers["content-length"]) == len(request.content)
        return service(request)

    with client(handler, limits=limits) as sdk:
        sdk.transcribe_file(b"fLaC", model=MODEL)
    expected = (
        "/v1/audio/transcriptions"
        if boundary == "sync" and not delta
        else ("/v1/uploads" if boundary == "job" and delta else "/v1/transcription_jobs")
    )
    assert service.calls[0].url.path == expected
    assert bool(service.initializations) is (boundary == "job" and bool(delta))
    if expected == "/v1/transcription_jobs":
        assert len(service.calls) == 2


def test_uploaded_content_type_for_every_suffix() -> None:
    assert _SUFFIX_MIME_TYPES == {
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".mpeg": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".webm": "audio/webm",
    }
