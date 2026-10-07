from __future__ import annotations

import ast
import asyncio
import inspect
import random
import re
import time
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from support import MODEL, client, completed

import machinera as m
from machinera import _contract as contract
from machinera import _core
from machinera._files import validate_headers
from machinera._types import UNSET

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = (ROOT / "api.md").read_text()
README = (ROOT / "README.md").read_text()


def section(text: str, title: str) -> str:
    return text.split(title, 1)[1].split("\n### ", 1)[0]


def test_status_literals_and_polling_match_contract() -> None:
    assert set(get_args(m.JobStatus)) == set(contract.JOB_STATUSES)
    assert _core.PENDING_JOB_STATUSES is contract.PENDING_JOB_STATUSES
    documented = section(REFERENCE, "### `JobStatus`")
    literal = re.search(r"Literal\[([^]]+)\]", documented)
    assert literal is not None
    assert set(ast.literal_eval(literal[1])) == set(contract.JOB_STATUSES)
    pending = documented.split("While polling,", 1)[1].split("continue", 1)[0]
    assert set(re.findall(r'`"([^"]+)"`', pending)) == contract.PENDING_JOB_STATUSES


@pytest.mark.parametrize("status", sorted(contract.PENDING_JOB_STATUSES))
def test_all_pending_statuses_continue_polling(status: str) -> None:
    responses = iter([httpx.Response(200, json={"id": "job-1", "status": status}), completed()])
    with client(lambda _: next(responses)) as sdk:
        assert sdk.resume("job-1").job_id == "job-1"


@pytest.mark.parametrize("header", [contract.IDEMPOTENCY_KEY_HEADER, contract.CONTENT_MD5_HEADER])
@pytest.mark.parametrize("part", [False, True])
def test_contract_headers_are_reserved(header: str, part: bool) -> None:
    with pytest.raises(ValueError, match="Cannot override"):
        validate_headers({header.swapcase(): "override"}, part=part)


def test_contract_headers_on_requests_and_retry_parsing() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1"})
        return completed()

    with client(handler) as sdk:
        sdk.transcribe_file(b"audio", filename="clip.wav", model=MODEL, idempotency_key="key")
        assert (
            sdk._retry_after(httpx.Response(429, headers={contract.RETRY_AFTER_HEADER: "7"})) == 7
        )
        assert (
            sdk._retry_after(
                httpx.Response(
                    429, headers={contract.RETRY_AFTER_HEADER: "Tue, 14 Nov 2023 22:13:27 GMT"}
                )
            )
            == 7
        )
    assert calls[0].headers[contract.IDEMPOTENCY_KEY_HEADER] == "key"
    assert calls[0].headers[contract.CONTENT_MD5_HEADER]
    assert "Content-MD5" not in calls[0].headers


def test_documented_and_example_model_aliases() -> None:
    assert MODEL in contract.PUBLISHED_MODEL_ALIASES
    texts = [(ROOT / name).read_text() for name in ("README.md", "api.md", "examples/README.md")]
    sources = [path.read_text() for path in (ROOT / "examples").glob("*.py")]
    models = []
    for source in [*texts, *sources]:
        models.extend(re.findall(r'\bmodel\s*=\s*["\']([^"\']+)["\']', source))
    for source, pattern in zip(
        texts,
        [r"\*\*Model:\*\* `([^`]+)`", r'Use `model="([^"]+)"`', r"uses the `([^`]+)` model"],
        strict=True,
    ):
        match = re.search(pattern, source)
        assert match is not None
        models.append(match[1])
    assert models and set(models) <= set(contract.PUBLISHED_MODEL_ALIASES)


def test_documented_numeric_behavior_sets() -> None:
    upload = REFERENCE.split("| `UploadError` |", 1)[1].split("raise it", 1)[0]
    assert {int(code) for code in re.findall(r"`(\d+)`", upload)} == contract.UPLOAD_ERROR_CODES
    fallback = REFERENCE.split("applies to a size refusal (", 1)[1].split(")", 1)[0]
    assert {
        int(code) for code in re.findall(r"`(\d+)`", fallback)
    } == contract.SYNC_CAP_FALLBACK_CODES


def test_documented_formats_and_language() -> None:
    formats = section(REFERENCE, "### `ResponseFormat`")
    match = re.search(r"Literal\[([^]]+)\]", formats)
    assert match is not None
    assert set(ast.literal_eval(match[1])) == contract.RESPONSE_FORMATS
    summary = README.split("- **Output:**", 1)[1].split("\n", 1)[0]
    assert set(re.findall(r'`"([^"]+)"`', summary)) == contract.RESPONSE_FORMATS
    assert contract.SERVED_LANGUAGE == "en", "Update the English-language descriptions"
    language = REFERENCE.split("`language` accepts", 1)[1].split("There is no prompt", 1)[0]
    assert f"`{contract.SERVED_LANGUAGE}`" in language
    assert f"`{contract.SERVED_LANGUAGE}-*`" in language
    summary = README.split("- **English only:**", 1)[1].split("- **No prompt:**", 1)[0]
    assert f'`"{contract.SERVED_LANGUAGE}"`' in summary
    assert f"`{contract.SERVED_LANGUAGE}-*`" in summary


def test_validation_messages_derive_from_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_core, "RESPONSE_FORMATS", frozenset({"future-format"}))
    monkeypatch.setattr(_core, "SERVED_LANGUAGE", "zz")
    with client(lambda _: pytest.fail("validation must precede HTTP")) as sdk:
        with pytest.raises(ValueError, match="future-format"):
            sdk.transcribe_url("https://audio.example/clip.wav", model=MODEL)
        with pytest.raises(ValueError, match="zz"):
            sdk.transcribe_url(
                "https://audio.example/clip.wav",
                model=MODEL,
                response_format="future-format",
                language="en",
            )


def documented_signature(name: str) -> inspect.Signature:
    block = next(
        block
        for block in re.findall(r"```python\n(.*?)```", REFERENCE, re.S)
        if block.startswith(name + "(")
    )
    namespace: dict[str, Any] = {"time": time, "random": random}
    exec("from __future__ import annotations\ndef " + block.rstrip() + ": pass", namespace)
    return inspect.signature(namespace[name])


def normalized_annotation(annotation: Any) -> str:
    source = str(annotation).replace("int | Unset", "int")
    if source == "Timeout":
        source = "float | httpx.Timeout | TimeoutPolicy | None"
    return ast.unparse(ast.parse(source))


@pytest.mark.parametrize(
    "name", ["Machinera", "transcribe_file", "transcribe_url", "get_job", "resume"]
)
def test_reference_signatures(name: str) -> None:
    actual = inspect.signature(m.Machinera if name == "Machinera" else getattr(m.Machinera, name))
    documented = documented_signature(name)
    parameters = {key: value for key, value in actual.parameters.items() if key != "self"}
    assert list(documented.parameters) == list(parameters)
    for key, parameter in parameters.items():
        copy = documented.parameters[key]
        assert copy.kind == parameter.kind
        assert copy.default == (Ellipsis if parameter.default is UNSET else parameter.default)
        assert normalized_annotation(copy.annotation) == normalized_annotation(parameter.annotation)
    if name != "Machinera":
        assert documented.return_annotation == actual.return_annotation


@pytest.mark.parametrize(
    "name", ["__init__", "transcribe_file", "transcribe_url", "get_job", "resume"]
)
def test_async_signature_parity(name: str) -> None:
    sync = inspect.signature(getattr(m.Machinera, name))
    asynchronous = inspect.signature(getattr(m.AsyncMachinera, name))
    parameters = dict(sync.parameters)
    if name == "__init__":
        parameters.pop("cancel_on_interrupt")
        for key in ("http_client", "transport", "sleeper"):
            parameter = parameters[key]
            annotation = (
                str(parameter.annotation)
                .replace("httpx.Client", "httpx.AsyncClient")
                .replace("httpx.BaseTransport", "httpx.AsyncBaseTransport")
                .replace("Callable[[float], None]", "Callable[[float], Awaitable[None]]")
            )
            parameters[key] = parameter.replace(
                annotation=annotation,
                default=asyncio.sleep if key == "sleeper" else parameter.default,
            )
            substitutions = section(REFERENCE, "### `AsyncMachinera`")
            declaration = re.search(rf"- `({key}: .*?)`", substitutions)
            assert declaration is not None
            namespace: dict[str, Any] = {"asyncio": asyncio}
            exec(
                "from __future__ import annotations\ndef f(*, " + declaration[1] + "): pass",
                namespace,
            )
            documented = inspect.signature(namespace["f"]).parameters[key]
            assert documented.default == parameters[key].default
            assert normalized_annotation(documented.annotation) == normalized_annotation(annotation)
    assert asynchronous == sync.replace(parameters=parameters.values())


def test_result_signature_fields_and_defaults() -> None:
    documented = documented_signature("TranscriptionResult")
    assert set(documented.parameters) == set(m.TranscriptionResult.model_fields)
    for name, field in m.TranscriptionResult.model_fields.items():
        parameter = documented.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default == (
            inspect.Parameter.empty if field.is_required() else field.default
        )


def test_recovery_docs_and_docstring_destinations() -> None:
    assert "Storage 401/403" not in REFERENCE
    assert "[expired-grant refresh rule](#file-upload-recovery-and-expiry)" in REFERENCE
    recovery = " ".join(section(REFERENCE, "##### File upload recovery and expiry").split())
    assert "403 after grant expiry" in recovery
    assert "initialization before retrying PUT" in recovery
    assert "Other storage 403 responses remain terminal" in recovery
    for value in [*(getattr(m, name) for name in m.__all__), _core.Core._poll]:
        doc = inspect.getdoc(value) or ""
        for filename, anchor in re.findall(r"\b([\w/]+\.md)#([\w-]+)", doc):
            text = (ROOT / filename).read_text()
            headings = re.findall(r"^#+ (.+)$", text, re.M)
            assert anchor in {
                re.sub(r"[^\w -]", "", heading).lower().replace(" ", "-") for heading in headings
            }
    assert "api.md#file-upload-recovery-and-expiry" in (m.UploadError.__doc__ or "")
    assert "api.md#retrypolicy" in (_core.Core._poll.__doc__ or "")


def test_readme_limit_summaries_cite_defaults_without_numbers() -> None:
    for start, end in [
        ("- **Direct audio URL:**", "\n## "),
        ("## How requests are routed", "\n## "),
        ("## Long files", "\n- "),
    ]:
        summary = README.split(start, 1)[1].split(end, 1)[0]
        assert "api.md#defaults)" in summary
        assert not re.search(r"\b\d{1,3}(?:,\d{3})+\b", summary)
