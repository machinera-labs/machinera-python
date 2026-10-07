from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from support import AUDIO, WALL, Clock, Service, client, grant, transcribe

import machinera as m
from machinera._contract import IDEMPOTENCY_KEY_HEADER
from machinera._uploads import Grant


@pytest.mark.parametrize("field", ["expires_at", "upload_deadline", "submit_expires_at"])
@pytest.mark.parametrize("value", [True, "1700003600", 1700003600.5, None])
def test_grant_timestamp_shapes(field: str, value: object) -> None:
    expected = {"size_bytes": 4, "content_type": "audio/flac", "content_md5": "checksum"}
    data = grant(expected, **{field: value})
    if field == "submit_expires_at" and value is None:
        assert Grant.parse(data, expected, 201).submit_expires_at is None
    else:
        with pytest.raises(m.APIResponseValidationError):
            Grant.parse(data, expected, 201)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("answer", [200, 403, 412])
def test_put_spanning_grant_is_confirmed_or_refreshed(answer: int, asynchronous: bool) -> None:
    clock = Clock()
    service = Service()
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("/uploads"):
            keys.append(request.headers[IDEMPOTENCY_KEY_HEADER])
            data = grant(service.descriptor, expires_at=WALL + int(clock.now) + 3600)
            return httpx.Response(201 if len(keys) == 1 else 200, json=data)
        if request.method == "PUT" and len(service.puts) == 1:
            clock.now = 3601
            return httpx.Response(answer)
        return response

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, deadline=10000, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.initializations) == len(service.puts) == (2 if answer == 403 else 1)
    assert len(set(keys)) == 1
    assert len(service.submissions) == 1
    assert service.submissions[0]["upload_id"] == "upload-1"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_completion_replay_has_no_write_grant_and_preserves_grace(asynchronous: bool) -> None:
    clock = Clock()
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("/uploads"):
            data = grant(service.descriptor, expires_at=WALL - 1, submit_expires_at=WALL + 300)
            for field in ("put_url", "required_headers", "method"):
                data.pop(field)
            return httpx.Response(200, json=data)
        return response

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        assert transcribe(sdk).job_id == "job-1"
    assert not service.puts
    assert len(service.initializations) == len(service.submissions) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_incomplete_submission_refreshes_grant_and_keeps_identity(asynchronous: bool) -> None:
    clock = Clock()
    service = Service()
    init_keys, submit_keys = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("/uploads"):
            init_keys.append(request.headers[IDEMPOTENCY_KEY_HEADER])
            return httpx.Response(
                200, json=grant(service.descriptor, expires_at=WALL + int(clock.now) + 3600)
            )
        if request.url.path.endswith("/transcription_jobs"):
            submit_keys.append(request.headers[IDEMPOTENCY_KEY_HEADER])
            if len(submit_keys) == 1:
                clock.now = 3601
                return httpx.Response(409, json={"error": {"code": 1002, "retryable": False}})
        return response

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, deadline=10000, idempotency_key="saved-key").job_id == "job-1"
    assert len(init_keys) == len(submit_keys) == len(service.puts) == 2
    assert init_keys[0] == init_keys[1]
    assert submit_keys == ["saved-key", "saved-key"]
    assert service.puts == [AUDIO, AUDIO]
    assert service.submissions[0] == service.submissions[1]


def test_slow_concurrent_uploads_have_no_transfer_duration_deadline() -> None:
    barrier = threading.Barrier(2)

    def run(index: int) -> str | None:
        clock = Clock()
        service = Service()

        def handler(request: httpx.Request) -> httpx.Response:
            response = service(request)
            if request.method == "PUT":
                barrier.wait(timeout=5)
                clock.now = 3500
            if request.url.path.endswith("/transcription_jobs"):
                assert clock.now == 3500
            return response

        with client(
            handler, clock, limits=m.Limits(1, 2), wall_clock=lambda: WALL + clock.now
        ) as sdk:
            result = transcribe(sdk, deadline=10000, idempotency_key=f"key-{index}")
        assert len(service.puts) == len(service.submissions) == 1
        return result.job_id

    with ThreadPoolExecutor(2) as threads:
        assert list(threads.map(run, [0, 1])) == ["job-1", "job-1"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_ambiguous_submit_must_be_refused_before_keys_change(asynchronous: bool) -> None:
    clock = Clock()
    requests = []
    original_id = None
    accepted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal original_id
        requests.append(request)
        if request.url.path.endswith("/uploads"):
            upload_id = "upload-1" if original_id is None else "upload-2"
            original_id = "upload-1"
            return httpx.Response(201, json=grant(json.loads(request.content), upload_id=upload_id))
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET":
            from support import completed

            return completed()
        submits = [r for r in requests if r.url.path.endswith("/transcription_jobs")]
        if len(submits) == 1:
            raise httpx.ReadTimeout("submission response lost")
        if len(submits) == 2:
            assert len([r for r in requests if r.method == "PUT"]) == 1
            assert submits[0].content == request.content
            assert submits[0].headers["idempotency-key"] == request.headers[IDEMPOTENCY_KEY_HEADER]
            return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
        key = request.headers[IDEMPOTENCY_KEY_HEADER]
        assert key != submits[0].headers["idempotency-key"]
        accepted.setdefault(key, "job-1")
        return httpx.Response(202, json={"id": accepted[key]})

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    assert len(accepted) == 1
    assert len([r for r in requests if r.method == "PUT"]) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_observed_file_expiry_uses_new_keys_only_after_refusal(asynchronous: bool) -> None:
    clock = Clock()
    init_keys, submit_keys, puts = [], [], []
    observations = {}
    jobs = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            key = request.headers[IDEMPOTENCY_KEY_HEADER]
            init_keys.append(key)
            upload_id = f"upload-{len(init_keys)}"
            return httpx.Response(
                201,
                json=grant(
                    json.loads(request.content),
                    upload_id=upload_id,
                    put_url=f"https://storage.example/{upload_id}",
                    expires_at=WALL + int(clock.now) + 3600,
                ),
            )
        if request.method == "PUT":
            upload_id = request.url.path.removeprefix("/")
            puts.append(request.content)
            clock.now += 3500
            observations[upload_id] = clock.now + 100
            clock.now = observations[upload_id]
            return httpx.Response(200)
        if request.method == "GET":
            from support import completed

            return completed()
        upload_id = json.loads(request.content)["upload_id"]
        key = request.headers[IDEMPOTENCY_KEY_HEADER]
        submit_keys.append(key)
        if len(submit_keys) == 1:
            clock.now += 301
        if clock.now > observations[upload_id] + 300:
            return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
        jobs.setdefault(key, "job-1")
        return httpx.Response(202, json={"id": jobs[key]})

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, deadline=10000, idempotency_key="saved-key").job_id == "job-1"
    assert len(set(init_keys)) == len(set(submit_keys)) == 2
    assert puts == [AUDIO, AUDIO]
    assert len(jobs) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_saved_expired_upload_is_confirmed_before_replacement(asynchronous: bool) -> None:
    from test_upload_expiry import ExpiringUploads

    clock = Clock()
    service = ExpiringUploads(clock)
    init_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("/uploads"):
            init_keys.append(request.headers[IDEMPOTENCY_KEY_HEADER])
            if len(init_keys) == 1:
                return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
        return response

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        assert (
            sdk.resume(
                file=AUDIO,
                model="transcribe-v1",
                filename="recording.wav",
                operation_key="saved-key",
                upload_id="upload-1",
            ).job_id
            == "job-1"
        )
    assert service.puts == [("upload-2", AUDIO)]
    assert service.submits[0][0] == "saved-key"
    assert service.submits[1][0] != "saved-key"
    assert init_keys[0] != init_keys[1]
    assert len(service.accepted) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_grant_expiry_retry_still_respects_operation_deadline(asynchronous: bool) -> None:
    service = Service()
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.method == "PUT":
            clock.now = 3601
            return httpx.Response(403)
        return response

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        with pytest.raises(m.DeadlineExceededError) as caught:
            transcribe(sdk, idempotency_key="saved-key", deadline=3601.1)
    assert caught.value.operation_key == "saved-key"
    assert len(service.initializations) == len(service.puts) == 1
    assert not service.submissions


@pytest.mark.parametrize("field", ["upload_deadline", "submit_expires_at", "submit_grace_seconds"])
def test_current_grant_fields_are_required(field: str) -> None:
    expected = {"size_bytes": 4, "content_type": "audio/flac", "content_md5": "checksum"}
    data = grant(expected)
    target = data["limits"] if field == "submit_grace_seconds" else data
    target.pop(field)
    with pytest.raises(m.APIResponseValidationError):
        Grant.parse(data, expected, 201)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("grace", [None, 300, 301])
def test_observed_submission_deadline_cannot_reset(grace: int | None, asynchronous: bool) -> None:
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.url.path.endswith("/uploads"):
            deadline = (
                WALL + 300
                if len(service.initializations) == 1
                else (WALL + grace if grace is not None else None)
            )
            data = grant(service.descriptor, submit_expires_at=deadline)
            for field in ("put_url", "required_headers", "method"):
                data.pop(field)
            return httpx.Response(200, json=data)
        if request.url.path.endswith("/transcription_jobs") and len(service.submissions) == 1:
            return httpx.Response(409, json={"error": {"code": 1002, "retryable": False}})
        return response

    with client(handler, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        if grace == 300:
            assert transcribe(sdk).job_id == "job-1"
        else:
            with pytest.raises(m.UploadError, match="changed the submission deadline"):
                transcribe(sdk)
    assert not service.puts
    assert len(service.submissions) == (2 if grace == 300 else 1)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "modified", [None, "invalid", "Tue, 14 Nov 2023 20:00:00 GMT", "Tue, 14 Nov 2034 20:00:00 GMT"]
)
def test_first_observation_can_follow_local_completion_by_more_than_grace(
    asynchronous: bool, modified: str | None
) -> None:
    clock = Clock()
    service = Service()
    submissions = []
    observed_at = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_at
        answer = service(request)
        if request.method == "PUT":
            clock.now = 10
            return httpx.Response(
                200, headers={} if modified is None else {"Last-Modified": modified}
            )
        if request.url.path.endswith("/transcription_jobs"):
            submissions.append((request.headers[IDEMPOTENCY_KEY_HEADER], request.content))
            if len(submissions) == 1:
                return httpx.Response(
                    503,
                    json={"error": {"code": 4009, "retryable": True}},
                    headers={"Retry-After": "400"},
                )
            observed_at = clock.now
        return answer

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, deadline=1000, idempotency_key="saved-key").job_id == "job-1"
    assert observed_at == 410
    assert len(service.initializations) == len(service.puts) == 1
    assert submissions == [submissions[0], submissions[0]]
    assert submissions[0][0] == "saved-key"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_replay_supplies_first_observation_deadline_after_old_put(asynchronous: bool) -> None:
    clock = Clock()
    service = Service()
    supplied = []

    def handler(request: httpx.Request) -> httpx.Response:
        answer = service(request)
        if request.url.path.endswith("/uploads"):
            if len(service.initializations) == 1:
                return answer
            data = grant(service.descriptor, submit_expires_at=WALL + 700)
            for field in ("put_url", "required_headers", "method"):
                data.pop(field)
            supplied.append(data)
            return httpx.Response(200, json=data)
        if request.url.path.endswith("/transcription_jobs") and len(service.submissions) == 1:
            clock.now = 400
            return httpx.Response(409, json={"error": {"code": 1002, "retryable": False}})
        return answer

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, deadline=1000, idempotency_key="saved-key").job_id == "job-1"
    assert supplied[0]["submit_expires_at"] == WALL + 700
    assert len(service.puts) == 1
    assert service.submissions[0] == service.submissions[1]
