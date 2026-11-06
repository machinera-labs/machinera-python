from __future__ import annotations

import logging

import httpx
import pytest
from support import AUDIO, CREDENTIAL, MODEL, SIGNED, Service, client, result

from machinera import Limits


@pytest.mark.parametrize("asynchronous", [False, True], ids=["blocking", "async"])
@pytest.mark.parametrize("level", ["info", "debug"])
@pytest.mark.parametrize("route", ["sync", "multipart", "upload", "url"])
def test_logger_excludes_secrets_at_every_emitted_level(
    asynchronous: bool,
    level: str,
    route: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    sdk_logger: logging.Logger,
) -> None:
    monkeypatch.setenv("MACHINERA_LOG", level)
    caplog.set_level(logging.DEBUG, logger="machinera")
    service = Service()
    attempts: set[tuple[str, str]] = set()
    requests: list[httpx.Request] = []
    authorization = "Bearer " + CREDENTIAL

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        exchange = (request.method, request.url.path)
        if exchange not in attempts:
            attempts.add(exchange)
            return httpx.Response(
                503,
                json={"error": {"code": 4001, "retryable": True}},
                headers={"x-request-id": "request-retry"},
            )
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json=result())
        return service(request)

    with client(
        handler,
        asynchronous=asynchronous,
        limits=Limits(1, 1) if route == "upload" else Limits(),
        sync_replay="always",
    ) as sdk:
        if route == "url":
            sdk.transcribe_url(SIGNED, model=MODEL)
        else:
            sdk.transcribe_file(
                AUDIO,
                model=MODEL,
                filename="recording.wav",
                idempotency_key="saved-operation" if route in ("multipart", "upload") else None,
            )

    assert any(request.headers.get("authorization") == authorization for request in requests)
    if route == "upload":
        assert service.puts == [AUDIO]
        assert any(str(request.url) == SIGNED for request in requests)
    elif route != "url":
        assert any(AUDIO in request.content for request in requests)

    records = [record for record in caplog.records if record.name == sdk_logger.name]
    assert records
    # Inspect every emitted level, including raw arguments custom handlers can format.
    output = "\n".join(logging.Formatter().format(record) + repr(record.args) for record in records)
    for secret in (
        CREDENTIAL,
        authorization,
        SIGNED,
        SIGNED.partition("?")[2],
        "private-signature",
        AUDIO.decode(),
        "audio-bytes",
    ):
        assert secret not in output
    if level == "debug":
        retries = [record.getMessage() for record in records if record.levelno == logging.DEBUG]
        assert any(
            "status=503" in message and "request_id=request-retry" in message for message in retries
        )
    assert any(record.levelno == logging.INFO for record in records)
