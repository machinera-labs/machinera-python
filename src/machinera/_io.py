from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from contextvars import ContextVar
from typing import Any, TypeVar

import httpcore
import httpx

from ._exceptions import DeadlineExceededError

# Bounds cancellation detection while a daemon is inside blocking user or network I/O.
_CANCEL_INTERVAL = 0.05


class Cancellation:
    """One-way event with a reentrant lock for Python main-thread signal handlers."""

    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.cancelled = False

    def set(self) -> None:
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()

    def check(self) -> None:
        if self.cancelled:
            raise KeyboardInterrupt

    def wait(self, delay: float) -> None:
        end = time.monotonic() + delay
        with self.condition:
            while not self.cancelled:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return
                # A signal can notify reentrantly just before wait releases the lock.
                self.condition.wait(min(remaining, _CANCEL_INTERVAL))
        self.check()


_T = TypeVar("_T")
_storage_exchange: ContextVar[bool] = ContextVar("machinera_storage_exchange", default=False)
_driver: ContextVar[Exchange | None] = ContextVar("machinera_exchange", default=None)


class _StorageLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _storage_exchange.get()


# HTTP request logs contain signed URLs; suppress only the current storage exchange.
logging.getLogger("httpx").addFilter(_StorageLogFilter())


class _CapturedStream(httpcore.NetworkStream):
    """Hand the socket to whichever exchange is reading or writing it.

    A reused keep-alive connection emits no connect event, so this is how a watchdog
    finds the socket of a pooled connection that stalls before response headers.
    """

    def __init__(self, stream: httpcore.NetworkStream) -> None:
        self.stream = stream

    def _capture(self) -> None:
        exchange = _driver.get()
        if exchange is not None:
            exchange.capture(self.stream)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._capture()
        return self.stream.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._capture()
        self.stream.write(buffer, timeout)

    def close(self) -> None:
        self.stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        return _CapturedStream(self.stream.start_tls(ssl_context, server_hostname, timeout))

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


class _CapturingBackend(httpcore.NetworkBackend):
    def __init__(self, backend: httpcore.NetworkBackend) -> None:
        self.backend = backend

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        return _CapturedStream(
            self.backend.connect_tcp(host, port, timeout, local_address, socket_options)
        )

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


def keepalive_transport(limits: httpx.Limits, fallback: httpx.Limits) -> httpx.HTTPTransport:
    """Build a capturing keep-alive transport, or a fallback-limits one if httpx changed.

    The backend hook uses private httpx and httpcore attributes; without them a reused
    connection could not be closed by a deadline, so keep-alive is disabled instead.
    """
    transport = httpx.HTTPTransport(limits=limits, trust_env=False)
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if not isinstance(pool, httpcore.ConnectionPool) or not isinstance(
        backend, httpcore.NetworkBackend
    ):
        return httpx.HTTPTransport(limits=fallback, trust_env=False)
    pool._network_backend = _CapturingBackend(backend)
    return transport


def run_bounded(
    action: Callable[[], _T],
    budget: float,
    expired: Callable[[], BaseException],
    cancel: Callable[[], None],
    finished: Callable[[], None],
    *,
    starting: Callable[[], None] = lambda: None,
    interrupted: Callable[[], None] = lambda: None,
) -> _T:
    interrupted()
    done = threading.Event()
    startup = threading.Lock()
    abandoned = False
    result: list[_T] = []
    errors: list[BaseException] = []

    def run() -> None:
        with startup:
            if abandoned:
                return
        try:
            result.append(action())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
            finished()

    stop = time.monotonic() + budget
    with startup:
        starting()
        try:
            threading.Thread(target=run, daemon=True).start()
        except BaseException:
            # A partially started thread must not use a released exchange registration.
            abandoned = True
            finished()
            raise
    try:
        while not done.is_set():
            interrupted()
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise expired()
            done.wait(min(remaining, _CANCEL_INTERVAL))
        interrupted()
        if errors:
            raise errors[0]
        if time.monotonic() >= stop:
            raise expired()
        return result[0]
    except BaseException:
        if not done.is_set():
            cancel()
        raise


def buffered(
    response: httpx.Response, content: bytearray, request: httpx.Request
) -> httpx.Response:
    result = httpx.Response(
        response.status_code,
        content=bytes(content),
        request=request,
        extensions=response.extensions,
    )
    # Preserve metadata after buffering to avoid decoding compressed content twice.
    result.headers = response.headers
    if response.encoding is not None:
        result.encoding = response.encoding
    return result


class Exchange:
    def __init__(
        self,
        client: httpx.Client,
        request: httpx.Request,
        check: Callable[[], None],
        finished: Callable[[], None],
        abort_body: Callable[[], None] | None = None,
        storage: bool = False,
        starting: Callable[[], None] = lambda: None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.request = request
        self.check = check
        self.finished = finished
        self.starting = starting
        self.abort_body = abort_body
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        # Custom stream.close() may block; it must not hold up recovery metadata.
        self.network_lock = threading.Lock()
        self.response: httpx.Response | None = None
        self.network: Any = None
        self.content = bytearray()
        self.request.extensions["trace"] = self.trace

    def trace(self, event: str, info: dict[str, Any]) -> None:
        with self.network_lock:
            if event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
                self.network = info.get("return_value")
            if event.startswith("http2.") or event.endswith("response_closed.started"):
                self.network = None
        if "response_closed" not in event and not event.endswith(".failed"):
            self._check()

    def capture(self, network: httpcore.NetworkStream) -> None:
        with self.network_lock:
            if self.cancelled.is_set():
                raise DeadlineExceededError("HTTP exchange cancelled")
            self.network = network

    def _check(self) -> None:
        if self.cancelled.is_set():
            raise DeadlineExceededError("HTTP exchange cancelled")
        self.check()

    def _send(self) -> httpx.Response:
        self._check()
        token = _storage_exchange.set(self.storage)
        _driver.set(self)
        try:
            response = self.client.send(
                self.request, auth=None, follow_redirects=False, stream=True
            )
        finally:
            _storage_exchange.reset(token)
        with self.lock:
            self.response = response
        try:
            for chunk in response.iter_bytes():
                with self.lock:
                    self.content.extend(chunk)
                self._check()
            self._check()
            return buffered(response, self.content, self.request)
        finally:
            # Clear ownership before the connection can return to the shared pool.
            with self.network_lock:
                self.network = None
            response.close()

    def _close(self) -> None:
        with self.network_lock:
            if self.network is not None:
                with suppress(Exception):
                    connection = self.network.get_extra_info("socket")
                    if isinstance(connection, socket.socket):
                        connection.shutdown(socket.SHUT_RDWR)
                    self.network.close()
                self.network = None
        with self.lock:
            response = self.response
        if response is not None:
            with suppress(Exception):
                response.close()

    def cancel(self) -> None:
        self.cancelled.set()
        if self.abort_body is not None:
            self.abort_body()
        threading.Thread(target=self._close, daemon=True).start()

    def run(
        self,
        budget: float,
        expired: Callable[[], BaseException],
        interrupted: Callable[[], None] = lambda: None,
    ) -> httpx.Response:
        return run_bounded(
            self._send,
            budget,
            expired,
            self.cancel,
            self.finished,
            starting=self.starting,
            interrupted=interrupted,
        )
