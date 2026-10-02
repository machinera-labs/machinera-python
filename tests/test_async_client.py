from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_client import API, CREDENTIAL, MODEL, Clock, completed, result
from test_uploads import (
    AUDIO,
    WALL,
    InterruptedFile,
    Service,
    grant,
)

from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    AsyncMachinera,
    DeadlineExceededError,
    IntegrityError,
    JobSnapshot,
    Limits,
    Machinera,
    RetryPolicy,
    TerminalJobError,
    TimeoutPolicy,
)

pytestmark = pytest.mark.asyncio
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


async def capture_cancellation(call: Awaitable[Any], errors: list[asyncio.CancelledError]) -> Any:
    try:
        return await call
    except asyncio.CancelledError as error:
        errors.append(error)
        raise


def client(handler: Handler, clock: Clock | None = None, **kwargs: Any) -> AsyncMachinera:
    if clock is not None:

        async def sleep(delay: float) -> None:
            clock.sleep(delay)
            await asyncio.sleep(0)

        kwargs.update(clock=clock, sleeper=sleep)
    return AsyncMachinera(
        api_key=CREDENTIAL,
        base_url=API,
        transport=httpx.MockTransport(handler),
        wall_clock=lambda: WALL,
        random_source=lambda: 0.5,
        **kwargs,
    )


@pytest.mark.parametrize("text", ["  exact\ntext  ", ""])
@pytest.mark.parametrize(
    "mode,fmt",
    [("sync", "json"), ("sync", "text"), ("sync", "verbose_json")]
    + [(mode, "json") for mode in ("keyed", "size", "staged")],
)
async def test_file_transports(text: str, fmt: Any, mode: str) -> None:
    source = io.BytesIO(b"skip-" + AUDIO)
    source.seek(5)
    service = Service()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if mode == "staged":
            response = service(request)
            return completed(text) if request.method == "GET" else response
        if request.method == "GET":
            return completed(text)
        assert int(request.headers["content-length"]) == len(request.content)
        assert hashlib.md5(request.content).hexdigest() == request.headers["x-content-md5"]
        assert AUDIO in request.content and b"skip-" not in request.content
        if mode != "sync":
            return httpx.Response(202, json={"id": "job-1"})
        return (
            httpx.Response(200, text=text)
            if fmt == "text"
            else httpx.Response(200, json=result(text))
        )

    limits = Limits(1, 2) if mode == "staged" else Limits(1) if mode == "size" else Limits()
    async with client(handler, limits=limits) as sdk:
        output = await sdk.transcribe_file(
            source,
            model=MODEL,
            filename="clip.wav",
            response_format=fmt,
            idempotency_key="saved-key" if mode == "keyed" else None,
        )
    assert output.text == text
    assert source.tell() == 5 and not source.closed
    assert calls[0].url.path == (
        "/v1/audio/transcriptions"
        if mode == "sync"
        else "/v1/uploads"
        if mode == "staged"
        else "/v1/transcription_jobs"
    )
    if mode == "staged":
        assert service.puts == [AUDIO]


@pytest.mark.parametrize("phase", ["upload_init", "upload_put", "submit", "poll"])
async def test_resume_each_phase(phase: str) -> None:
    service = Service()

    async def handler(request: httpx.Request) -> httpx.Response:
        return service(request)

    async with client(handler) as sdk:
        output = await sdk.resume(
            "job-1" if phase == "poll" else None,
            file=AUDIO,
            model=MODEL,
            filename="recording.flac",
            operation_key="saved-key",
            upload_id="upload-1",
        )
    assert output.job_id == "job-1"
    if phase == "poll":
        assert [r.method for r in service.calls] == ["GET"]
    else:
        assert service.puts == [AUDIO]
        assert service.calls[-2].headers["idempotency-key"] == "saved-key"


async def test_get_job_and_url() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            assert json.loads(request.content)["url"] == "https://audio.example/clip.wav"
            return httpx.Response(202, json={"id": "job-1"})
        return completed("")

    async with client(handler) as sdk:
        snapshot = await sdk.get_job("job-1")
        assert isinstance(snapshot, JobSnapshot) and snapshot.result is not None
        assert snapshot.result.text == ""
        assert (await sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL)).text == ""
    assert [r.method for r in calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry_after", ["2", "Tue, 14 Nov 2023 22:13:22 GMT"])
async def test_retry_after_and_stable_keys(status: int, retry_after: str) -> None:
    clock = Clock()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"Retry-After": retry_after})
        return (
            httpx.Response(202, json={"id": "job-1"}) if request.method == "POST" else completed()
        )

    async with client(handler, clock) as sdk:
        await sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL)
    assert clock.sleeps == [2]
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    assert requests[0].content == requests[1].content


@pytest.mark.parametrize("status,retryable", [(401, True), (403, True), (503, False), (400, False)])
async def test_nonretryable_errors(status: int, retryable: bool) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, json={"error": {"retryable": retryable, "message": CREDENTIAL}}
        )

    async with client(handler) as sdk:
        with pytest.raises(APIError) as caught:
            await sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL)
    assert calls == 1 and caught.value.status_code == status
    assert CREDENTIAL not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.parametrize("stage", ["upload_init", "upload_put", "submit", "poll", "sync_submit"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_deadline_and_cancellation_context(stage: str, cancel: bool) -> None:
    entered = asyncio.Event()
    stopped = asyncio.Event()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        phase = (
            "upload_init"
            if request.url.path.endswith("/uploads")
            else "upload_put"
            if request.method == "PUT"
            else "poll"
            if request.method == "GET"
            else "sync_submit"
            if request.url.path.endswith("/audio/transcriptions")
            else "submit"
        )
        calls.append(phase)
        if phase == stage:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        if phase == "upload_init":
            return httpx.Response(201, json=grant(json.loads(request.content)))
        if phase == "upload_put":
            return httpx.Response(200)
        if phase == "submit":
            return httpx.Response(202, json={"id": "job-1"})
        return completed()

    cancellations: list[asyncio.CancelledError] = []
    source = io.BytesIO(AUDIO)
    async with client(handler, limits=Limits() if stage == "sync_submit" else Limits(1, 2)) as sdk:
        task = asyncio.create_task(
            capture_cancellation(
                sdk.transcribe_file(
                    source,
                    filename="clip.wav",
                    model=MODEL,
                    deadline=10 if cancel else 0.1,
                ),
                cancellations,
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
        expected = (
            asyncio.CancelledError
            if cancel
            else (AmbiguousSubmissionError if stage == "sync_submit" else DeadlineExceededError)
        )
        with pytest.raises(expected) as caught:
            await task
        error = cancellations[0] if cancel else caught.value
        assert task.cancelled() is cancel
        assert error.phase == stage
        assert error.operation_key
        assert error.upload_id == (None if stage in ("upload_init", "sync_submit") else "upload-1")
        assert error.job_id == ("job-1" if stage == "poll" else None)
        assert stopped.is_set()
        before = calls.copy()
        await asyncio.sleep(0.01)
        assert calls == before
    assert not source.closed and source.tell() == 0


@pytest.mark.parametrize("owned", [False, True])
async def test_lifecycle_waits_and_ownership(owned: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return completed()

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = client(handler) if owned else AsyncMachinera(api_key=CREDENTIAL, http_client=http)
    async with sdk:
        task = asyncio.create_task(sdk.get_job("job-1"))
        await entered.wait()
        closing = asyncio.create_task(sdk.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(APIConnectionError):
            await sdk.get_job("job-1")
        release.set()
        await task
        await closing
    assert sdk._http.is_closed is owned
    assert not http.is_closed
    await http.aclose()


@pytest.mark.parametrize("max_concurrency", [None, 1, 2])
async def test_concurrent_calls_have_separate_state(max_concurrency: int | None) -> None:
    keys: list[str] = []
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if request.method == "POST":
            key = request.headers["idempotency-key"]
            keys.append(key)
            return httpx.Response(202, json={"id": key})
        return completed(request.url.path.rsplit("/", 1)[1], request.url.path.rsplit("/", 1)[1])

    async with client(handler, max_concurrency=max_concurrency) as sdk:
        outputs = await asyncio.gather(
            *(
                sdk.transcribe_file(AUDIO, filename="clip.wav", model=MODEL, idempotency_key=key)
                for key in ("first", "second")
            )
        )
    assert [o.text for o in outputs] == ["first", "second"]
    assert set(keys) == {"first", "second"}
    assert peak == (1 if max_concurrency == 1 else 2)


async def test_semaphore_wait_counts_toward_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("No HTTP while waiting")

    async with client(handler, max_concurrency=1) as sdk:
        semaphore = sdk._lifecycle.semaphore
        assert semaphore is not None
        await semaphore.acquire()
        try:
            with pytest.raises(DeadlineExceededError) as caught:
                await sdk.resume("job-1", deadline=0.02)
            assert caught.value.job_id == "job-1"
        finally:
            semaphore.release()


@pytest.mark.parametrize("staged", [False, True])
async def test_hash_and_chunk_reads_do_not_block_loop(staged: bool) -> None:
    reading = threading.Event()
    threads: list[int] = []
    ticks = 0

    class SlowFile(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= 65536
            threads.append(threading.get_ident())
            reading.set()
            time.sleep(0.01)
            data = super().read(size)
            reading.clear()
            return data

    async def ticker() -> None:
        nonlocal ticks
        while True:
            if reading.is_set():
                ticks += 1
            await asyncio.sleep(0.001)

    service = Service()

    async def handler(request: httpx.Request) -> httpx.Response:
        return service(request) if staged else httpx.Response(200, json=result())

    task = asyncio.create_task(ticker())
    try:
        async with client(handler, limits=Limits(1, 2) if staged else Limits()) as sdk:
            await sdk.transcribe_file(SlowFile(AUDIO), filename="clip.wav", model=MODEL)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert ticks >= 5
    assert threading.get_ident() not in threads


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_hash_releases_handle(cancel: bool) -> None:
    entered = threading.Event()

    class SlowFile(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            entered.set()
            time.sleep(0.05)
            return super().read(size)

    source = SlowFile(AUDIO)
    source.seek(5)

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Preparation must not submit")

    async with client(handler) as sdk:
        task = asyncio.create_task(
            sdk.transcribe_file(
                source, filename="clip.wav", model=MODEL, deadline=5 if cancel else 0.02
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0.001)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else DeadlineExceededError):
            await task
        assert not sdk._lifecycle.files
    assert source.tell() == 5 and not source.closed


@pytest.mark.parametrize("kind", ["bytes", "handle", "tuple", "typed", "path"])
async def test_input_forms_and_sniffing(kind: str, tmp_path: Path) -> None:
    audio = b"fLaC" + AUDIO
    path = tmp_path / "audio.flac"
    path.write_bytes(audio)
    forms: dict[str, Any] = {
        "bytes": audio,
        "handle": io.BytesIO(audio),
        "tuple": (None, audio),
        "typed": (None, audio, "audio/flac"),
        "path": path,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert b'.flac"' in request.content and audio in request.content
        return httpx.Response(200, json=result())

    async with client(handler) as sdk:
        await sdk.transcribe_file(forms[kind], model=MODEL)


@pytest.mark.parametrize(
    "file,kwargs",
    [
        (b"unknown", {}),
        (b"RIFF", {}),
        (b"audio", {"filename": "bad.txt"}),
        (b"audio", {"content_type": "unknown/type"}),
        (("a.wav", b"audio"), {"filename": "b.wav"}),
    ],
)
async def test_invalid_input_before_http(file: Any, kwargs: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input must not submit")

    async with client(handler) as sdk:
        with pytest.raises(ValueError):
            await sdk.transcribe_file(file, model=MODEL, **kwargs)


async def test_terminal_error_and_malformed_snapshot() -> None:
    for payload, error in [
        ({"id": "job-1", "status": "error", "error": {"message": "private"}}, TerminalJobError),
        ({"id": "wrong", "status": "queued"}, APIResponseValidationError),
        ({"id": "job-1", "status": "cancelled"}, TerminalJobError),
    ]:

        async def handler(request: httpx.Request, payload: Any = payload) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with client(handler) as sdk:
            with pytest.raises(error):
                await sdk.resume("job-1")


async def test_signatures_match_and_methods_are_coroutines() -> None:
    for name in ("__init__", "transcribe_file", "transcribe_url", "get_job", "resume"):
        sync = inspect.signature(getattr(Machinera, name))
        asynchronous = inspect.signature(getattr(AsyncMachinera, name))
        assert list(sync.parameters) == list(asynchronous.parameters)
        if name != "__init__":
            assert sync == asynchronous
            assert inspect.iscoroutinefunction(getattr(AsyncMachinera, name))


async def test_lost_admission_and_sync_ambiguity() -> None:
    keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return completed()
        keys.append(request.headers.get("idempotency-key", ""))
        if len(keys) == 1:
            raise httpx.ReadError("private-url")
        return httpx.Response(202, json={"id": "job-1"})

    async with client(handler, Clock()) as sdk:
        await sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL)
        assert len(keys) == 2 and keys[0] == keys[1]
        keys.clear()
        with pytest.raises(AmbiguousSubmissionError) as caught:
            await sdk.transcribe_file(AUDIO, filename="clip.wav", model=MODEL)
        assert len(keys) == 1
        assert caught.value.__cause__ is caught.value.__context__ is None


async def test_poll_request_timeout_retries_same_job() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return completed()

    async with client(
        handler,
        timeout=TimeoutPolicy(poll_request=0.02),
        retry_policy=RetryPolicy(initial_delay=0.001),
    ) as sdk:
        assert (await sdk.resume("job-1")).job_id == "job-1"
    assert calls == 2


async def test_mutation_detected_on_stream() -> None:
    source = io.BytesIO(AUDIO)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            source.seek(0)
            source.write(b"changed")
            return httpx.Response(201, json=grant(json.loads(request.content)))
        pytest.fail("A changed upload must not finish")

    async with client(handler, limits=Limits(1, 2)) as sdk:
        with pytest.raises(IntegrityError):
            await sdk.transcribe_file(source, filename="clip.wav", model=MODEL)


@pytest.mark.parametrize("where", ["http", "file"])
async def test_keyboard_interrupt_keeps_recovery(where: str) -> None:
    from machinera import TranscriptionInterrupted

    async def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    async with client(handler) as sdk:
        with pytest.raises(TranscriptionInterrupted) as caught:
            if where == "http":
                await sdk.resume("job-1")
            else:
                await sdk.transcribe_file(InterruptedFile(AUDIO), filename="clip.wav", model=MODEL)
    assert caught.value.job_id == ("job-1" if where == "http" else None)
    assert caught.value.operation_key


async def test_cancel_while_opening_closes_owned_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    from machinera import _files

    source = io.BytesIO(AUDIO)
    entered = threading.Event()

    def slow_open(*args: Any) -> io.BytesIO:
        entered.set()
        time.sleep(0.03)
        return source

    monkeypatch.setattr(_files, "open", slow_open, raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("No submission after cancellation")

    async with client(handler) as sdk:
        task = asyncio.create_task(sdk.transcribe_file("clip.wav", model=MODEL))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.closed


@pytest.mark.parametrize("status", [412, 403, 503])
async def test_storage_response_recovery(status: int) -> None:
    service = Service()
    puts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        if request.method == "PUT":
            puts += 1
            if puts == 1:
                return httpx.Response(status, text="<Error><Code>AccessDenied</Code></Error>")
        return service(request)

    async with client(handler, Clock(), limits=Limits(1, 2)) as sdk:
        if status == 403:
            with pytest.raises(APIError) as caught:
                await sdk.transcribe_file(AUDIO, filename="clip.wav", model=MODEL)
            assert caught.value.upload_id == "upload-1"
            assert caught.value.storage_code == "AccessDenied"
            assert puts == 1 and not service.submissions
        else:
            assert (
                await sdk.transcribe_file(AUDIO, filename="clip.wav", model=MODEL)
            ).job_id == "job-1"
            assert puts == (2 if status == 503 else 1)


async def test_storage_credentials_and_logs_are_isolated(caplog: pytest.LogCaptureFixture) -> None:
    service = Service()

    async def handler(request: httpx.Request) -> httpx.Response:
        return service(request)

    caplog.set_level("INFO", logger="httpx")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=("private-user", "private-password"),
        cookies={"private-cookie": "value"},
        headers={"X-Private": "value"},
        follow_redirects=True,
    ) as http:
        async with AsyncMachinera(
            api_key=CREDENTIAL, http_client=http, limits=Limits(1, 2), wall_clock=lambda: WALL
        ) as sdk:
            await sdk.transcribe_file(AUDIO, filename="clip.wav", model=MODEL)
    assert "signature=" not in caplog.text
    assert "/transcription_jobs" in caplog.text


@pytest.mark.parametrize("initial", [True, False])
async def test_grant_refresh_and_bound_replay(initial: bool) -> None:
    service = Service()
    inits = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inits
        if request.url.path.endswith("/uploads"):
            inits += 1
            data = json.loads(request.content)
            if initial and inits == 1:
                return httpx.Response(200, json=grant(data, expires_at=WALL - 1))
            if not initial:
                return httpx.Response(
                    200, json=grant(data, state="bound", job_id="job-1", put_url=None)
                )
        return service(request)

    async with client(handler, limits=Limits(1, 2)) as sdk:
        assert (
            await sdk.transcribe_file(AUDIO, filename="clip.wav", model=MODEL)
        ).job_id == "job-1"
    assert inits == (2 if initial else 1)
    assert len(service.puts) == (1 if initial else 0)


async def test_cancel_partial_admission_keeps_job_and_closes_response() -> None:
    cancellations: list[asyncio.CancelledError] = []
    entered = asyncio.Event()
    closed = asyncio.Event()

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b'{"id":"job-1"}'
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, stream=ResponseStream(), headers={"x-request-id": "request-1"})

    async with client(handler) as sdk:
        task = asyncio.create_task(
            capture_cancellation(
                sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL), cancellations
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancellations[0].job_id == "job-1"
    assert cancellations[0].last_status == "queued"
    assert closed.is_set()


async def test_deadline_in_file_cleanup_is_not_success() -> None:
    clock = Clock()

    class SlowRestore(io.BytesIO):
        def seek(self, offset: int, whence: int = 0) -> int:
            if clock.now > 0:
                clock.now += 5
            return super().seek(offset, whence)

    async def handler(request: httpx.Request) -> httpx.Response:
        clock.now = 0.1
        return httpx.Response(200, json=result())

    async with client(handler, clock) as sdk:
        with pytest.raises(DeadlineExceededError):
            await sdk.transcribe_file(
                SlowRestore(AUDIO), filename="clip.wav", model=MODEL, deadline=1
            )


async def test_cancel_during_streaming_put_reads_bounded_chunks() -> None:
    cancellations: list[asyncio.CancelledError] = []
    entered = asyncio.Event()
    chunks: list[bytes] = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/uploads"):
                return httpx.Response(201, json=grant(json.loads(await request.aread())))
            assert request.method == "PUT"
            assert isinstance(request.stream, httpx.AsyncByteStream)
            async for chunk in request.stream:
                chunks.append(chunk)
                entered.set()
                await asyncio.Event().wait()
            raise AssertionError("Cancelled PUT must not finish")

    source = io.BytesIO(AUDIO)
    async with AsyncMachinera(
        api_key=CREDENTIAL, transport=Transport(), limits=Limits(1, 2), wall_clock=lambda: WALL
    ) as sdk:
        task = asyncio.create_task(
            capture_cancellation(
                sdk.transcribe_file(source, filename="clip.wav", model=MODEL), cancellations
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(chunks) == 1 and len(chunks[0]) <= 65536
    assert cancellations[0].upload_id == "upload-1"
    assert cancellations[0].phase == "upload_put"
    assert source.tell() == 0 and not source.closed
