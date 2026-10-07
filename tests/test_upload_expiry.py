from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from support import AUDIO, WALL, Clock, client, completed, grant, transcribe

import machinera as m


class ExpiringUploads:
    def __init__(self, clock: Clock, *, failures: int = 1, slow: bool = False) -> None:
        self.clock = clock
        self.failures = failures
        self.slow = slow
        self.lock = threading.Lock()
        self.uploads: dict[str, str] = {}
        self.descriptors: list[dict[str, object]] = []
        self.puts: list[tuple[str, bytes]] = []
        self.submits: list[tuple[str, bytes]] = []
        self.accepted: dict[str, str] = {}
        self.expirations: dict[str, float] = {}
        self.lose_submit = False
        self.barrier: threading.Barrier | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return completed()
        if request.url.path.endswith("/uploads"):
            key = request.headers["idempotency-key"]
            data = json.loads(request.content)
            with self.lock:
                if key not in self.uploads:
                    self.uploads[key] = f"upload-{len(self.uploads) + 1}"
                upload_id = self.uploads[key]
                self.descriptors.append(data)
                deadline = self.expirations.setdefault(upload_id, WALL + self.clock.now + 3)
                body = grant(
                    data,
                    upload_id=upload_id,
                    put_url=f"https://storage.example/{upload_id}",
                    expires_at=int(deadline),
                    upload_deadline=int(deadline),
                )
                if upload_id in self.accepted.values():
                    body.update(state="bound", job_id="job-1")
                    for field in ("put_url", "method", "required_headers"):
                        body.pop(field)
            return httpx.Response(201, json=body)
        if request.method == "PUT":
            upload_id = request.url.path.removeprefix("/")
            with self.lock:
                self.puts.append((upload_id, request.content))
                if self.slow and len(self.puts) == 1:
                    self.clock.now += 4
            return httpx.Response(200)
        key = request.headers["idempotency-key"]
        upload_id = json.loads(request.content)["upload_id"]
        with self.lock:
            self.submits.append((key, request.content))
            if key in self.accepted:
                assert self.accepted[key] == upload_id, "Changed an accepted submission"
                return httpx.Response(202, json={"id": "job-1"})
        if self.barrier is not None and int(upload_id.split("-")[-1]) <= self.failures:
            self.barrier.wait(timeout=5)
        with self.lock:
            if int(upload_id.split("-")[-1]) <= self.failures:
                if self.slow:
                    assert WALL + self.clock.now >= self.expirations[upload_id]
                return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
            self.accepted[key] = upload_id
            if self.lose_submit:
                raise httpx.ReadTimeout("response lost")
        return httpx.Response(202, json={"id": "job-1"})


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("slow", [False, True])
def test_expired_submission_reuploads_identical_file(slow: bool, asynchronous: bool) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, slow=slow)
    source = io.BytesIO(b"skip" + AUDIO)
    source.seek(4)
    with client(
        service,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, file=source, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.uploads) == 2
    assert service.puts == [("upload-1", AUDIO), ("upload-2", AUDIO)]
    assert service.descriptors[0] == service.descriptors[1]
    assert service.submits[0][0] == "saved-key"
    assert service.submits[1][0] != "saved-key"
    assert len(service.submits) == 2 and len(service.accepted) == 1
    assert source.tell() == 4 and not source.closed
    assert clock.sleeps == [0.4375]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_expiry_recovery_obeys_retry_budget(attempts: int, asynchronous: bool) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, failures=10)
    with client(
        service,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        retry_policy=m.RetryPolicy(max_attempts=attempts),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        with pytest.raises(m.UploadError, match="retry budget.*new call") as caught:
            transcribe(sdk, idempotency_key="saved-key")
    assert caught.value.code == 1003 and not caught.value.is_transient
    assert caught.value.retryable is False and caught.value.operation_key == service.submits[-1][0]
    assert len(service.uploads) == len(service.puts) == len(service.submits) == attempts


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("deadline", [0.25, 6])
def test_expiry_recovery_requires_time_for_another_upload(
    deadline: float, asynchronous: bool
) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, slow=deadline == 6)
    with client(
        service,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        with pytest.raises(m.UploadError, match="deadline.*new call") as caught:
            transcribe(sdk, idempotency_key="saved-key", deadline=deadline)
    assert caught.value.retryable is False and not caught.value.is_transient
    assert caught.value.job_id is None and caught.value.upload_id == "upload-1"
    assert len(service.uploads) == len(service.puts) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failures", [0, 1])
def test_ambiguous_submit_replays_descriptor_without_another_upload(
    failures: int, asynchronous: bool
) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, failures=failures)
    service.lose_submit = True
    with client(
        service,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.uploads) == len(service.puts) == failures + 1
    assert service.submits[-1] == service.submits[-2]
    assert len(service.accepted) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_exhausted_ambiguous_submit_recovers_existing_job(asynchronous: bool) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, failures=0)
    service.lose_submit = True
    with client(
        service,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        retry_policy=m.RetryPolicy(max_attempts=1),
    ) as sdk:
        with pytest.raises(m.APITimeoutError):
            transcribe(sdk, idempotency_key="saved-key")
        assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.uploads) == len(service.puts) == 1
    assert service.submits[0] == service.submits[1]


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_clients_keep_replacement_keys_separate_or_identical(same_key: bool) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, failures=1 if same_key else 2)
    service.barrier = threading.Barrier(2)

    def run(index: int) -> str | None:
        key = "shared-key" if same_key else f"key-{index}"
        with client(service, limits=m.Limits(1, 2)) as sdk:
            return transcribe(sdk, idempotency_key=key).job_id

    with ThreadPoolExecutor(2) as threads:
        assert list(threads.map(run, [0, 1])) == ["job-1", "job-1"]
    assert len(service.uploads) == (2 if same_key else 4)
    assert len(service.accepted) == (1 if same_key else 2)
    assert len(service.puts) in ((3, 4) if same_key else (4,))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("phase", ["upload_init", "upload_put"])
def test_deadline_during_replacement_has_actionable_terminal_error(
    asynchronous: bool, phase: str
) -> None:
    clock = Clock()
    service = ExpiringUploads(clock)

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if len(service.uploads) == 2 and (
            request.url.path.endswith("/uploads")
            if phase == "upload_init"
            else request.method == "PUT"
        ):
            clock.now = 20
        return response

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        with pytest.raises(m.UploadError, match="deadline.*new call") as caught:
            transcribe(sdk, idempotency_key="saved-key", deadline=10)
    assert caught.value.code == 1003 and caught.value.retryable is False
    assert caught.value.phase == phase and not caught.value.is_transient
    assert len(service.submits) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("response", ["known_job", "unknown_code", "initialization"])
def test_only_definitive_unbound_submission_expiry_replaces_upload(
    asynchronous: bool, response: str
) -> None:
    clock = Clock()
    service = ExpiringUploads(clock, failures=0)

    def handler(request: httpx.Request) -> httpx.Response:
        answer = service(request)
        if request.url.path.endswith("/uploads"):
            if response == "initialization":
                return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
            if response == "known_job":
                data = answer.json()
                data.update(state="bound", job_id="job-1")
                return httpx.Response(200, json=data)
        if request.url.path.endswith("/transcription_jobs"):
            return httpx.Response(
                410,
                json={
                    "error": {
                        "code": 1999 if response == "unknown_code" else 1003,
                        "retryable": False,
                    }
                },
            )
        return answer

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        with pytest.raises(m.APIError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
    assert len(service.uploads) == 1
    assert caught.value.status_code == 410
    assert caught.value.job_id == ("job-1" if response == "known_job" else None)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_saved_replacement_context_recovers_ambiguous_submission(asynchronous: bool) -> None:
    clock = Clock()
    service = ExpiringUploads(clock)
    lost = True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads") and service.accepted:
            return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
        answer = service(request)
        if request.url.path.endswith("/transcription_jobs") and answer.status_code == 202 and lost:
            raise httpx.ReadError("response lost")
        return answer

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        with pytest.raises(m.APIConnectionError) as caught:
            transcribe(sdk, idempotency_key="saved-key")
        error = caught.value
        assert error.upload_id == "upload-2" and error.job_id is None
        assert error.operation_key != "saved-key"
        assert len(service.uploads) == len(service.puts) == 2
        lost = False
        assert (
            sdk.resume(
                file=AUDIO,
                model="transcribe-v1",
                filename="recording.wav",
                operation_key=error.operation_key,
                upload_id=error.upload_id,
            ).job_id
            == "job-1"
        )
    assert len(service.uploads) == len(service.puts) == 2
    assert service.submits[-1] == service.submits[-2]
