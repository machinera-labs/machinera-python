from __future__ import annotations

import io
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import httpcore
import httpx
import pytest
from support import API, CREDENTIAL, MODEL, WALL, Service, completed, context, result

from machinera import Limits, Machinera, RetryPolicy, TranscriptionInterrupted
from machinera._io import Cancellation, _CapturedStream


@contextmanager
def thread(action: Callable[[], Any]) -> Iterator[list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield errors
    finally:
        thread.join(1)
        assert not thread.is_alive(), "operation did not stop after cancellation"


def interrupted(errors: list[BaseException], job: str | None = "job-1") -> TranscriptionInterrupted:
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, TranscriptionInterrupted)
    assert isinstance(error, KeyboardInterrupt)
    assert error.job_id == job
    assert error.__context__ is error.__cause__ is None
    return error


@pytest.mark.parametrize("backoff", [False, True])
def test_cancel_wakes_poll_and_backoff(monkeypatch: pytest.MonkeyPatch, backoff: bool) -> None:
    waiting = threading.Event()
    original = Cancellation.wait
    calls = []

    def wait(event: Cancellation, delay: float) -> None:
        assert delay >= 60
        waiting.set()
        original(event, delay)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if backoff:
            return httpx.Response(503)
        return httpx.Response(200, json={"id": "job-1", "status": "processing"})

    monkeypatch.setattr(Cancellation, "wait", wait)
    with Machinera(
        api_key=CREDENTIAL,
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(poll_interval=100, initial_delay=100, max_delay=100),
    ) as sdk:
        with thread(lambda: sdk.resume("job-1")) as errors:
            assert waiting.wait(1)
            start = time.monotonic()
            sdk.cancel()
        assert time.monotonic() - start < 0.5  # scheduling margin, not the 50 ms contract
        interrupted(errors)
        assert len(calls) == 1


@pytest.mark.parametrize("before_headers", [False, True])
@pytest.mark.parametrize("reused", [False, True])
def test_cancel_aborts_blocked_socket_read(before_headers: bool, reused: bool) -> None:
    # An AF_UNIX socket pair exercises actual recv/shutdown without a network server.
    local, peer = socket.socketpair()
    reading = threading.Event()
    finished = threading.Event()
    stream = httpcore._backends.sync.SyncStream(local)
    network = _CapturedStream(stream) if reused else stream
    calls = []

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            try:
                if isinstance(network, _CapturedStream):
                    network._capture()
                reading.set()
                assert network.read(1, timeout=None) == b""
                yield b""
            finally:
                finished.set()

        def close(self) -> None:
            network.close()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if not reused:
                request.extensions["trace"](
                    "connection.connect_tcp.complete", {"return_value": network}
                )
            if before_headers:
                list(Body())
            return httpx.Response(200, stream=Body())

    try:
        with Machinera(api_key=CREDENTIAL, transport=Transport()) as sdk:
            with thread(lambda: sdk.resume("job-1")) as errors:
                assert reading.wait(1)
                sdk.cancel()
            interrupted(errors)
            assert finished.wait(1), "blocked recv must be aborted, not just abandoned"
            assert len(calls) == 1
    finally:
        local.close()
        peer.close()


def test_cancel_closes_custom_response_stream() -> None:
    entered, closed, finished = threading.Event(), threading.Event(), threading.Event()

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            try:
                entered.set()
                assert closed.wait(2)
                yield b"{}"
            finally:
                finished.set()

        def close(self) -> None:
            closed.set()

    with Machinera(
        api_key=CREDENTIAL,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body())),
    ) as sdk:
        with thread(lambda: sdk.get_job("job-1")) as errors:
            assert entered.wait(1)
            sdk.cancel()
        interrupted(errors)
        assert closed.wait(1) and finished.wait(1)


def test_cancel_mid_file_upload_upload_preserves_recovery() -> None:
    service = Service()
    entered, released, finished = threading.Event(), threading.Event(), threading.Event()
    partial: list[bytes] = []
    failures: list[BaseException] = []
    audio = b"audio" * 30000  # several upload chunks
    source = io.BytesIO(audio)

    class Network:
        def get_extra_info(self, name: str) -> None:
            return None

        def close(self) -> None:
            released.set()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method != "PUT":
                return service(request)
            request.extensions["trace"](
                "connection.connect_tcp.complete", {"return_value": Network()}
            )
            chunks = iter(request.stream)
            partial.append(next(chunks))
            entered.set()
            try:
                assert released.wait(2)
                # Aborting the body must prevent another chunk (and any submission).
                partial.append(next(chunks))
            except BaseException as error:
                failures.append(error)
                raise
            finally:
                finished.set()
            pytest.fail("upload continued after cancellation")

    with Machinera(
        api_key=CREDENTIAL,
        base_url=API,
        transport=Transport(),
        limits=Limits(1, 2),
        wall_clock=lambda: WALL,
    ) as sdk:
        with thread(
            lambda: sdk.transcribe_file(
                source, model=MODEL, filename="recording.wav", idempotency_key="saved-key"
            )
        ) as errors:
            assert entered.wait(1)
            sdk.cancel()
        error = interrupted(errors, None)
        context(error, "upload_put")
        assert error.wait_for_file_release(1)
        assert not source.closed
        assert finished.wait(1) and failures
        assert len(partial) == 1 and len(partial[0]) < len(audio)
        assert service.submissions == []

    # A cancelled client is terminal; resume with a new client and the original key/input.
    source.seek(0)
    with Machinera(
        api_key=CREDENTIAL,
        base_url=API,
        transport=httpx.MockTransport(service),
        wall_clock=lambda: WALL,
    ) as fresh:
        assert (
            fresh.resume(
                file=source,
                model=MODEL,
                filename="recording.wav",
                operation_key=error.operation_key,
                upload_id=error.upload_id,
            ).job_id
            == "job-1"
        )
    assert service.initializations[0] == service.initializations[1]
    assert service.puts == [audio]
    assert len(service.submissions) == 1
    assert {
        r.headers["idempotency-key"] for r in service.calls if r.url.path.endswith("uploads")
    } == {service.calls[0].headers["idempotency-key"]}
    assert not source.closed


def test_cancel_all_threads_including_concurrency_waiters() -> None:
    entered, release = threading.Event(), threading.Event()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        assert release.wait(2)
        return completed()

    with Machinera(
        api_key=CREDENTIAL, transport=httpx.MockTransport(handler), max_concurrency=1
    ) as sdk:
        executor = ThreadPoolExecutor(4)
        try:
            futures = [executor.submit(sdk.resume, "job-1") for _ in range(4)]
            assert entered.wait(1)
            sdk.cancel()
            sdk.cancel()
            for future in futures:
                with pytest.raises(TranscriptionInterrupted) as caught:
                    future.result(timeout=1)
                assert caught.value.job_id == "job-1"
            assert len(requests) == 1
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("closed", [False, True])
def test_post_cancel_calls_and_idempotency(closed: bool) -> None:
    requests = []
    with Machinera(
        api_key=CREDENTIAL,
        transport=httpx.MockTransport(lambda request: requests.append(request) or completed()),
    ) as sdk:
        sdk.cancel()
        sdk.cancel()
        if closed:
            sdk.close()
            sdk.cancel()
        actions = [
            lambda: sdk.get_job("job-1"),
            lambda: sdk.resume("job-1"),
            lambda: sdk.transcribe_file(b"audio", model=MODEL),
            lambda: sdk.transcribe_url("https://audio.example/test.wav", model=MODEL),
            lambda: sdk.resume(file=b"audio", operation_key="saved", model=MODEL),
        ]
        for action in actions:
            with pytest.raises(TranscriptionInterrupted):
                action()
        assert requests == []


def test_cancel_from_signal_handler_is_reentrant() -> None:
    with Machinera(api_key=CREDENTIAL) as sdk:
        previous = signal.signal(signal.SIGINT, lambda *_: sdk.cancel())
        try:
            # Simulate delivery while the main thread already holds the event lock.
            with sdk._lifecycle.cancellation.condition:
                signal.raise_signal(signal.SIGINT)
            with pytest.raises(TranscriptionInterrupted):
                sdk.resume("job-1")
        finally:
            signal.signal(signal.SIGINT, previous)


def test_cancel_does_not_close_borrowed_http_client_or_interrupt_other_sdk() -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("slow"):
            try:
                entered.set()
                assert release.wait(2)
                return completed(job="slow")
            finally:
                finished.set()
        return completed(job="fast")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with (
            Machinera(api_key=CREDENTIAL, http_client=http) as cancelled,
            Machinera(api_key=CREDENTIAL, http_client=http) as other,
        ):
            try:
                with thread(lambda: cancelled.resume("slow")) as errors:
                    assert entered.wait(1)
                    cancelled.cancel()
                interrupted(errors, "slow")
                assert not http.is_closed
                assert other.resume("fast").job_id == "fast"
            finally:
                release.set()
                assert finished.wait(1)
        assert not http.is_closed


def test_cancel_during_hashing_defers_file_release() -> None:
    from support import BlockedInputAccess

    tracker = BlockedInputAccess("hash")
    with Machinera(api_key=CREDENTIAL, transport=tracker.transport) as sdk:
        try:
            with thread(
                lambda: sdk.transcribe_file(
                    tracker.source, model=MODEL, content_type="audio/wav", idempotency_key="saved"
                )
            ) as errors:
                assert tracker.entered.wait(1)
                sdk.cancel()
            error = interrupted(errors, None)
            assert not error.wait_for_file_release(0)
            assert not tracker.source.closed
            accesses = tracker.file_operations.copy()
        finally:
            tracker.release_read.set()
        assert error.wait_for_file_release(1)
        assert tracker.file_operations == accesses
        assert not tracker.source.closed
        assert tracker.requests == []


def test_blocked_transport_cleanup_cannot_block_interrupted_caller() -> None:
    entered, closing, release, finished = (threading.Event() for _ in range(4))

    class Network:
        def get_extra_info(self, name: str) -> None:
            return None

        def close(self) -> None:
            closing.set()
            assert release.wait(2)

    def handler(request: httpx.Request) -> httpx.Response:
        request.extensions["trace"]("connection.connect_tcp.complete", {"return_value": Network()})
        try:
            entered.set()
            assert release.wait(2)
            return completed()
        finally:
            finished.set()

    with Machinera(api_key=CREDENTIAL, transport=httpx.MockTransport(handler)) as sdk:
        try:
            with thread(lambda: sdk.resume("job-1")) as errors:
                assert entered.wait(1)
                sdk.cancel()
                assert closing.wait(1)
            interrupted(errors)
            assert not release.is_set()
        finally:
            release.set()
            assert finished.wait(1)


def test_cancel_after_result_construction_returns_completed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Machinera._run
    constructed = []
    requests = []

    def cancel_after_result(sdk: Machinera, flow: Any) -> Any:
        output = original(sdk, flow)
        constructed.append(output)
        sdk.cancel()  # The result exists, but _operation has not exited yet.
        return output

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(200, json=result("completed output"))

    monkeypatch.setattr(Machinera, "_run", cancel_after_result)
    with Machinera(api_key=CREDENTIAL, base_url=API, transport=httpx.MockTransport(handler)) as sdk:
        output = sdk.transcribe_file(b"audio", model=MODEL, filename="recording.wav")
        assert output is constructed[0]
        assert output.text == "completed output" and output.job_id is None
        with pytest.raises(TranscriptionInterrupted):
            sdk.transcribe_file(b"audio", model=MODEL, filename="recording.wav")
    assert len(requests) == 1


@pytest.fixture
def sigint_handler() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGINT)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.usefixtures("sigint_handler")
@pytest.mark.parametrize("explicit_cancel", [False, True])
def test_interrupt_chains_to_python_handler_and_restores(explicit_cancel: bool) -> None:
    received = []

    def previous(signum: int, frame: object) -> None:
        # Cancellation must happen before delegating to the harness's handler.
        assert sdk._lifecycle.cancellation.cancelled
        received.append((signum, frame, threading.current_thread()))

    signal.signal(signal.SIGINT, previous)
    with Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True) as sdk:
        if explicit_cancel:
            sdk.cancel()
        for _ in range(2):
            signal.raise_signal(signal.SIGINT)
            sdk.cancel()
        assert len(received) == 2
        assert all(signum == signal.SIGINT for signum, _, _ in received)
        assert all(frame is not None for _, frame, _ in received)
        assert all(thread is threading.main_thread() for _, _, thread in received)
        with pytest.raises(TranscriptionInterrupted):
            sdk.resume("job-1")
    assert signal.getsignal(signal.SIGINT) is previous
    sdk.close()
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_chains_to_default_and_cancels_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = threading.Event()
    original = Cancellation.wait

    def wait(event: Cancellation, delay: float) -> None:
        waiting.set()
        original(event, delay)

    monkeypatch.setattr(Cancellation, "wait", wait)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    with Machinera(
        api_key=CREDENTIAL,
        cancel_on_interrupt=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "job-1", "status": "processing"})
        ),
        retry_policy=RetryPolicy(poll_interval=100),
    ) as sdk:
        with thread(lambda: sdk.resume("job-1")) as errors:
            assert waiting.wait(1)
            with pytest.raises(KeyboardInterrupt) as caught:
                signal.raise_signal(signal.SIGINT)
            assert type(caught.value) is KeyboardInterrupt
            assert not isinstance(caught.value, Exception)
        interrupted(errors)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_interrupt_option_refuses_construction_off_main_thread() -> None:
    previous = signal.getsignal(signal.SIGINT)
    with thread(lambda: Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True)) as errors:
        pass
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "main thread" in str(errors[0])
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_option_requires_main_thread_close() -> None:
    previous = signal.getsignal(signal.SIGINT)
    with Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True) as sdk:
        installed = signal.getsignal(signal.SIGINT)
        with thread(sdk.close) as errors:
            pass
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert not sdk._lifecycle.closed
        assert signal.getsignal(signal.SIGINT) is installed
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_default_leaves_handler_untouched_even_off_main_thread() -> None:
    previous = signal.getsignal(signal.SIGINT)

    def construct_and_close() -> None:
        with Machinera(api_key=CREDENTIAL):
            assert signal.getsignal(signal.SIGINT) is previous

    with thread(construct_and_close) as errors:
        pass
    assert not errors
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_close_preserves_later_handler() -> None:
    with Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_cancels_even_when_previous_handler_ignores_signal() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True) as sdk:
        signal.raise_signal(signal.SIGINT)
        with pytest.raises(TranscriptionInterrupted):
            sdk.resume("job-1")
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_nested_clients_restore_in_order() -> None:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    with Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True) as first:
        installed = signal.getsignal(signal.SIGINT)
        with Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True) as second:
            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGINT)
            assert first._lifecycle.cancellation.cancelled
            assert second._lifecycle.cancellation.cancelled
        assert signal.getsignal(signal.SIGINT) is installed
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.usefixtures("sigint_handler")
def test_interrupt_restores_handler_if_http_cleanup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = signal.getsignal(signal.SIGINT)
    sdk = Machinera(api_key=CREDENTIAL, cancel_on_interrupt=True)

    def fail_close(self: Machinera) -> None:
        raise RuntimeError("cleanup failed")

    with monkeypatch.context() as patch:
        patch.setattr(Machinera, "_close_http", fail_close)
        with pytest.raises(RuntimeError, match="cleanup failed"):
            sdk.close()
    assert signal.getsignal(signal.SIGINT) is previous
    sdk.close()
