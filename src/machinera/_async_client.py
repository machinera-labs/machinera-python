from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from functools import partial, wraps
from typing import Any, BinaryIO, Literal, cast

import httpx

from ._core import (
    _P,
    _T,
    CloseFile,
    Core,
    Flow,
    OpenFile,
    Prepare,
    Send,
    Sleep,
    _Call,
    local_failure,
)
from ._exceptions import (
    APIConnectionError,
    APIError,
    DeadlineExceededError,
    TranscriptionInterrupted,
)
from ._files import FileContent, FileInput, open_file
from ._io import _storage_exchange, buffered
from ._multipart import Multipart, UploadBody
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


def _sanitized(function: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
    @wraps(function)
    async def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        failure: APIError | ValueError | TypeError
        try:
            return await function(*args, **kwargs)
        except (APIError, ValueError, TypeError) as error:
            failure = error
        failure.__context__ = None
        failure.__cause__ = None
        raise failure from None

    return invoke


class _Interrupt(Exception):
    """Carry keyboard interruption through child tasks without stopping the event loop."""


async def _thread(action: Callable[[], _T], abort: Callable[[], None] = lambda: None) -> _T:
    def run() -> _T:
        try:
            return action()
        except KeyboardInterrupt:
            raise _Interrupt from None

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        abort()
        # A running file read cannot be cancelled; retain ownership until it finishes.
        while not task.done():
            with suppress(BaseException):
                await asyncio.shield(task)
        with suppress(BaseException):
            task.result()
        raise


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body: Multipart | UploadBody) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = iter(self.body)
        while (chunk := await _thread(lambda: next(chunks, None), self.body.abort)) is not None:
            yield chunk


class _Lifecycle:
    def __init__(self, max_concurrency: int | None) -> None:
        self.semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self.active = 0
        self.closed = False
        self.idle = asyncio.Event()
        self.idle.set()
        self.close_lock = asyncio.Lock()
        self.files: set[int] = set()


class AsyncMachinera(Core):
    """Native asyncio client, shareable across tasks on one event loop."""

    _http: httpx.AsyncClient
    _poll_http: httpx.AsyncClient
    _transport_type = httpx.AsyncBaseTransport
    _client_type = httpx.AsyncClient
    _lifecycle: _Lifecycle
    _sleeper: Callable[[float], Awaitable[None]]

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
        transport: Literal["auto", "job"] | httpx.AsyncBaseTransport = "auto",
        sync_replay: Literal["never", "always"] = "never",
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
    ) -> None:
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

    async def __aenter__(self) -> AsyncMachinera:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Wait for active calls, then close the HTTP clients the SDK created."""
        life = self._lifecycle
        life.closed = True
        await life.idle.wait()
        async with life.close_lock:
            if self._owns_http:
                await self._http.aclose()
                await self._poll_http.aclose()

    @asynccontextmanager
    async def _operation(
        self,
        timeout: Timeout,
        deadline: float | None,
        key: str | None = None,
        *,
        phase: str = "prepare",
        upload_id: str | None = None,
        job_id: str | None = None,
    ) -> AsyncIterator[_Call]:
        call = self._new_call(
            timeout, deadline, key, phase=phase, upload_id=upload_id, job_id=job_id
        )
        life = self._lifecycle
        if life.closed:
            error = APIConnectionError("Client is closed")
            error._local = True
            self._attach(error, call)
            raise error
        life.active += 1
        life.idle.clear()
        acquired = False
        failure: BaseException | None = None
        try:
            if life.semaphore is not None:
                call.phase = "concurrency_wait" if phase == "prepare" else phase
                budget = call.remaining()
                try:
                    await asyncio.wait_for(life.semaphore.acquire(), budget)
                except asyncio.TimeoutError:
                    raise DeadlineExceededError("Operation deadline exceeded") from None
                acquired = True
            call.remaining()
            call.phase = phase
            yield call
        except (APIError, asyncio.CancelledError) as error:
            failure = error
        except (ValueError, TypeError) as error:
            if call.phase not in ("upload_init", "upload_put", "submit", "poll"):
                raise
            failure = error
        except (KeyboardInterrupt, _Interrupt):
            failure = TranscriptionInterrupted(
                "Transcription interrupted; use recovery context",
                ambiguous=key is None and call.phase == "sync_submit",
            )
        except (OSError, httpx.HTTPError) as local:
            failure = local_failure(local)
        finally:
            if acquired and life.semaphore is not None:
                life.semaphore.release()
            life.active -= 1
            if not life.active:
                life.idle.set()
        if failure is not None:
            self._attach(failure, call)
            raise failure from None

    @_sanitized
    async def transcribe_file(
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
        async with self._operation(timeout, deadline, idempotency_key) as call:
            return await self._run(
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
    async def transcribe_url(
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
        async with self._operation(timeout, deadline, idempotency_key) as call:
            return await self._run(
                self._transcribe_url(call, url, model, language, response_format)
            )

    @_sanitized
    async def get_job(self, job_id: str, *, timeout: Timeout = UNSET) -> JobSnapshot:
        """Read one job snapshot, retrying only eligible transient read failures."""
        async with self._operation(timeout, None) as call:
            call.job_id = self._job_id(job_id)
            data, _ = await self._run(self._read_job(call))
            return data

    @_sanitized
    async def resume(
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
        async with self._operation(timeout, deadline, operation_key, **context) as call:
            return await self._run(
                self._resume(call, file, model, filename, content_type, language, selected)
            )

    async def _send(self, effect: Send) -> httpx.Response:
        if isinstance(effect.body, (Multipart, UploadBody)):
            effect.request.stream = _Stream(effect.body)
        effect.check()
        token = _storage_exchange.set(effect.storage)
        try:
            http = self._poll_http if effect.request.method == "GET" else self._http
            response = await http.send(
                effect.request, auth=None, follow_redirects=False, stream=True
            )
            effect.response = response
            try:
                async for chunk in response.aiter_bytes():
                    effect.content.extend(chunk)
                    effect.check()
                effect.check()
                return buffered(response, effect.content, effect.request)
            finally:
                await response.aclose()
        except KeyboardInterrupt:
            raise _Interrupt from None
        finally:
            _storage_exchange.reset(token)

    async def _open_file(self, content: FileContent) -> tuple[BinaryIO, bool]:
        opened: list[tuple[BinaryIO, bool]] = []

        def acquire() -> tuple[BinaryIO, bool]:
            source = open_file(content)
            opened.append(source)
            return source

        try:
            return await _thread(acquire)
        except BaseException:
            if opened and opened[0][1]:
                await _thread(opened[0][0].close)
            raise

    async def _run(self, flow: Flow[_T]) -> _T:
        files: dict[int, tuple[bool, int | None]] = {}
        value: Any = None
        error: BaseException | None = None
        while True:
            try:
                effect = flow.throw(error) if error is not None else flow.send(value)
            except StopIteration as done:
                return cast(_T, done.value)
            value, error = None, None
            try:
                if isinstance(effect, Sleep):
                    await self._sleeper(effect.delay)
                elif isinstance(effect, OpenFile):
                    budget = effect.call.remaining()
                    try:
                        source, owned = await asyncio.wait_for(
                            self._open_file(effect.content), budget
                        )
                    except asyncio.TimeoutError:
                        raise DeadlineExceededError(
                            "Operation deadline exceeded opening file"
                        ) from None
                    if id(source) in self._lifecycle.files:
                        raise ValueError("A file handle cannot be shared by simultaneous calls")
                    self._lifecycle.files.add(id(source))
                    files[id(source)] = (owned, None)
                    value = source
                elif isinstance(effect, CloseFile):
                    source = effect.source
                    owned, offset = files.pop(id(source))
                    try:
                        if owned:
                            await _thread(source.close)
                        elif offset is not None:
                            await _thread(partial(source.seek, offset))
                    finally:
                        self._lifecycle.files.remove(id(source))
                elif isinstance(effect, Prepare):
                    body = effect.body
                    budget = effect.call.remaining()
                    try:
                        await asyncio.wait_for(_thread(body.prepare, body.abort), budget)
                    except asyncio.TimeoutError:
                        raise DeadlineExceededError(
                            "Operation deadline exceeded during preparation"
                        ) from None
                    finally:
                        owned, _ = files[id(body.source)]
                        files[id(body.source)] = (owned, body.offset)
                else:
                    try:
                        value = await asyncio.wait_for(self._send(effect), effect.budget)
                    except asyncio.TimeoutError:
                        raise effect.expired() from None
            except BaseException as caught:
                error = caught
