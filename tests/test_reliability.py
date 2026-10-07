from __future__ import annotations

import hashlib
import io
import shutil
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import copy_context
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from support import (
    API,
    CREDENTIAL,
    MODEL,
    BlockedInputAccess,
    Clock,
    accepted,
    client,
    completed,
    keepalive_server,
    open_client,
    result,
)

import machinera
from machinera import (
    AmbiguousSubmissionError,
    APIConnectionError,
    BadRequestError,
    DeadlineExceededError,
    Machinera,
    RetryPolicy,
    TimeoutPolicy,
)
from machinera._io import _CapturedStream, _CapturingBackend, _driver


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("guidance", [None, True])
@pytest.mark.parametrize(
    "code,status",
    [
        (4018, 400),
        (4021, 400),
        (4019, 408),
    ],
)
def test_retryable_refusals_replay_exact_bytes(
    keyed: bool, guidance: bool | None, code: int, status: int
) -> None:
    clock = Clock()
    submissions = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return completed()
        submissions.append(request)
        if len(submissions) == 1:
            detail: dict[str, Any] = {"code": code}
            if guidance is not None:
                detail["retryable"] = guidance
            return httpx.Response(status, json={"error": detail}, headers={"Retry-After": "2"})
        if keyed:
            return accepted()
        return httpx.Response(200, json={"text": ""})

    with client(handler, clock) as sdk:
        sdk.transcribe_file(
            io.BytesIO(b"audio"),
            model=MODEL,
            idempotency_key="saved" if keyed else None,
            content_type="audio/wav",
        )
    assert len(submissions) == 2 and clock.sleeps == [2]
    assert submissions[0].content == submissions[1].content
    assert submissions[0].headers == submissions[1].headers
    assert (
        hashlib.md5(submissions[0].content).hexdigest() == submissions[0].headers["x-content-md5"]
    )
    if keyed:
        assert submissions[1].headers["idempotency-key"] == "saved"


@pytest.mark.parametrize("deadline", [None, 1])
def test_transfer_refusal_obeys_attempt_and_deadline_bounds(deadline: int | None) -> None:
    requests = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, json={"error": {"code": 4018}}, headers={"Retry-After": "2"})

    with client(handler, clock, retry_policy=RetryPolicy(max_attempts=2)) as sdk:
        with pytest.raises(BadRequestError if deadline is None else DeadlineExceededError):
            sdk.transcribe_file(
                io.BytesIO(b"audio"), model=MODEL, deadline=deadline, content_type="audio/wav"
            )
    assert len(requests) == (2 if deadline is None else 1)


def test_random_boundary_preserves_audio_containing_multipart_framing() -> None:
    audio = (
        b"prefix\r\n--machinera-python-multipart-boundary\r\n"
        b'Content-Disposition: form-data; name="model"\r\n\r\ninjected\r\nsuffix'
    )
    boundaries = []

    def handler(request: httpx.Request) -> httpx.Response:
        boundary = request.headers["content-type"].split("boundary=")[1]
        boundaries.append(boundary)
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        parts = list(message.iter_parts())
        assert len(parts) == 3
        assert parts[-1].get_payload(decode=True) == audio
        assert parts[0].get_payload(decode=True) == MODEL.encode()
        assert int(request.headers["content-length"]) == len(request.content)
        return httpx.Response(200, json={"text": ""})

    with client(handler) as sdk:
        for _ in range(2):
            sdk.transcribe_file(io.BytesIO(audio), model=MODEL, content_type="audio/wav")
    assert boundaries[0] != boundaries[1]


@pytest.mark.parametrize("split", [False, True])
def test_boundary_collision_rejected_including_chunk_join(monkeypatch: Any, split: bool) -> None:
    monkeypatch.setattr("machinera._multipart.secrets.token_hex", lambda _: "a" * 48)
    marker = b"machinera-" + b"a" * 48
    audio = (b"x" * (65536 - 10) if split else b"") + marker
    requests = []
    source = io.BytesIO(audio)
    with client(lambda r: requests.append(r)) as sdk:
        with pytest.raises(ValueError, match="boundary"):
            sdk.transcribe_file(source, model=MODEL, content_type="audio/wav")
    assert requests == [] and source.tell() == 0 and not source.closed


@pytest.mark.parametrize("field", ["model", "language"])
@pytest.mark.parametrize(
    "bad", ['bad"value', "bad\rvalue", "bad\nvalue", "bad\x00value", "bad\x7fvalue"]
)
def test_multipart_fields_reject_framing_characters(field: str, bad: str) -> None:
    requests = []
    fields = {"model": MODEL, field: bad}
    with client(lambda r: requests.append(r)) as sdk:
        with pytest.raises(
            ValueError, match="controls or quotes" if field == "model" else "language must be"
        ):
            sdk.transcribe_file(io.BytesIO(b"audio"), **fields, content_type="audio/wav")
    assert requests == []


@contextmanager
def stalled_server(mode: str) -> Iterator[tuple[str, threading.Event, threading.Event]]:
    accepted = threading.Event()
    disconnected = threading.Event()
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.respond()

        def do_GET(self) -> None:
            self.respond()

        def respond(self) -> None:
            accepted.set()
            try:
                if mode == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                elif mode == "headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                else:
                    self.connection.settimeout(3)
                    if self.connection.recv(1) == b"":
                        disconnected.set()
                    return
                for _ in range(100):
                    if stop.wait(0.02):
                        break
                    self.wfile.write(b"a")
                    self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", accepted, disconnected
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode", ["stall", "headers", "body"])
@pytest.mark.parametrize("keyed", [False, True])
def test_real_http_deadline_bounds_blocked_exchange(mode: str, keyed: bool) -> None:
    with stalled_server(mode) as (endpoint, accepted, disconnected):
        with Machinera(api_key=CREDENTIAL, base_url=endpoint) as sdk:
            start = time.monotonic()
            with pytest.raises(
                DeadlineExceededError if keyed else AmbiguousSubmissionError
            ) as caught:
                sdk.transcribe_file(
                    io.BytesIO(b"audio"),
                    model=MODEL,
                    deadline=0.2,
                    idempotency_key="saved" if keyed else None,
                    content_type="audio/wav",
                )
            elapsed = time.monotonic() - start
            assert accepted.is_set()
            assert elapsed < 0.65
            assert caught.value.operation_key
            assert caught.value.phase == ("job_submit" if keyed else "sync_submit")
            assert caught.value.__context__ is None
            if mode == "stall":
                assert disconnected.wait(1)


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_real_http_poll_request_has_total_elapsed_budget(mode: str) -> None:
    with stalled_server(mode) as (endpoint, accepted, disconnected):
        with Machinera(
            api_key=CREDENTIAL,
            base_url=endpoint,
            timeout=TimeoutPolicy(poll_request=0.2),
            retry_policy=RetryPolicy(max_attempts=1),
        ) as sdk:
            start = time.monotonic()
            with pytest.raises(APIConnectionError) as caught:
                sdk.resume("job-1", deadline=5)
            assert time.monotonic() - start < 0.65
            assert accepted.is_set() and caught.value.job_id == "job-1"
            assert caught.value.retryable is True


def test_transport_without_cancellation_cannot_hold_caller_or_file_open() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    source = io.BytesIO(b"audio")
    errors: list[BaseException] = []

    class BlockedTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            try:
                stream = iter(request.stream)
                next(stream)
                entered.set()
                assert release.wait(3)
                try:
                    next(stream)
                except BaseException as error:
                    errors.append(error)
                return httpx.Response(200, json={"text": ""})
            finally:
                finished.set()

    sdk = Machinera(api_key=CREDENTIAL, base_url=API, transport=BlockedTransport(), clock=lambda: 0)
    try:
        start = time.monotonic()
        with pytest.raises(DeadlineExceededError):
            sdk.transcribe_file(
                source, model=MODEL, idempotency_key="saved", deadline=0.1, content_type="audio/wav"
            )
        sdk.close()
        assert time.monotonic() - start < 0.5 and entered.is_set()
        assert source.tell() == 0 and not source.closed
        source.close()
    finally:
        release.set()
        assert finished.wait(2)
        sdk.close()
    assert len(errors) == 1 and isinstance(errors[0], DeadlineExceededError)


def test_slow_upload_does_not_reset_budget_for_stalled_response_headers() -> None:
    class SlowUpload(io.BytesIO):
        reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if self.reads == 3:
                threading.Event().wait(0.25)
            return super().read(size)

    with stalled_server("stall") as (endpoint, accepted, disconnected):
        with Machinera(api_key=CREDENTIAL, base_url=endpoint) as sdk:
            source = SlowUpload(b"audio")
            start = time.monotonic()
            with pytest.raises(DeadlineExceededError):
                sdk.transcribe_file(
                    source,
                    model=MODEL,
                    idempotency_key="saved",
                    deadline=0.4,
                    content_type="audio/wav",
                )
            assert time.monotonic() - start < 0.58
            assert accepted.is_set() and disconnected.wait(1)
            assert source.tell() == 0 and not source.closed


def test_cancelled_exchange_does_not_change_a_later_call() -> None:
    release = threading.Event()
    finished = threading.Event()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("slow"):
            try:
                assert release.wait(3)
                return completed(job="slow")
            finally:
                finished.set()
        return completed(job="fast")

    with Machinera(
        api_key=CREDENTIAL,
        base_url=API,
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    ) as sdk:
        try:
            with pytest.raises(DeadlineExceededError) as caught:
                sdk.resume("slow", deadline=0.1)
            assert sdk.resume("fast").job_id == "fast"
            release.set()
            assert finished.wait(1)
            assert caught.value.job_id == "slow"
            assert sdk.resume("fast").job_id == "fast"
        finally:
            release.set()
    assert len(requests) == 3


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("blocked_phase", ["hash", "restore", "upload"])
def test_deadline_during_input_access_releases_caller_before_input(
    monkeypatch: Any, owned: bool, blocked_phase: str
) -> None:
    tracker = BlockedInputAccess(blocked_phase)
    if owned:
        monkeypatch.setattr("machinera._files.open", lambda *args: tracker.source, raising=False)
    # A frozen operation clock gives every bounded phase the full deadline, so the
    # phases before the blocked one cannot spend it on a slow runner and expire the
    # call before the blocking access is reached; only the blocked phase's own
    # real-time watchdog can fire.
    sdk = Machinera(api_key=CREDENTIAL, base_url=API, transport=tracker.transport, clock=Clock())
    try:
        with sdk:
            with pytest.raises(DeadlineExceededError) as caught:
                sdk.transcribe_file(
                    "recording.wav" if owned else tracker.source,
                    model=MODEL,
                    idempotency_key="saved",
                    deadline=0.3,
                    content_type="audio/wav",
                )
            assert tracker.entered.is_set() and not tracker.release_read.is_set()
            assert time.monotonic() - tracker.entered_at[0] < 0.75
            assert tracker.source.reads == tracker.expected_reads and not tracker.source.closed
            assert not caught.value.wait_for_file_release(0)
            if blocked_phase == "upload":
                assert tracker.connection_closed.wait(0.5)
            else:
                assert tracker.requests == [] and not tracker.connection_closed.is_set()
                assert caught.value.phase == "prepare"
            if not owned:
                with pytest.raises(ValueError, match="simultaneous"):
                    sdk.transcribe_file(tracker.source, model=MODEL, content_type="audio/wav")
            operations_at_abort = tracker.file_operations.copy()
        assert time.monotonic() - tracker.entered_at[0] < 0.85
        assert not tracker.client_closed.is_set() and not tracker.source.closed
        tracker.release_read.set()
        assert caught.value.wait_for_file_release(1)
        assert tracker.client_closed.wait(1)
        if blocked_phase == "upload":
            assert tracker.exchange_finished.wait(1)
        else:
            assert tracker.requests == [] and not tracker.exchange_finished.is_set()
        assert tracker.file_operations == operations_at_abort
        assert tracker.source.reads == tracker.expected_reads
        assert tracker.source.closed is owned
        if not owned:
            tracker.source.seek(0)
            assert tracker.source.read() == b"audio"
            tracker.source.close()
    finally:
        tracker.release_read.set()
        sdk.close()


def test_file_release_check_is_immediate_without_a_pending_read() -> None:
    with client(lambda _: httpx.Response(400, json={"error": {"code": 1019}})) as sdk:
        with pytest.raises(BadRequestError) as caught:
            sdk.transcribe_file(io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav")
    assert caught.value.wait_for_file_release(0)
    assert BadRequestError("Invalid input").wait_for_file_release(0)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deadline_during_file_acquisition_releases_exchange_and_client(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    clock = Clock()

    class UnreadFile(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            pytest.fail("Preparation must not start after the deadline")

    source = UnreadFile(b"audio")

    def acquire(*args: object) -> UnreadFile:
        clock.now += 2
        return source

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("No HTTP after the deadline")

    monkeypatch.setattr("machinera._files.open", acquire, raising=False)
    sdk = client(handler, clock, asynchronous=asynchronous)
    with sdk:
        with pytest.raises(DeadlineExceededError) as caught:
            sdk.transcribe_file("clip.wav", model=MODEL, deadline=1)
        assert caught.value.phase == "prepare"
        assert caught.value.wait_for_file_release(0)
        assert source.closed
        assert not sdk._lifecycle.files
        assert sdk._lifecycle.active == getattr(sdk._lifecycle, "exchanges", 0) == 0
    assert sdk._http.is_closed


@pytest.mark.parametrize("phase", ["prepare", "request"])
@pytest.mark.parametrize("failure", ["construct", "start", "after_start"])
def test_thread_startup_failure_releases_exchange_and_client(
    monkeypatch: pytest.MonkeyPatch, phase: str, failure: str
) -> None:
    source = io.BytesIO(b"audio")
    threads: list[threading.Thread] = []

    class FailedThread(threading.Thread):
        def __init__(self, **kwargs: Any) -> None:
            if failure == "construct":
                raise RuntimeError("Cannot construct thread")
            super().__init__(**kwargs)
            threads.append(self)

        def start(self) -> None:
            if failure == "after_start":
                super().start()
            raise RuntimeError("Cannot start thread")

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("An abandoned exchange must not send HTTP")

    monkeypatch.setattr("machinera._files.open", lambda *args: source, raising=False)
    monkeypatch.setattr("machinera._io.threading.Thread", FailedThread)
    sdk = client(handler)
    with sdk:
        with pytest.raises(RuntimeError, match="Cannot"):
            if phase == "prepare":
                sdk.transcribe_file("clip.wav", model=MODEL)
            else:
                sdk.get_job("job-1")
        for thread in threads:
            if failure == "after_start":
                thread.join(timeout=1)
                assert not thread.is_alive()
        assert not sdk._lifecycle.files
        assert sdk._lifecycle.active == sdk._lifecycle.exchanges == 0
        if phase == "prepare":
            assert source.closed
    assert sdk._http.is_closed


@pytest.mark.parametrize("asynchronous", [False, True])
def test_consecutive_polls_reuse_one_connection(asynchronous: bool) -> None:
    with keepalive_server() as (endpoint, state):
        with open_client(
            asynchronous, lambda _: None, api_key=CREDENTIAL, base_url=endpoint
        ) as sdk:
            for _ in range(3):
                sdk.get_job("job-1")
            assert sdk.resume("job-done").text == result()["text"]
        assert state["gets"] == 4
        assert state["connections"] == 1


def test_expired_upload_closes_its_connection_and_polls_continue() -> None:
    with keepalive_server() as (endpoint, state):
        with Machinera(api_key=CREDENTIAL, base_url=endpoint) as sdk:
            sdk.get_job("job-1")
            with pytest.raises(AmbiguousSubmissionError):
                sdk.transcribe_file(
                    io.BytesIO(b"audio"), model=MODEL, deadline=0.2, content_type="audio/wav"
                )
            assert state["disconnected"].wait(1)
            assert sdk.get_job("job-1").status == "processing"
        assert state["connections"] == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_injected_client_serves_polls_and_stays_open(asynchronous: bool) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completed()

    kind = httpx.AsyncClient if asynchronous else httpx.Client
    http = kind(transport=httpx.MockTransport(handler))
    with open_client(asynchronous, api_key=CREDENTIAL, base_url=API, http_client=http) as sdk:
        sdk.resume("job-1")
        assert sdk._poll_http is http
    assert not http.is_closed
    assert [request.method for request in requests] == ["GET"]


def assert_stalled_poll_recovers(sdk: Any, state: dict[str, Any]) -> None:
    # Only the stalled poll runs under the client's short poll_request; the calls that
    # open a connection (and TLS handshake) get the default budget, so a slow runner
    # cannot time them out.
    unhurried = TimeoutPolicy(read=None)
    sdk.get_job("job-1", timeout=unhurried)
    start = time.monotonic()
    with pytest.raises(APIConnectionError):
        sdk.get_job("job-1")
    assert time.monotonic() - start < 0.65
    assert state["disconnected"].wait(1)
    stop = time.monotonic() + 1
    while getattr(sdk._lifecycle, "exchanges", 0) and time.monotonic() < stop:
        time.sleep(0.01)
    assert getattr(sdk._lifecycle, "exchanges", 0) == 0
    assert sdk.get_job("job-1", timeout=unhurried).status == "processing"


@pytest.mark.parametrize(
    "asynchronous,before_headers", [(False, False), (False, True), (True, True)]
)
def test_expired_poll_closes_reused_connection(asynchronous: bool, before_headers: bool) -> None:
    with keepalive_server(stalled_get=2, before_headers=before_headers) as (endpoint, state):
        with open_client(
            asynchronous,
            api_key=CREDENTIAL,
            base_url=endpoint,
            timeout=TimeoutPolicy(read=None, poll_request=0.2),
            retry_policy=RetryPolicy(max_attempts=1),
        ) as sdk:
            assert_stalled_poll_recovers(sdk, state)
        assert state["connections"] == 2
        assert sdk._poll_http.is_closed and sdk._http.is_closed
        assert sdk._poll_http is not sdk._http


class FakeStream:
    def __init__(self, name: str) -> None:
        self.name = name

    def start_tls(self, *args: Any) -> FakeStream:
        return FakeStream("tls")

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b"r"

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        pass


def test_tls_upgrade_keeps_socket_captures() -> None:
    captures: list[Any] = []

    class Owner:
        def capture(self, network: Any) -> None:
            captures.append(network)

    upgraded = _CapturedStream(FakeStream("tcp")).start_tls(ssl.create_default_context())  # type: ignore[arg-type]
    assert isinstance(upgraded, _CapturedStream)

    def drive() -> None:
        _driver.set(Owner())  # type: ignore[arg-type]
        assert upgraded.read(1) == b"r"
        upgraded.write(b"w")

    copy_context().run(drive)
    assert [capture.name for capture in captures] == ["tls", "tls"]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_expired_poll_before_headers_closes_reused_tls_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        + ["-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1"]
        + ["-addext", "subjectAltName=IP:127.0.0.1"],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=str(cert))

    class TrustingTransport(httpx.HTTPTransport):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(verify=client_context, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", TrustingTransport)
    with keepalive_server(stalled_get=2, before_headers=True, tls=server_context) as (
        endpoint,
        state,
    ):
        with Machinera(
            api_key=CREDENTIAL,
            base_url=endpoint,
            timeout=TimeoutPolicy(read=None, poll_request=0.2),
            retry_policy=RetryPolicy(max_attempts=1),
        ) as sdk:
            assert_stalled_poll_recovers(sdk, state)
        assert state["connections"] == 2
        assert sdk._poll_http.is_closed and sdk._http.is_closed


@pytest.mark.parametrize("missing", ["_pool", "_network_backend"])
def test_poll_connections_falls_back_without_backend_hook(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    original = httpx.HTTPTransport.__init__

    def without_hook(self: httpx.HTTPTransport, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if not kwargs["limits"].max_keepalive_connections:
            return
        if missing == "_pool":
            self._pool.close()
            del self._pool
        else:
            del self._pool._network_backend

    monkeypatch.setattr(httpx.HTTPTransport, "__init__", without_hook)
    with keepalive_server() as (endpoint, state):
        with Machinera(api_key=CREDENTIAL, base_url=endpoint) as sdk:
            sdk.get_job("job-1")
            sdk.get_job("job-1")
            pool = sdk._poll_http._transport.current()._pool  # type: ignore[attr-defined]
            assert not isinstance(pool._network_backend, _CapturingBackend)
            assert pool._max_keepalive_connections == 0
        assert state["connections"] == 2


def test_failed_trace_does_not_interrupt_transport_cleanup() -> None:
    from machinera._io import Exchange

    def expired() -> None:
        raise machinera.DeadlineExceededError("expired")

    with httpx.Client() as http:
        exchange = Exchange(http, httpx.Request("GET", API), expired, lambda: None)
        exchange.trace("http11.receive_response_body.failed", {"exception": GeneratorExit()})
        with pytest.raises(machinera.DeadlineExceededError):
            exchange.trace("http11.receive_response_body.started", {})
