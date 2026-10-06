from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import threading
import time
from collections.abc import Awaitable
from typing import Any

import httpx
import pytest
from support import (
    API,
    AUDIO,
    CREDENTIAL,
    MODEL,
    WALL,
    Clock,
    Service,
    accepted,
    completed,
    grant,
    request_phase,
    result,
)
from support import async_client as client

from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    AsyncMachinera,
    DeadlineExceededError,
    Limits,
    Machinera,
    RetryPolicy,
    TimeoutPolicy,
)

pytestmark = pytest.mark.asyncio


async def capture_cancellation(call: Awaitable[Any], errors: list[asyncio.CancelledError]) -> Any:
    try:
        return await call
    except asyncio.CancelledError as error:
        errors.append(error)
        raise


async def test_client_identity_equality_and_hashing() -> None:
    async with (
        AsyncMachinera(api_key=CREDENTIAL, base_url=API) as first,
        AsyncMachinera(api_key=CREDENTIAL, base_url=API) as second,
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


@pytest.mark.parametrize("text", ["  exact\ntext  ", ""])
@pytest.mark.parametrize(
    "mode,fmt",
    [("sync", "json"), ("sync", "text"), ("sync", "verbose_json")]
    + [(mode, "json") for mode in ("keyed", "size", "staged")],
)
async def test_async_transport_selection_preserves_payload_and_ownership(
    text: str, fmt: Any, mode: str
) -> None:
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
            return accepted()
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


@pytest.mark.parametrize("stage", ["upload_init", "upload_put", "submit", "poll", "sync_submit"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_deadline_and_cancellation_context(stage: str, cancel: bool) -> None:
    service = Service()
    entered = asyncio.Event()
    stopped = asyncio.Event()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        phase = request_phase(request)
        calls.append(phase)
        if phase == stage:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return service(request)

    cancellations: list[asyncio.CancelledError] = []
    source = io.BytesIO(AUDIO)
    async with client(
        handler, Clock(), limits=Limits() if stage == "sync_submit" else Limits(1, 2)
    ) as sdk:
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
async def test_interrupted_hash_releases_handle(
    cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from machinera._multipart import Multipart

    entered = threading.Event()
    release = threading.Event()
    abort = Multipart.abort

    def release_aborted_read(body: Multipart) -> None:
        abort(body)
        release.set()

    monkeypatch.setattr(Multipart, "abort", release_aborted_read)

    class SlowFile(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            entered.set()
            assert release.wait(3)
            return super().read(size)

    source = SlowFile(AUDIO)
    source.seek(5)

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Preparation must not submit")

    async with client(handler, Clock()) as sdk:
        task = asyncio.create_task(
            sdk.transcribe_file(
                source, filename="clip.wav", model=MODEL, deadline=5 if cancel else 0.3
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else DeadlineExceededError):
            await task
        assert not sdk._lifecycle.files
    assert source.tell() == 5 and not source.closed


async def test_signatures_match_and_methods_are_coroutines() -> None:
    for name in ("__init__", "transcribe_file", "transcribe_url", "get_job", "resume"):
        sync = inspect.signature(getattr(Machinera, name))
        asynchronous = inspect.signature(getattr(AsyncMachinera, name))
        if name == "__init__":
            option = sync.parameters["cancel_on_interrupt"]
            assert option.default is False
            assert option.kind is inspect.Parameter.KEYWORD_ONLY
            assert "cancel_on_interrupt" not in asynchronous.parameters
            sync = sync.replace(
                parameters=[p for p in sync.parameters.values() if p.name != "cancel_on_interrupt"]
            )
        assert list(sync.parameters) == list(asynchronous.parameters)
        if name != "__init__":
            assert sync == asynchronous
            assert inspect.iscoroutinefunction(getattr(AsyncMachinera, name))


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


async def test_cancel_while_opening_closes_owned_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    from machinera import _files

    source = io.BytesIO(AUDIO)
    entered = threading.Event()
    release = threading.Event()

    def slow_open(*args: Any) -> io.BytesIO:
        entered.set()
        assert release.wait(3)
        return source

    monkeypatch.setattr(_files, "open", slow_open, raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("No submission after cancellation")

    async with client(handler) as sdk:
        task = asyncio.create_task(sdk.transcribe_file("clip.wav", model=MODEL))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.closed


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
