"""Process-wide connection pools shared by the HTTP clients the SDK creates.

Clients with the same connection settings share one transport, so building a client
per call costs no TLS context and no new connection. Lifetime rules:

- Blocking transports live for the process and are closed at interpreter exit.
- Asyncio transports belong to one running event loop, are never shared across loops,
  and are closed by the last client on that loop to close.
- A forked child never reuses its parent's transports or sockets.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
import weakref
from collections.abc import Callable, Hashable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class PoolKey:
    """Everything that affects a connection; base URLs and timeouts stay per request."""

    role: str
    limits: tuple[int | None, int | None, float | None]

    @classmethod
    def of(cls, role: str, limits: httpx.Limits) -> PoolKey:
        return cls(
            role,
            (limits.max_connections, limits.max_keepalive_connections, limits.keepalive_expiry),
        )


@dataclass
class _AsyncEntry:
    transport: httpx.AsyncBaseTransport
    owners: set[int] = field(default_factory=set)


_lock = threading.Lock()
_pid = os.getpid()
_sync: dict[Hashable, httpx.BaseTransport] = {}
_async: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Hashable, _AsyncEntry]] = (
    weakref.WeakKeyDictionary()
)


def _forget_parent() -> None:
    # The parent's sockets are inherited, not owned: drop them without closing, so the
    # child neither sends on nor shuts down a connection the parent still uses.
    global _lock, _pid, _async
    _lock = threading.Lock()
    _pid = os.getpid()
    _sync.clear()
    _async = weakref.WeakKeyDictionary()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_parent)


def _check_pid() -> None:
    if os.getpid() != _pid:
        _forget_parent()


def sync_transport(key: Hashable, factory: Callable[[], httpx.BaseTransport]) -> Any:
    _check_pid()
    with _lock:
        transport = _sync.get(key)
        if transport is None:
            transport = _sync[key] = factory()
        return transport


@atexit.register
def close_all() -> None:
    """Close every blocking transport; asyncio transports close with their clients."""
    _check_pid()
    with _lock:
        transports = list(_sync.values())
        _sync.clear()
    for transport in transports:
        with suppress(Exception):
            transport.close()


class SharedTransport(httpx.BaseTransport):
    """One client's handle on a shared blocking transport; closing it closes nothing."""

    def __init__(self, key: Hashable, factory: Callable[[], httpx.BaseTransport]) -> None:
        self.key = key
        self.factory = factory

    def current(self) -> Any:
        return sync_transport(self.key, self.factory)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response: httpx.Response = self.current().handle_request(request)
        return response

    def close(self) -> None:
        pass


class SharedAsyncTransport(httpx.AsyncBaseTransport):
    """One client's handle on its running loop's shared transport, reference counted."""

    def __init__(self, key: Hashable, factory: Callable[[], httpx.AsyncBaseTransport]) -> None:
        self.key = key
        self.factory = factory
        self.loops: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()

    def current(self) -> Any:
        loop = asyncio.get_running_loop()
        _check_pid()
        with _lock:
            entries = _async.setdefault(loop, {})
            entry = entries.get(self.key)
            if entry is None:
                entry = entries[self.key] = _AsyncEntry(self.factory())
            entry.owners.add(id(self))
            self.loops.add(loop)
            return entry.transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response: httpx.Response = await self.current().handle_async_request(request)
        return response

    async def aclose(self) -> None:
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        closing: list[httpx.AsyncBaseTransport] = []
        _check_pid()
        with _lock:
            for loop in list(self.loops):
                entries = _async.get(loop, {})
                entry = entries.get(self.key)
                if entry is None:
                    continue
                entry.owners.discard(id(self))
                if not entry.owners:
                    del entries[self.key]
                    # Another loop's transport cannot be awaited here; it is dropped.
                    if loop is running:
                        closing.append(entry.transport)
            self.loops.clear()
        for transport in closing:
            await transport.aclose()
