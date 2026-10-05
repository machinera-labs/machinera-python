from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from support import MODEL, accepted, client, completed, error_code, recorder, result

import machinera as m
from machinera import _contract as contract
from machinera._types import STAGED_UPLOAD_THRESHOLD_BYTES


def test_generated_contract_digest() -> None:
    source = Path(contract.__file__).read_bytes()
    _, header, body = source.split(b"\n", 2)
    assert header == b"# contract-sha256: " + hashlib.sha256(body).hexdigest().encode()


def test_service_error_literals_only_appear_in_generated_contract() -> None:
    root = Path(contract.__file__).parent
    for path in root.rglob("*.py"):
        if path.name == "_contract.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in contract.ERROR_CODES, (path.name, node.lineno)


def test_contract_constants_and_static_response_formats() -> None:
    limits = m.Limits()
    assert limits.sync_inline_body_bytes == contract.DEFAULT_SYNC_CAP_BYTES
    assert limits.job_inline_body_bytes == STAGED_UPLOAD_THRESHOLD_BYTES
    assert STAGED_UPLOAD_THRESHOLD_BYTES <= contract.DEFAULT_INLINE_CAP_BYTES
    assert limits.descriptor_bytes == contract.MAX_DESCRIPTOR_BYTES
    assert m.SUPPORTED_MEDIA_SUFFIXES is contract.SUPPORTED_MEDIA_SUFFIXES
    assert set(get_args(m.ResponseFormat)) == contract.RESPONSE_FORMATS
    source = Path(contract.__file__).with_name("_types.py").read_text()
    static = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )
    assignment = static.body[0]
    assert isinstance(assignment, ast.Assign)
    assert isinstance(assignment.value, ast.Subscript)
    assert set(ast.literal_eval(assignment.value.slice)) == contract.RESPONSE_FORMATS


@pytest.mark.parametrize("entry", contract.ERROR_CODES.values(), ids=lambda entry: entry.code)
def test_every_contract_error_maps_to_its_exception(entry: contract.ErrorCode) -> None:
    expected: type[m.APIError] = {
        400: m.BadRequestError,
        401: m.AuthenticationError,
        403: m.PermissionDeniedError,
        404: m.NotFoundError,
        409: m.ConflictError,
        413: m.PayloadTooLargeError,
        422: m.UnprocessableEntityError,
        429: m.RateLimitError,
    }.get(entry.status, m.InternalServerError if entry.status >= 500 else m.APIStatusError)
    if entry.status == 200:
        expected = m.TerminalJobError
    elif entry.code.startswith("upload_") and entry.status != 429:
        expected = m.UploadError
    if entry.code == "upload_integrity_mismatch":
        expected = m.IntegrityError
    with client(lambda _: completed()) as sdk:
        error = sdk._error(
            httpx.Response(entry.status, json={"error": entry._asdict()}),
            terminal=entry.status == 200,
        )
    assert type(error) is expected
    assert error.code == entry.code and error.status_code == entry.status
    assert error.retryable is entry.retryable
    assert error.body is not None and error.body["code"] == entry.code


@pytest.mark.parametrize(
    "status,retryable", [(429, True), (502, True), (503, True), (504, True), (500, False)]
)
@pytest.mark.parametrize("body", [{"json": {"error": {}}}, {"text": "<html>busy</html>"}])
def test_status_heuristic_without_service_guidance(
    status: int, retryable: bool, body: dict[str, Any]
) -> None:
    calls: list[httpx.Request] = []
    with client(recorder(lambda _: httpx.Response(status, **body), calls)) as sdk:
        with pytest.raises(m.APIStatusError) as caught:
            sdk.get_job("job-1")
    error = caught.value
    assert len(calls) == (m.RetryPolicy().max_attempts if retryable else 1)
    assert error.code is None and error.retryable is retryable


@pytest.mark.parametrize(
    "body,attempts,status,operation,kind",
    [
        (body, attempts, status, operation, m.APIError)
        for body, attempts in [
            ({"error": {"code": error_code(503, False), "retryable": True}}, 2),
            ({"error": {"code": "input_busy", "retryable": False}}, 1),
            ({"error": {"code": "input_busy"}}, 2),
            ({"error": {"code": error_code(503, False)}}, 1),
            ({"error": {"code": "future_code"}}, 2),
            ({"error": {"code": "future_code", "retryable": False}}, 1),
            ({}, 2),
            ({"error": {}}, 2),
            ({"error": {"message": "Temporarily unavailable"}}, 2),
            ({"message": "Temporarily unavailable"}, 2),
            ({"error": {"code": [], "retryable": None}}, 2),
            ({"error": {"retryable": "false"}}, 2),
            ({"error": {"retryable": False}}, 1),
            (None, 2),
        ]
        for status in (429, 503)
        for operation in ("get_job", "transcribe_url")
    ]
    + [
        ({"error": {"code": code, "retryable": guidance}}, 1, 400, "keyed_file", m.BadRequestError)
        for code, guidance in (
            ("invalid_request", None),
            ("invalid_request", False),
            ("content_md5_mismatch", False),
        )
    ]
    + [
        (
            {"error": {"code": "temporary_refusal", "retryable": True}},
            2,
            400,
            "get_job",
            m.APIError,
        ),
        (
            {"error": {"code": "inline_completion_timeout", "retryable": True}},
            1,
            504,
            "file",
            m.InternalServerError,
        ),
    ],
)
def test_retry_guidance_precedence(
    body: dict[str, Any] | None, attempts: int, status: int, operation: str, kind: type[m.APIError]
) -> None:
    calls: list[httpx.Request] = []
    method = "GET" if operation == "get_job" else "POST"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != method:
            return completed()
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        if method == "POST":
            return accepted()
        return completed()

    with client(handler) as sdk:

        def invoke() -> m.JobSnapshot | m.TranscriptionResult:
            if operation == "transcribe_url":
                return sdk.transcribe_url(
                    "https://audio.example/clip.wav", model=MODEL, idempotency_key="saved-key"
                )
            if operation == "get_job":
                return sdk.get_job("job-1")
            return sdk.transcribe_file(
                b"audio",
                model=MODEL,
                content_type="audio/wav",
                **({"idempotency_key": "saved-key"} if operation == "keyed_file" else {}),
            )

        if attempts == 1:
            with pytest.raises(kind):
                invoke()
        else:
            output = invoke()
            if isinstance(output, m.JobSnapshot):
                assert output.status == "completed"
            else:
                assert output.text == result()["text"]
    assert len(calls) == attempts
    if method == "POST" and operation != "file":
        assert all(request.headers["idempotency-key"] == "saved-key" for request in calls)
    if attempts == 2:
        assert calls[0].url == calls[1].url
        assert calls[0].content == calls[1].content
        assert calls[0].headers == calls[1].headers


@pytest.mark.parametrize("text", ["", " \t\n ", "  exact\ntext  "])
@pytest.mark.parametrize("response_format", sorted(contract.RESPONSE_FORMATS))
def test_models_preserve_exact_body_and_projections(text: str, response_format: str) -> None:
    body = result(text)
    body["words"] = [{"word": text, "start": 0, "end": 1, "future": [None, ""]}]
    body["warnings"] = ["", " \n ", {"code": "", "message": " \t ", "extra": [None]}]
    body["future"] = {"nested": [None, "", " \t "]}
    model = m.TranscriptionResult.model_validate(body).model_copy(
        update={"response_format": response_format}
    )
    assert isinstance(model, BaseModel)
    assert model.text == text and model.to_text() == text
    assert model.words is not None and model.words[0].word == text
    assert model.words[0].raw == body["words"][0]
    assert model.warnings is body["warnings"]
    assert model.raw == body and model.to_verbose_json() is not model.raw
    assert json.dumps(model.to_verbose_json()).encode() == json.dumps(body.copy()).encode()
    assert model.to_json() == {"text": text, "usage": body["usage"]}
    expected = (
        text
        if response_format == "text"
        else model.to_json()
        if response_format == "json"
        else body
    )
    assert model.output == expected and isinstance(type(model).output, property)
    job_body = {
        "id": "job-1",
        "status": "completed",
        "result": body,
        "future": "",
        "warnings": [text],
    }
    snapshot = m.JobSnapshot.model_validate(job_body)
    assert snapshot.raw == job_body and snapshot.raw["future"] == ""
    assert snapshot.warnings == [text]
    assert snapshot.result is not None and snapshot.result.text == text
    assert snapshot.result.raw == body
    for item, field in ((model, "text"), (snapshot, "status")):
        with pytest.raises(ValidationError):
            setattr(item, field, "changed")
    with pytest.raises((AttributeError, ValidationError)):
        model.output = "changed"  # type: ignore[misc]


def test_snapshot_metadata_and_typed_error() -> None:
    error = {
        "code": "",
        "message": " \n ",
        "type": "",
        "retryable": False,
        "details": {"extra": 1},
        "future": "",
    }
    snapshot = m.JobSnapshot.model_validate(
        {
            "id": "job-1",
            "status": "error",
            "created_at": 1,
            "updated_at": 2,
            "eta_seconds": 0,
            "error": error,
        }
    )
    assert (snapshot.created_at, snapshot.updated_at, snapshot.eta_seconds) == (1, 2, 0)
    assert isinstance(snapshot.error, m.JobError)
    assert snapshot.error.message == " \n " and snapshot.error.raw == error
    minimal = m.JobSnapshot(id="job-1", status="queued")
    assert minimal.result is minimal.error is minimal.created_at is minimal.updated_at is None
    assert minimal.eta_seconds is minimal.warnings is None


@pytest.mark.parametrize("language", [contract.SERVED_LANGUAGE, contract.SERVED_LANGUAGE + "-US"])
def test_served_language_hint(language: str) -> None:
    with client(lambda _: httpx.Response(200, json={"text": ""})) as sdk:
        assert (
            sdk.transcribe_file(b"audio", model=MODEL, filename="clip.wav", language=language).text
            == ""
        )


def test_unsupported_language_fails_before_http() -> None:
    calls = []
    with client(lambda request: calls.append(request)) as sdk:
        with pytest.raises(ValueError, match="English"):
            sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL, language="fr")
    assert calls == []


@pytest.mark.parametrize(
    "extension,text",
    [({}, "actual"), ({"text": "different"}, ""), (None, " \t\n "), (["extension"], "actual")],
)
@pytest.mark.parametrize("operation", ["get_job", "resume", "transcribe_file"])
def test_wire_raw_extension_preserves_result(extension: Any, text: str, operation: str) -> None:
    body = {
        **result(text),
        "raw": extension,
        "words": [{"word": text, "start": 0, "end": 1, "raw": extension}],
    }
    job_body = {
        "id": "job-1",
        "status": "completed",
        "result": body,
        "raw": {"id": "different", "status": "queued", "result": {"text": "different"}},
    }
    payload = body if operation == "transcribe_file" else job_body
    with client(lambda _: httpx.Response(200, json=payload)) as sdk:
        if operation == "get_job":
            snapshot = sdk.get_job("job-1")
            assert snapshot.raw == job_body
            assert snapshot.id == "job-1" and snapshot.status == "completed"
            output = snapshot.result
        elif operation == "resume":
            output = sdk.resume("job-1")
        else:
            output = sdk.transcribe_file(b"audio", model=MODEL, filename="clip.wav")
    assert output is not None and output.text == text
    assert output.raw == body and output.raw["raw"] == extension
    assert output.model_extra is not None and output.model_extra["raw"] == extension
    assert output.words is not None and output.words[0].word == text
    assert output.words[0].start == 0 and output.words[0].end == 1
    assert output.words[0].raw == body["words"][0]
    assert output.to_text() == text
    assert output.to_json() == {"text": text, "usage": body["usage"]}
    assert json.dumps(output.to_verbose_json()).encode() == json.dumps(body).encode()


@pytest.mark.parametrize(
    "body",
    [
        {"raw": {"text": "invented"}},
        {"text": None, "raw": {"text": "invented"}},
        {"text": "actual", "words": [{"raw": {"word": "invented", "start": 0, "end": 1}}]},
        {"text": "actual", "words": [{"word": "actual", "raw": {"start": 0, "end": 1}}]},
    ],
)
@pytest.mark.parametrize("operation", ["get_job", "resume", "transcribe_file"])
def test_wire_raw_extension_cannot_supply_required_fields(
    body: dict[str, Any], operation: str
) -> None:
    payload = (
        body
        if operation == "transcribe_file"
        else {"id": "job-1", "status": "completed", "result": body}
    )
    with client(lambda _: httpx.Response(200, json=payload)) as sdk:
        with pytest.raises(m.APIError) as caught:
            if operation == "transcribe_file":
                sdk.transcribe_file(b"audio", model=MODEL, filename="clip.wav")
            else:
                getattr(sdk, operation)("job-1")
    assert "invented" not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.parametrize("operation", ["get_job", "resume"])
def test_wire_raw_extension_cannot_supply_job_fields(operation: str) -> None:
    body = {"raw": {"id": "job-1", "status": "completed", "result": {"text": "invented"}}}
    with client(lambda _: httpx.Response(200, json=body)) as sdk:
        with pytest.raises(m.APIError):
            getattr(sdk, operation)("job-1")


def test_wire_raw_extension_cannot_populate_error_fields() -> None:
    extension = {
        "code": "future_code",
        "message": "invented",
        "type": "future_type",
        "retryable": True,
        "details": {"future": True},
    }
    body = {"raw": extension}
    snapshot = m.JobSnapshot.model_validate({"id": "job-1", "status": "error", "error": body})
    assert snapshot.error is not None
    assert snapshot.error.raw == body
    for name in extension:
        assert getattr(snapshot.error, name) is None
    actual = {**extension, "message": "actual", "retryable": False, "raw": extension}
    error = m.JobError.model_validate(actual)
    assert error.message == "actual" and error.retryable is False
    assert error.raw == actual


MALFORMED_RESULTS = [
    ("text", 12),
    ("text", None),
    ("words", [{"word": 12, "start": 0, "end": 1}]),
    ("warnings", [12]),
    ("warnings", [{"message": 12}]),
]
MALFORMED_SNAPSHOTS = [
    ("id", 12),
    ("status", []),
    ("created_at", "secret"),
    ("eta_seconds", "secret"),
    ("error", {"retryable": "secret"}),
]
MALFORMED_RESPONSES = {
    "missing_text": httpx.Response(200, json={"id": "job-1", "status": "completed", "result": {}}),
    "invalid_json": httpx.Response(200, text="<html>invalid</html>"),
    "bad_status": httpx.Response(200, json={"id": "job-1", "status": 7}),
    "wrong_id": httpx.Response(200, json={"id": "job-2", "status": "queued"}),
    "no_result": httpx.Response(200, json={"id": "job-1", "status": "completed"}),
    "admission": httpx.Response(202, json={"status": "queued"}),
    "sync": httpx.Response(200, json={"text": 7}),
}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "method,response",
    [
        pytest.param(
            method,
            httpx.Response(
                200,
                json=(
                    body
                    if method == "transcribe_file"
                    else {"id": "job-1", "status": "completed", "result": body}
                ),
            ),
            id=f"result-{field}-{i}-{method}",
        )
        for i, (field, value) in enumerate(MALFORMED_RESULTS)
        for body in [{**result(), field: value, "secret": "sensitive-response-marker"}]
        for method in ("transcribe_file", "get_job", "resume")
    ]
    + [
        pytest.param(
            "get_job",
            httpx.Response(200, json={"id": "job-1", "status": "queued", field: value}),
            id=f"snapshot-{field}",
        )
        for field, value in MALFORMED_SNAPSHOTS
    ]
    + [
        pytest.param(
            "transcribe_url"
            if kind == "admission"
            else "transcribe_file"
            if kind == "sync"
            else "resume",
            response,
            id=kind,
        )
        for kind, response in MALFORMED_RESPONSES.items()
    ],
)
def test_malformed_responses_are_sanitized(
    method: str, response: httpx.Response, asynchronous: bool
) -> None:
    calls: list[httpx.Request] = []
    response = httpx.Response(
        response.status_code, content=response.content, headers=response.headers
    )
    with client(recorder(lambda _: response, calls), asynchronous=asynchronous) as sdk:
        with pytest.raises(m.APIResponseValidationError) as caught:
            if method == "transcribe_file":
                sdk.transcribe_file(b"audio", model=MODEL, filename="clip.wav")
            elif method == "transcribe_url":
                sdk.transcribe_url("https://audio.example/a", model=MODEL)
            else:
                getattr(sdk, method)("job-1")
    error = caught.value
    assert not isinstance(error, m.APIConnectionError)
    assert error.retryable is False and len(calls) == 1
    assert error.status_code == response.status_code and error.body is None
    assert "secret" not in str(error) and "sensitive-response-marker" not in str(error)
    assert error.__context__ is error.__cause__ is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_poll_reuses_validated_result(monkeypatch: pytest.MonkeyPatch, asynchronous: bool) -> None:
    validated: list[m.TranscriptionResult] = []
    cls = m.AsyncMachinera if asynchronous else m.Machinera
    original = cls._read_job

    def read_job(self: m.Machinera | m.AsyncMachinera, call: Any) -> Any:
        snapshot, response = yield from original(self, call)
        assert snapshot.result is not None
        validated.append(snapshot.result)
        snapshot.result.raw["text"] = "changed after validation"
        return snapshot, response

    monkeypatch.setattr(cls, "_read_job", read_job)
    with client(lambda _: completed("actual"), asynchronous=asynchronous) as sdk:
        output = sdk.resume("job-1", response_format="text")
    assert output.text == output.output == "actual"
    assert output is not validated[0]
    assert output.raw["text"] == "changed after validation"
    assert output.raw is validated[0].raw
    assert output.words is validated[0].words
    assert output.job_id == "job-1" and output.request_id == "request-1"
    assert output.elapsed_seconds >= 0
    assert validated[0].job_id is None and validated[0].response_format == "json"
    assert validated[0].request_id is None and validated[0].elapsed_seconds == 0
