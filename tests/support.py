from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import io
import json
import ssl
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from machinera import APIError, AsyncMachinera, Limits, Machinera
from machinera._contract import ERROR_CODES

API = "https://api.machinera.com/v1"
MODEL = "transcribe-v1"
CREDENTIAL = "test-credential"
AUDIO = b"audio-bytes" * 15000
WALL = 1_700_000_000
SIGNED = "https://storage.example/object?signature=private-signature"
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    monotonic = __call__

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


def sync_upload(sdk: Any, **kwargs: Any) -> Any:
    return sdk.transcribe_file(
        io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav", **kwargs
    )


def refused(code: str, retryable: bool = True) -> httpx.Response:
    return httpx.Response(
        503, json={"error": {"code": code, "retryable": retryable}}, headers={"x-request-id": "r-1"}
    )


def failed_job(code: str = "job_shed", retryable: bool = True) -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": "job-1", "status": "error", "error": {"code": code, "retryable": retryable}},
    )


def submit(sdk: Any, source: str, key: str | None) -> Any:
    if source == "url":
        return sdk.transcribe_url("https://audio.example/a", model=MODEL, idempotency_key=key)
    return sdk.transcribe_file(
        io.BytesIO(b"audio"), model=MODEL, content_type="audio/wav", idempotency_key=key
    )


def refused_sync(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/v1/audio/transcriptions":
        return httpx.Response(
            503, json={"error": {"code": "inline_admission_refused", "retryable": True}}
        )
    return None


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


def staged_unavailable(limits: dict[str, Any] | None) -> httpx.Response:
    error: dict[str, Any] = {"code": "staged_uploads_unavailable", "retryable": False}
    if limits is not None:
        error["limits"] = limits
    return httpx.Response(503, json={"error": error})


class InterruptedFile(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise KeyboardInterrupt


def async_client(handler: Handler, clock: Clock | None = None, **kwargs: Any) -> AsyncMachinera:
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


@contextmanager
def keepalive_server(
    stalled_get: int = 0, before_headers: bool = False, tls: ssl.SSLContext | None = None
) -> Iterator[tuple[str, dict[str, Any]]]:
    lock = threading.Lock()
    stop = threading.Event()
    state: dict[str, Any] = {"connections": 0, "gets": 0, "disconnected": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            with lock:
                state["connections"] += 1

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.close_connection = True
            self.connection.settimeout(3)
            if self.connection.recv(1) == b"":
                state["disconnected"].set()

        def do_GET(self) -> None:
            with lock:
                state["gets"] += 1
                count = state["gets"]
            if count == stalled_get and before_headers:
                self.close_connection = True
                self.connection.settimeout(3)
                try:
                    closed = self.connection.recv(1) == b""
                except (ssl.SSLEOFError, ConnectionResetError):
                    closed = True
                if closed:
                    state["disconnected"].set()
                return
            if count == stalled_get:
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    while not stop.wait(0.02):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                except OSError:
                    state["disconnected"].set()
                self.close_connection = True
                return
            status = "completed" if self.path.endswith("job-done") else "processing"
            job = self.path.rsplit("/", 1)[-1]
            snapshot: dict[str, Any] = {"id": job, "status": status}
            if status == "completed":
                snapshot["result"] = result()
            body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        scheme = "http" if tls is None else "https"
        yield f"{scheme}://127.0.0.1:{server.server_port}/v1", state
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def accepted(job: str = "job-1") -> httpx.Response:
    return queued(202, job=job)


def queued(status: int = 200, *, job: str = "job-1") -> httpx.Response:
    return httpx.Response(status, json={"id": job, "status": "queued"})


def unavailable() -> httpx.Response:
    return httpx.Response(503, json={"error": {"code": "service_unavailable"}})


def error_code(status: int, retryable: bool) -> str:
    return next(
        code
        for code, entry in ERROR_CODES.items()
        if (entry.status, entry.retryable) == (status, retryable)
    )


def job_api(
    submit: Callable[[httpx.Request], httpx.Response] = lambda _: accepted(),
    poll: Callable[[httpx.Request], httpx.Response] = lambda _: completed(),
    sync: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions" and sync is not None:
            return sync(request)
        return (submit if request.method == "POST" else poll)(request)

    return handler


def recorder(
    handler: Callable[[httpx.Request], httpx.Response], requests: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return recorded


class ProbeFile(io.BytesIO):
    def __init__(
        self,
        data: bytes,
        *,
        max_read: int = 65536,
        on_read: Callable[[], None] | None = None,
        seekable: bool = True,
    ) -> None:
        super().__init__(data)
        self.max_read = max_read
        self.on_read = on_read
        self.can_seek = seekable
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        assert 0 < size <= self.max_read
        self.reads.append(size)
        if self.on_read is not None:
            self.on_read()
        return super().read(size)

    def seekable(self) -> bool:
        return self.can_seek


@contextmanager
def held_slot(sdk: Any) -> Iterator[None]:
    entered, release = threading.Event(), threading.Event()

    def block() -> None:
        entered.set()
        assert release.wait(5)
        raise ValueError("input released")

    source = ProbeFile(b"audio", on_read=block)
    with ThreadPoolExecutor(1) as pool:
        if isinstance(sdk, Blocking):
            running = sdk.loop.create_task(
                sdk.sdk.transcribe_file(source, model=MODEL, content_type="audio/wav")
            )
            ready = sdk.run(asyncio.to_thread(entered.wait, 2))
        else:
            running = pool.submit(
                sdk.transcribe_file, source, model=MODEL, content_type="audio/wav"
            )
            ready = entered.wait(2)
        try:
            assert ready
            yield
        finally:
            release.set()
            with suppress(ValueError, APIError):
                sdk.run(running) if isinstance(sdk, Blocking) else running.result(timeout=2)


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


class BlockedInputAccess:
    def __init__(self, blocked_phase: str) -> None:
        probe = self
        probe.entered = threading.Event()
        probe.release_read = threading.Event()
        probe.exchange_finished = threading.Event()
        probe.connection_closed = threading.Event()
        probe.client_closed = threading.Event()
        probe.file_operations: list[str] = []
        probe.requests: list[httpx.Request] = []
        probe.expected_reads = {"hash": 1, "restore": 2, "upload": 3}[blocked_phase]
        probe.entered_at: list[float] = []

        def block() -> None:
            probe.entered_at.append(time.monotonic())
            probe.entered.set()
            assert probe.release_read.wait(3)

        class SlowUpload(io.BytesIO):
            reads = 0
            seeks = 0

            def seek(self, offset: int, whence: int = 0) -> int:
                self.seeks += 1
                probe.file_operations.append("seek")
                if blocked_phase == "restore" and self.seeks == 3:
                    block()
                return super().seek(offset, whence)

            def read(self, size: int = -1) -> bytes:
                self.reads += 1
                probe.file_operations.append("read")
                if blocked_phase != "restore" and self.reads == probe.expected_reads:
                    block()
                return super().read(size)

        class Connection:
            def get_extra_info(self, name: str) -> None:
                return None

            def close(self) -> None:
                probe.connection_closed.set()

        class ReadingTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                probe.requests.append(request)
                request.extensions["trace"](
                    "connection.connect_tcp.complete", {"return_value": Connection()}
                )
                try:
                    request.read()
                    return httpx.Response(200, json={"text": ""})
                finally:
                    probe.exchange_finished.set()

            def close(self) -> None:
                probe.client_closed.set()

        probe.source = SlowUpload(b"audio")
        probe.transport = ReadingTransport()


class ReplayService(Service):
    def __init__(self, refresh_path: str) -> None:
        super().__init__()
        self.refresh_path = refresh_path
        self.accepted: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = super().__call__(request)
        if request.url.path.endswith("uploads") and self.accepted is not None:
            if self.refresh_path == "refresh" and len(self.initializations) == 2:
                return httpx.Response(200, json=grant(self.descriptor, expires_at=WALL - 1))
            bound = grant(self.descriptor, state="bound", job_id="job-1")
            for field in ("put_url", "required_headers", "method"):
                bound.pop(field)
            return httpx.Response(200, json=bound)
        if request.url.path.endswith("jobs"):
            assert request.headers["idempotency-key"] == "saved-key"
            submitted = self.submissions[-1]
            if self.accepted is None:
                self.accepted = submitted
            elif submitted != self.accepted:
                return httpx.Response(
                    422,
                    json={"error": {"code": "idempotency_payload_mismatch", "retryable": False}},
                )
        return response


@contextmanager
def mounted_storage_client(
    service: Service, mount: str, status: int, asynchronous: bool
) -> Iterator[Any]:
    routed: list[httpx.Request] = []
    request_hooks: list[httpx.Request] = []
    response_hooks: list[httpx.Response] = []

    def default_transport(request: httpx.Request) -> httpx.Response:
        assert request.method != "PUT", "Storage bypassed the caller's mount"
        return service(request)

    def mounted_transport(request: httpx.Request) -> httpx.Response:
        routed.append(request)
        response = service(request)
        if request.method == "PUT":
            return httpx.Response(status, headers={"Location": SIGNED + "redirect"})
        return response

    async def async_request(request: httpx.Request) -> None:
        request_hooks.append(request)

    async def async_response(response: httpx.Response) -> None:
        response_hooks.append(response)

    on_request = async_request if asynchronous else request_hooks.append
    on_response = async_response if asynchronous else response_hooks.append
    http_cls = httpx.AsyncClient if asynchronous else httpx.Client
    http = http_cls(
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
        event_hooks={"request": [on_request], "response": [on_response]},
        follow_redirects=True,
        trust_env=False,
    )

    with open_client(
        asynchronous,
        api_key=CREDENTIAL,
        http_client=http,
        limits=Limits(1, 2),
        wall_clock=lambda: WALL,
    ) as sdk:
        try:
            yield sdk, http, routed, request_hooks, response_hooks
        finally:
            sdk.run(http.aclose()) if asynchronous else http.close()
