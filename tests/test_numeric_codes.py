from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from support import (
    AUDIO,
    MODEL,
    WALL,
    Clock,
    Service,
    accepted,
    client,
    completed,
    grant,
    transcribe,
)

import machinera as m
from machinera._contract import (
    ERROR_CODES,
    SYNC_CAP_FALLBACK_CODES,
    SYNC_FALLBACK_CODES,
    SYNC_REPLAYABLE_CODES,
    UPLOAD_GRANT_STATES,
)
from machinera._uploads import Grant


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code", sorted(SYNC_FALLBACK_CODES | SYNC_CAP_FALLBACK_CODES))
def test_numeric_sync_to_job_fallback_acceptance(code: int, asynchronous: bool) -> None:
    calls: list[httpx.Request] = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            entry = ERROR_CODES[code]
            return httpx.Response(
                entry.status,
                json={"error": {"code": code, "retryable": entry.retryable}},
                headers={"Retry-After": "0"},
            )
        return accepted() if request.method == "POST" else completed()

    with client(handler, clock, asynchronous=asynchronous) as sdk:
        output = sdk.transcribe_file(b"fLaC", model=MODEL)
    sync_count = m.RetryPolicy().max_attempts if code in SYNC_REPLAYABLE_CODES else 1
    assert [request.url.path for request in calls] == [
        *(["/v1/audio/transcriptions"] * sync_count),
        "/v1/transcription_jobs",
        "/v1/transcription_jobs/job-1",
    ]
    assert output.job_id == "job-1"
    assert all(request.content == calls[-2].content for request in calls[:sync_count])
    assert calls[-2].headers["idempotency-key"]
    assert clock.sleeps == ([0.4375, 0.875] if sync_count == 3 else [])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code", [4005, 4006, 4020])
def test_current_nonretryable_refusals_do_not_retry_or_fallback(
    code: int, asynchronous: bool
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(ERROR_CODES[code].status, json={"error": {"code": code}})

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(m.APIError) as caught:
            sdk.transcribe_file(b"fLaC", model=MODEL)
    assert len(calls) == 1 and caught.value.code == code
    assert caught.value.retryable is False


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code", ["4001", "old-code", True, 4001.0, [], {}])
def test_nonnumeric_error_codes_are_rejected(code: object, asynchronous: bool) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"code": code, "retryable": True}})

    with client(handler, asynchronous=asynchronous) as sdk:
        with pytest.raises(m.APIResponseValidationError):
            sdk.get_job("job-1")
    assert calls == 1
    with pytest.raises(ValidationError):
        m.JobError.model_validate({"code": code})
    with pytest.raises(ValidationError):
        m.TranscriptionResult.model_validate({"text": "", "warnings": [{"code": code}]})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unknown_integer_retains_status_and_explicit_guidance(asynchronous: bool) -> None:
    with client(
        lambda _: httpx.Response(429, json={"error": {"code": 3999, "retryable": False}}),
        asynchronous=asynchronous,
    ) as sdk:
        with pytest.raises(m.RateLimitError) as caught:
            sdk.get_job("job-1")
    assert caught.value.code == 3999 and type(caught.value.code) is int
    assert caught.value.retryable is False


def test_published_upload_grant_fixture() -> None:
    data = json.loads((Path(__file__).parent / "fixtures/upload_grant.json").read_text())
    expected = {
        "size_bytes": 4,
        "content_type": "audio/flac",
        "content_md5": data["required_headers"]["Content-MD5"],
    }
    # OpenAPI property requirements; public states come from the current contract.
    schema = json.loads((Path(__file__).parent / "fixtures/upload_grant_schema.json").read_text())
    assert set(schema["required"]) <= data.keys() <= schema["properties"].keys()
    assert data["state"] in UPLOAD_GRANT_STATES
    assert schema["properties"]["state"]["enum"] == list(UPLOAD_GRANT_STATES)
    limits = schema["properties"]["limits"]
    assert set(limits["required"]) == data["limits"].keys() == limits["properties"].keys()
    assert all(type(value) is int for value in data["limits"].values())
    parsed = Grant.parse(data, expected, 201)
    assert parsed.state == "pending" and len(parsed.limits) == 6
    assert parsed.expires_at <= parsed.upload_deadline
    assert parsed.submit_expires_at is None
    assert parsed.limits["put_ttl_seconds"] == 3600
    assert parsed.limits["submit_grace_seconds"] == 300
    data["expires_at"] = data["upload_deadline"] + 1
    with pytest.raises(m.APIResponseValidationError):
        Grant.parse(data, expected, 200)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_lapsed_grant_preserves_ceiling_and_confirms_expiry(
    asynchronous: bool, expired: bool
) -> None:
    clock = Clock()
    service = Service()
    deadline = WALL + 5
    returned = []
    puts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        if request.method == "PUT":
            puts += 1
            if puts == 1:
                clock.now = 2
                raise httpx.ReadError("transfer interrupted")
        response = service(request)
        if request.url.path.endswith("/uploads"):
            replay = len(service.initializations) == 2
            if replay and expired:
                clock.now = 6
                return httpx.Response(
                    410,
                    json={"error": {"code": 1003, "retryable": False}},
                    headers={"Retry-After": "0"},
                )
            body = grant(
                service.descriptor,
                expires_at=deadline if replay else WALL + 1,
                upload_deadline=deadline,
            )
            returned.append(body)
            return httpx.Response(200 if replay else 201, json=body)
        return response

    with client(
        handler,
        clock,
        asynchronous=asynchronous,
        limits=m.Limits(1, 2),
        wall_clock=lambda: WALL + clock.now,
    ) as sdk:
        assert transcribe(sdk, idempotency_key="saved-key").job_id == "job-1"
    assert len(service.submissions) == 1
    assert len(service.initializations) == 2
    requests = [r for r in service.calls if r.url.path.endswith("/uploads")]
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    assert requests[0].content == requests[1].content
    assert all(item["upload_deadline"] == deadline for item in returned)
    assert all(item["state"] == "pending" for item in returned)
    assert service.puts == ([] if expired else [AUDIO])
    assert clock.sleeps == [0.4375]
