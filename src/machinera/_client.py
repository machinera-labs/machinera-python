from __future__ import annotations

import random
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import FrameType
from typing import Any, BinaryIO, Literal, cast

import httpx

from ._core import (
    _T,
    EXCHANGE_POOL,
    POLL_POOL,
    CloseFile,
    Core,
    Flow,
    OpenFile,
    Prepare,
    Sleep,
    _Call,
    _sanitized,
)
from ._exceptions import (
    DeadlineExceededError,
)
from ._files import FileContent, FileInput, open_file
from ._io import Cancellation, Exchange, keepalive_transport, run_bounded
from ._multipart import Multipart, UploadBody
from ._pool import PoolKey, SharedTransport
from ._types import (
    UNSET,
    JobSnapshot,
    Limits,
    ResponseFormat,
    RetryPolicy,
    Timeout,
    TranscriptionResult,
    Unset,
)


class _Lifecycle:
    def __init__(self, max_concurrency: int | None) -> None:
        self.condition = threading.Condition()
        self.cancellation = Cancellation()
        self.active = 0
        self.exchanges = 0
        self.closed = False
        self.files: set[int] = set()
        self.semaphore = threading.BoundedSemaphore(max_concurrency) if max_concurrency else None


class Machinera(Core):
    """Blocking, thread-safe Machinera API client."""

    _http: httpx.Client
    _poll_http: httpx.Client
    _transport_type = httpx.BaseTransport
    _client_type = httpx.Client
    _lifecycle: _Lifecycle
    _sleeper: Callable[[float], None]
    _restore_interrupt: Callable[[], None] | None

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int | Unset = UNSET,
        default_headers: Mapping[str, str] | None = None,
        timeout: Timeout = UNSET,
        retry_policy: RetryPolicy | None = None,
        limits: Limits | None = None,
        max_concurrency: int | None = None,
        cancel_on_interrupt: bool = False,
        transport: Literal["auto", "job"] | httpx.BaseTransport = "auto",
        sync_replay: Literal["never", "always"] = "never",
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if cancel_on_interrupt and threading.current_thread() is not threading.main_thread():
            raise ValueError("cancel_on_interrupt requires construction on the main thread")
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            default_headers=default_headers,
            timeout=timeout,
            retry_policy=retry_policy,
            limits=limits,
            max_concurrency=max_concurrency,
            transport=transport,
            sync_replay=sync_replay,
            http_client=http_client,
            clock=clock,
            wall_clock=wall_clock,
            random_source=random_source,
        )
        object.__setattr__(self, "_sleeper", sleeper)
        object.__setattr__(self, "_lifecycle", _Lifecycle(max_concurrency))
        object.__setattr__(self, "_restore_interrupt", None)
        if cancel_on_interrupt:
            previous = signal.getsignal(signal.SIGINT)

            def interrupt(signum: int, frame: FrameType | None) -> None:
                self.cancel()
                if callable(previous):
                    previous(signum, frame)
                elif previous == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    signal.raise_signal(signum)

            def restore() -> None:
                if signal.getsignal(signal.SIGINT) is interrupt:
                    signal.signal(signal.SIGINT, previous)

            signal.signal(signal.SIGINT, interrupt)
            object.__setattr__(self, "_restore_interrupt", restore)

    def _shared_transport(self, role: str) -> SharedTransport:
        if role == "poll":
            return SharedTransport(
                PoolKey.of("poll", POLL_POOL), lambda: keepalive_transport(POLL_POOL, EXCHANGE_POOL)
            )
        return SharedTransport(
            PoolKey.of("exchange", EXCHANGE_POOL),
            lambda: httpx.HTTPTransport(limits=EXCHANGE_POOL, trust_env=False),
        )

    def __enter__(self) -> Machinera:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def cancel(self) -> None:
        """Interrupt active and future operations locally; never cancel server jobs."""
        self._lifecycle.cancellation.set()

    def _sleep(self, delay: float) -> None:
        cancellation = self._lifecycle.cancellation
        if self._sleeper is time.sleep:
            cancellation.wait(delay)
        else:
            run_bounded(
                lambda: self._sleeper(delay),
                float("inf"),
                lambda: DeadlineExceededError("Sleep deadline exceeded"),
                lambda: None,
                self._exchange_finished,
                starting=self._exchange_starting,
                interrupted=cancellation.check,
            )

    def close(self) -> None:
        """Wait for active calls, then close the HTTP clients the SDK created."""
        if (
            self._restore_interrupt is not None
            and threading.current_thread() is not threading.main_thread()
        ):
            raise ValueError("cancel_on_interrupt requires close() on the main thread")
        life = self._lifecycle
        try:
            with life.condition:
                life.closed = True
                while life.active:
                    life.condition.wait()
                if self._owns_http and not life.exchanges:
                    self._close_http()
        finally:
            if self._restore_interrupt is not None:
                self._restore_interrupt()
                object.__setattr__(self, "_restore_interrupt", None)

    def _exchange_starting(self) -> None:
        with self._lifecycle.condition:
            self._lifecycle.exchanges += 1

    def _exchange_finished(self) -> None:
        life = self._lifecycle
        with life.condition:
            life.exchanges -= 1
            close = life.closed and not life.active and not life.exchanges and self._owns_http
        if close:
            self._close_http()

    def _close_http(self) -> None:
        self._http.close()
        self._poll_http.close()

    @contextmanager
    def _operation(
        self,
        timeout: Timeout,
        deadline: float | None,
        key: str | None = None,
        *,
        phase: str = "prepare",
        **context: Any,
    ) -> Iterator[_Call]:
        call = self._new_call(timeout, deadline, key, phase=phase, **context)
        life = self._lifecycle
        call.interrupted = life.cancellation.check
        with life.condition:
            if life.closed and not life.cancellation.cancelled:
                raise self._closed_error(call)
            life.active += 1
        acquired = False
        failure: BaseException | None = None
        try:
            call.interrupted()
            if life.semaphore is not None:
                call.phase = "concurrency_wait" if phase == "prepare" else phase
                while not life.semaphore.acquire(blocking=False):
                    self._sleep(min(0.05, call.remaining()))
                acquired = True
            call.remaining()
            call.phase = phase
            yield call
        except BaseException as error:
            failure = self._classify(
                KeyboardInterrupt() if life.cancellation.cancelled else error,
                call,
                key,
                blocking=True,
            )
            if failure is None:
                raise
        finally:
            if acquired and life.semaphore is not None:
                life.semaphore.release()
            with life.condition:
                life.active -= 1
                life.condition.notify_all()
        if failure is not None:
            self._attach(failure, call)
            raise failure from None

    @contextmanager
    def _file(self, file: FileContent, call: _Call) -> Iterator[BinaryIO]:
        source, owned = open_file(file)
        life = self._lifecycle
        with life.condition:
            if id(source) in life.files:
                raise ValueError("A file handle cannot be shared by simultaneous calls")
            life.files.add(id(source))
        released = threading.Event()
        call.file_released = released

        def release() -> None:
            if call.read_idle is not None:
                call.read_idle.wait()
            try:
                if owned:
                    source.close()
            finally:
                with life.condition:
                    life.files.remove(id(source))
                released.set()

        try:
            yield source
        finally:
            if call.read_idle is not None and not call.read_idle.is_set():
                threading.Thread(target=release, daemon=True).start()
            else:
                release()

    @_sanitized
    def transcribe_file(
        self,
        file: FileInput,
        *,
        model: str,
        filename: str | None = None,
        content_type: str | None = None,
        language: str | None = None,
        response_format: ResponseFormat = "json",
        timeout: Timeout = UNSET,
        deadline: float | None = None,
        idempotency_key: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a path, bytes, binary handle, or file tuple from its current offset."""
        with self._operation(timeout, deadline, idempotency_key) as call:
            return self._run(
                self._transcribe_file(
                    call,
                    file,
                    model,
                    filename,
                    content_type,
                    language,
                    response_format,
                    idempotency_key is not None,
                )
            )

    @_sanitized
    def transcribe_url(
        self,
        url: str,
        *,
        model: str,
        language: str | None = None,
        response_format: ResponseFormat = "json",
        timeout: Timeout = UNSET,
        deadline: float | None = None,
        idempotency_key: str | None = None,
    ) -> TranscriptionResult:
        """Submit an audio URL as a durable job and wait for its result."""
        with self._operation(timeout, deadline, idempotency_key) as call:
            return self._run(self._transcribe_url(call, url, model, language, response_format))

    @_sanitized
    def get_job(self, job_id: str, *, timeout: Timeout = UNSET) -> JobSnapshot:
        """Read one job snapshot, retrying only eligible transient read failures."""
        with self._operation(timeout, None, caller_key=None, job_id=self._job_id(job_id)) as call:
            data, _ = self._run(self._read_job(call))
            return data

    @_sanitized
    def resume(
        self,
        job_id: str | None = None,
        *,
        file: FileInput | None = None,
        operation_key: str | None = None,
        upload_id: str | None = None,
        model: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        language: str | None = None,
        response_format: ResponseFormat | None = None,
        timeout: Timeout = UNSET,
        deadline: float | None = None,
    ) -> TranscriptionResult:
        """Poll a known job or replay a staged operation with identical input and options."""
        selected, context = self._resume_plan(
            job_id, file, operation_key, upload_id, model, language, response_format
        )
        with self._operation(
            timeout, deadline, operation_key, caller_key=True if job_id is None else None, **context
        ) as call:
            return self._run(
                self._resume(call, file, model, filename, content_type, language, selected)
            )

    def _run(self, flow: Flow[_T]) -> _T:
        files: dict[int, Any] = {}
        value: Any = None
        error: BaseException | None = None
        while True:
            try:
                effect = flow.throw(error) if error is not None else flow.send(value)
            except StopIteration as done:
                return cast(_T, done.value)
            value, error = None, None
            try:
                if not isinstance(effect, CloseFile):
                    self._lifecycle.cancellation.check()
                if isinstance(effect, Sleep):
                    self._sleep(effect.delay)
                elif isinstance(effect, OpenFile):
                    scope = self._file(effect.content, effect.call)
                    value = scope.__enter__()
                    files[id(value)] = scope
                elif isinstance(effect, CloseFile):
                    files.pop(id(effect.source)).__exit__(None, None, None)
                elif isinstance(effect, Prepare):
                    budget = effect.call.remaining()
                    run_bounded(
                        effect.body.prepare,
                        budget,
                        lambda: DeadlineExceededError(
                            "Operation deadline exceeded during preparation"
                        ),
                        effect.body.abort,
                        self._exchange_finished,
                        starting=self._exchange_starting,
                        interrupted=self._lifecycle.cancellation.check,
                    )
                else:
                    exchange = Exchange(
                        self._poll_http if effect.request.method == "GET" else self._http,
                        effect.request,
                        effect.check,
                        self._exchange_finished,
                        effect.body.abort
                        if isinstance(effect.body, (Multipart, UploadBody))
                        else None,
                        storage=effect.storage,
                        starting=self._exchange_starting,
                    )
                    try:
                        value = exchange.run(
                            effect.budget, effect.expired, self._lifecycle.cancellation.check
                        )
                    finally:
                        with exchange.lock:
                            effect.response = exchange.response
                            effect.content = bytearray(exchange.content)
            except BaseException as caught:
                error = caught
