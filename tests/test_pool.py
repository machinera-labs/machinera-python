from __future__ import annotations

import asyncio
import os
import warnings
from typing import Any

import httpx
import pytest
from support import CREDENTIAL, keepalive_server, open_client

from machinera import AsyncMachinera, Machinera, _pool


def test_clients_share_one_connection_and_survive_each_other() -> None:
    with keepalive_server() as (endpoint, state):
        first = Machinera(api_key=CREDENTIAL, base_url=endpoint)
        second = Machinera(api_key=CREDENTIAL, base_url=endpoint)
        first.get_job("job-1")
        second.get_job("job-1")
        assert state["connections"] == 1
        first.close()
        assert second.get_job("job-1").status == "processing"
        second.close()
        with Machinera(api_key=CREDENTIAL, base_url=endpoint) as third:
            assert third.get_job("job-1").status == "processing"
        assert state["connections"] == 1
        assert first._poll_http.is_closed and second._poll_http.is_closed


def test_async_clients_on_one_loop_share_until_the_last_closes() -> None:
    async def scenario(endpoint: str, state: dict[str, Any]) -> None:
        first = AsyncMachinera(api_key=CREDENTIAL, base_url=endpoint)
        second = AsyncMachinera(api_key=CREDENTIAL, base_url=endpoint)
        await first.get_job("job-1")
        await second.get_job("job-1")
        assert state["connections"] == 1
        await first.aclose()
        assert (await second.get_job("job-1")).status == "processing"
        assert state["connections"] == 1
        await second.aclose()
        assert not _pool._async.get(asyncio.get_running_loop())
        async with AsyncMachinera(api_key=CREDENTIAL, base_url=endpoint) as third:
            assert (await third.get_job("job-1")).status == "processing"
        assert state["connections"] == 2

    with keepalive_server() as (endpoint, state):
        asyncio.run(scenario(endpoint, state))


async def current_transport(sdk: AsyncMachinera) -> Any:
    return sdk._poll_http._transport.current()  # type: ignore[attr-defined]


def test_async_pools_are_never_shared_across_loops() -> None:
    with keepalive_server() as (endpoint, state):
        with open_client(True, api_key=CREDENTIAL, base_url=endpoint) as one:
            with open_client(True, api_key=CREDENTIAL, base_url=endpoint) as two:
                one.get_job("job-1")
                two.get_job("job-1")
                assert one.loop is not two.loop
                assert state["connections"] == 2
                assert one.run(current_transport(one.sdk)) is not two.run(
                    current_transport(two.sdk)
                )


def test_different_settings_get_different_pools() -> None:
    poll = _pool.PoolKey.of("poll", httpx.Limits(max_keepalive_connections=20))
    other = _pool.PoolKey.of("poll", httpx.Limits(max_keepalive_connections=5))
    made: list[httpx.BaseTransport] = []

    def factory() -> httpx.BaseTransport:
        made.append(httpx.HTTPTransport(trust_env=False))
        return made[-1]

    assert _pool.sync_transport(poll, factory) is _pool.sync_transport(poll, factory)
    assert _pool.sync_transport(other, factory) is not _pool.sync_transport(poll, factory)
    assert len(made) == 2
    with Machinera(api_key=CREDENTIAL) as sdk:
        assert sdk._http._transport.current() is not sdk._poll_http._transport.current()  # type: ignore[attr-defined]


def test_injected_clients_are_never_shared() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as http:
        with Machinera(api_key=CREDENTIAL, http_client=http) as sdk:
            assert sdk._http is http and sdk._poll_http is http
    assert not _pool._sync


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX-only")
def test_forked_child_opens_its_own_connections() -> None:
    with keepalive_server() as (endpoint, state):
        sdk = Machinera(api_key=CREDENTIAL, base_url=endpoint)
        sdk.get_job("job-1")
        parent_transport = sdk._poll_http._transport.current()  # type: ignore[attr-defined]
        assert state["connections"] == 1
        with warnings.catch_warnings():
            # Forking while the test server thread runs is safe for this child.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            code = 1
            try:
                fresh = not _pool._sync
                ok = sdk.get_job("job-1").status == "processing"
                child_transport = sdk._poll_http._transport.current()  # type: ignore[attr-defined]
                code = 0 if fresh and ok and child_transport is not parent_transport else 1
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert state["connections"] == 2
        assert sdk.get_job("job-1").status == "processing"
        assert state["connections"] == 2
        sdk.close()


def test_pid_change_without_fork_hook_drops_inherited_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Machinera(api_key=CREDENTIAL) as sdk:
        inherited = sdk._poll_http._transport.current()  # type: ignore[attr-defined]
        monkeypatch.setattr(_pool, "_pid", -1)
        assert sdk._poll_http._transport.current() is not inherited  # type: ignore[attr-defined]
        assert _pool._pid == os.getpid()


@pytest.mark.parametrize("cls", [Machinera, AsyncMachinera])
def test_construction_creates_no_transport_or_connections(
    cls: type[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a transport was created during construction")

    for name in ("HTTPTransport", "AsyncHTTPTransport"):
        monkeypatch.setattr(getattr(httpx, name), "__init__", forbidden)
    for _ in range(200):
        cls(api_key=CREDENTIAL)
    assert not _pool._sync and not any(_pool._async.values())
