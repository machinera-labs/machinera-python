from __future__ import annotations

import logging

import httpx
import pytest
from support import (
    AUDIO,
    CREDENTIAL,
    MODEL,
    SIGNED,
    Clock,
    Service,
    accepted,
    client,
    completed,
    failed_job,
    queued,
    result,
    transcribe,
)

from machinera import APIError, Limits, RetryPolicy


@pytest.fixture(params=[False, True], ids=["blocking", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "machinera"]


def test_upload_and_poll_timings(asynchronous: bool, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="machinera")
    clock, service = Clock(), Service()
    polls = iter(
        [
            queued(),
            queued(),
            httpx.Response(200, json={"id": "job-1", "status": "processing"}),
            completed(),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            clock.now += 0.2
            response = next(polls)
            response.headers["x-request-id"] = "poll-request"
            return response
        clock.now += 2 if request.method == "PUT" else 1
        response = service(request)
        response.headers["x-request-id"] = "api-request"
        return response

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=Limits(1, 1),
        retry_policy=RetryPolicy(poll_interval=3),
    ) as sdk:
        transcribe(sdk)
    assert messages(caplog) == [
        "upload_init 1.0s request_id=api-request job_id=None",
        f"upload_put 2.0s {len(AUDIO)} bytes 0.2 MB (0.08 MB/s) request_id=api-request job_id=None",
        "submit 1.0s request_id=api-request job_id=job-1",
        "poll 0.2s request_id=poll-request job_id=job-1",
        "Job job-1 status queued",
        "poll 0.2s request_id=poll-request job_id=job-1",
        "poll 0.2s request_id=poll-request job_id=job-1",
        "job queued 6.4s request_id=poll-request job_id=job-1",
        "Job job-1 status processing",
        "poll 0.2s request_id=poll-request job_id=job-1",
        "job processing 3.2s request_id=poll-request job_id=job-1",
        "Job job-1 status completed",
        "total 13.8s request_id=poll-request job_id=job-1",
    ]


@pytest.mark.parametrize("source", ["file", "url"])
def test_job_submit_timing(
    asynchronous: bool, source: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="machinera")
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 2
        return accepted() if request.method == "POST" else completed()

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        if source == "file":
            transcribe(sdk, idempotency_key="private-operation-key")
        else:
            sdk.transcribe_url(SIGNED, model=MODEL)
    logs = messages(caplog)
    assert logs[0] == "job_submit 2.0s request_id=None job_id=job-1"
    assert logs[-1] == "total 4.0s request_id=request-1 job_id=job-1"
    assert not any(line.startswith(("job queued", "job processing")) for line in logs)


@pytest.mark.parametrize("code", [None, 4002, 4005, 4006, 4008])
def test_sync_timing_and_fallback(
    asynchronous: bool, code: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="machinera")
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 2
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(
                200 if code is None else 503,
                json=result() if code is None else {"error": {"code": code}},
                headers={"x-request-id": "sync-request"},
            )
        return accepted() if request.method == "POST" else completed()

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        transcribe(sdk)
    logs = messages(caplog)
    assert logs[0] == "sync_submit 2.0s request_id=sync-request job_id=None"
    if code is None:
        assert logs == [logs[0], "total 2.0s request_id=sync-request job_id=None"]
    else:
        assert logs[1] == (
            f"sync_submit fallback to job reason_code={code} request_id=sync-request job_id=None"
        )
        assert logs[2] == "job_submit 2.0s request_id=None job_id=job-1"
        assert logs[-1] == "total 6.0s request_id=request-1 job_id=job-1"


@pytest.mark.parametrize("terminal", ["completed", "error"])
def test_resume_observed_processing_ends_at_terminal_status(
    asynchronous: bool, terminal: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="machinera")
    clock = Clock()
    responses = iter(
        [
            httpx.Response(200, json={"id": "job-1", "status": "processing"}),
            completed() if terminal == "completed" else failed_job(),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        clock.now += 1
        return next(responses)

    with client(
        handler, clock, asynchronous=asynchronous, retry_policy=RetryPolicy(poll_interval=2)
    ) as sdk:
        if terminal == "error":
            with pytest.raises(APIError):
                sdk.resume("job-1")
        else:
            sdk.resume("job-1")
    logs = messages(caplog)
    assert any(line.startswith("job processing 3.0s ") for line in logs)
    assert not any(line.startswith("job queued") for line in logs)
    assert logs[-1].startswith("total 4.0s ")


@pytest.mark.parametrize("level", [logging.NOTSET, logging.WARNING])
def test_default_and_warning_are_quiet(
    asynchronous: bool,
    level: int,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    sdk_logger: logging.Logger,
) -> None:
    monkeypatch.delenv("MACHINERA_LOG", raising=False)
    caplog.set_level(logging.WARNING)
    sdk_logger.setLevel(level)
    with client(Service(), asynchronous=asynchronous, limits=Limits(1, 1)) as sdk:
        transcribe(sdk)
    assert not messages(caplog)


@pytest.mark.parametrize("level", ["info", "debug"])
def test_env_timing_logs_do_not_leak_content_or_credentials(
    asynchronous: bool,
    level: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    sdk_logger: logging.Logger,
) -> None:
    monkeypatch.setenv("MACHINERA_LOG", level)
    service = Service()

    def handler(request: httpx.Request) -> httpx.Response:
        response = service(request)
        if request.method == "GET":
            response = completed("private transcript")
        response.headers["x-request-id"] = SIGNED if request.method == "POST" else CREDENTIAL
        return response

    with client(handler, asynchronous=asynchronous, limits=Limits(1, 1)) as sdk:
        sdk.transcribe_file(
            AUDIO,
            model=MODEL,
            idempotency_key="private-operation-key",
            filename="private-audio.wav",
        )
    logs = "\n".join(messages(caplog))
    assert "upload_put 0.0s" in logs and "rate unavailable" in logs
    assert "total 0.0s" in logs
    for secret in (
        SIGNED,
        "private-signature",
        CREDENTIAL,
        "private transcript",
        "private-operation-key",
        "private-audio.wav",
    ):
        assert secret not in caplog.text


def test_retry_attempt_timings_and_total_include_backoff(
    asynchronous: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="machinera")
    clock = Clock()
    responses = iter(
        [
            httpx.Response(503, json={"error": {"code": 4001, "retryable": True}}),
            accepted(),
            completed(),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        clock.now += 1
        return next(responses)

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        sdk.transcribe_url(SIGNED, model=MODEL)
    logs = messages(caplog)
    assert len([line for line in logs if line.startswith("job_submit 1.0s")]) == 2
    assert logs[-1] == "total 3.4s request_id=request-1 job_id=job-1"
