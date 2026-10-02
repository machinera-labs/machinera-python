from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from test_client import CREDENTIAL, MODEL, Clock, client, completed, result

from machinera import Machinera

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def example_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ModuleType, Callable[[httpx.Request], httpx.Response], Clock | None], None]:
    monkeypatch.setenv("MACHINERA_API_KEY", CREDENTIAL)

    def install(
        module: ModuleType,
        handler: Callable[[httpx.Request], httpx.Response],
        clock: Clock | None = None,
    ) -> None:
        def factory(**kwargs: object) -> Machinera:
            assert kwargs == {}
            return client(handler, clock)

        monkeypatch.setattr(module, "Machinera", factory)

    return install


@pytest.mark.parametrize(
    "name",
    [
        "transcribe_file",
        "transcribe_url",
        "transcribe_large_file",
        "submit_and_resume",
        "async_transcribe_file",
        "async_submit_and_resume",
        "handle_errors",
    ],
)
def test_import_without_credentials_or_side_effects(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MACHINERA_API_KEY", raising=False)
    assert callable(load_example(name).main)


@pytest.mark.parametrize("name", ["transcribe_file", "handle_errors"])
@pytest.mark.parametrize("suffix", ["wav", "flac", "mp3"])
@pytest.mark.parametrize("text", ["  exact\ntext  ", ""])
def test_file_examples(
    name: str,
    suffix: str,
    text: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    example_client: Callable[..., None],
) -> None:
    module = load_example(name)
    source = tmp_path / f"recording.{suffix}"
    source.write_bytes(b"unchanged-audio")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"unchanged-audio" in request.content
        assert f".{suffix}".encode() in request.content
        assert MODEL.encode() in request.content
        return httpx.Response(200, json=result(text))

    example_client(module, handler)
    assert module.main([str(source)]) == 0
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert captured.out == text
    assert captured.err == ""


@pytest.mark.parametrize("text", ["  exact\ntext  ", ""])
def test_url_example(
    text: str, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("transcribe_url")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            assert request.url.path == "/v1/transcription_jobs"
            assert json.loads(request.content)["url"] == "https://audio.example/recording.wav"
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed(text)

    example_client(module, handler)
    assert module.main(["https://audio.example/recording.wav"]) == 0
    assert [request.method for request in calls] == ["POST", "GET"]
    assert capsys.readouterr().out == text


@pytest.mark.parametrize("interrupted", [False, True])
def test_save_job_and_resume_without_resubmission(
    interrupted: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    example_client: Callable[..., None],
) -> None:
    module = load_example("submit_and_resume")
    state_path = tmp_path / "recovery.json"
    calls: list[httpx.Request] = []
    ready = False

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            state = json.loads(state_path.read_text())
            assert state["operation_key"] == request.headers["idempotency-key"]
            assert state["job_id"] is None
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if ready:
            return completed()
        if interrupted:
            raise KeyboardInterrupt
        return httpx.Response(200, json={"id": "job-1", "status": "processing"})

    example_client(module, handler, Clock())
    args = ["--state", str(state_path)]
    assert module.main([*args, "submit", "https://audio.example/recording.wav"]) == 0
    assert json.loads(state_path.read_text())["job_id"] == "job-1"
    if os.name == "posix":
        assert state_path.stat().st_mode & 0o777 == 0o600
    assert "https://" not in state_path.read_text()
    assert capsys.readouterr().out == ""
    assert module.main([*args, "submit", "https://audio.example/recording.wav"]) == 1
    capsys.readouterr()
    ready = True
    resumed = load_example("submit_and_resume")
    example_client(resumed, handler, Clock())
    assert resumed.main([*args, "resume"]) == 0
    assert capsys.readouterr().out == result()["text"]
    assert sum(request.method == "POST" for request in calls) == 1


def test_replay_saved_key_before_id_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("submit_and_resume")
    state_path = tmp_path / "recovery.json"
    keys: list[str] = []
    ready = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            keys.append(request.headers["idempotency-key"])
            if not ready:
                return httpx.Response(503, headers={"Retry-After": "10"})
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return completed("")

    example_client(module, handler, Clock())
    args = ["--state", str(state_path)]
    submit_args = [*args, "submit", "https://audio.example/recording.wav"]
    assert module.main(submit_args) == 0
    assert json.loads(state_path.read_text())["job_id"] is None
    assert module.main([*args, "resume"]) == 2
    ready = True
    assert module.main(submit_args) == 0
    assert len(keys) == 2 and keys[0] == keys[1]
    assert json.loads(state_path.read_text())["job_id"] == "job-1"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "API key"),
        (403, "permissions"),
        (400, "Correct the input"),
        (422, "Correct the input"),
        (429, "Safe retries"),
    ],
)
def test_error_example_safe_messages(
    status: int,
    expected: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    example_client: Callable[..., None],
) -> None:
    module = load_example("handle_errors")
    source = tmp_path / "recording.wav"
    source.write_bytes(b"private-audio")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "private-server-message", "retryable": status == 429}},
        )

    example_client(module, handler, Clock())
    assert module.main([str(source)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    for sensitive in (CREDENTIAL, str(source), "private-audio", "private-server-message"):
        assert sensitive not in captured.err
    assert len(calls) == (3 if status == 429 else 1)


def test_ambiguous_submission_is_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("handle_errors")
    source = tmp_path / "recording.wav"
    source.write_bytes(b"audio")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("private-connection-details")

    example_client(module, handler)
    assert module.main([str(source)]) == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "Reconcile" in captured.err
    assert "private-connection-details" not in captured.err


def test_terminal_failure_retains_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("submit_and_resume")
    path = tmp_path / "recovery.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return httpx.Response(
            200,
            json={"id": "job-1", "status": "error", "error": {"message": "private-details"}},
        )

    example_client(module, handler)
    assert module.main(["--state", str(path), "submit", "https://audio.example/recording.wav"]) == 1
    assert json.loads(path.read_text())["job_id"] == "job-1"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-details" not in captured.err


def test_error_example_handles_local_input_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("handle_errors")
    source = tmp_path / "recording.txt"
    source.write_bytes(b"private-unrecognized-content")

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input must be rejected before HTTP")

    example_client(module, handler)
    assert module.main([str(source)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Correct the local configuration or input before retrying.\n"


def test_large_file_example_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_uploads import AUDIO, Service

    from machinera import Limits

    monkeypatch.setenv("MACHINERA_API_KEY", CREDENTIAL)
    module = load_example("transcribe_large_file")
    source = tmp_path / "recording.flac"
    source.write_bytes(AUDIO)
    service = Service()
    ready = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and not ready:
            return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
        return service(request)

    def factory() -> Machinera:
        return client(handler, limits=Limits(1, 2))

    monkeypatch.setattr(module, "Machinera", factory)
    args = [str(source), "--operation-key", "saved-key"]
    assert module.main(args) == 1
    recovery = json.loads(capsys.readouterr().err)
    assert recovery == {
        "operation_key": "saved-key",
        "upload_id": "upload-1",
        "phase": "upload_put",
        "job_id": None,
    }
    ready = True
    assert module.main([*args, "--resume", "--upload-id", recovery["upload_id"]]) == 0
    assert capsys.readouterr().out == result()["text"]
    source.unlink()
    before = len(service.calls)
    assert module.main([*args, "--resume", "--job-id", "job-1"]) == 0
    assert all(request.method == "GET" for request in service.calls[before:])


def test_saved_job_status_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("submit_and_resume")
    state_path = tmp_path / "recovery.json"
    module.save_state(state_path, {"operation_key": "saved-key", "job_id": "job-1"})
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return completed("")

    example_client(module, handler)
    assert module.main(["--state", str(state_path), "status"]) == 0
    assert capsys.readouterr().out == "completed\n"
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.asyncio
async def test_async_file_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_async_client import client as async_client

    monkeypatch.setenv("MACHINERA_API_KEY", CREDENTIAL)
    module = load_example("async_transcribe_file")
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audio")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=result())

    monkeypatch.setattr(module, "AsyncMachinera", lambda: async_client(handler))
    assert await module.main([str(source)]) == 0
    assert capsys.readouterr().out == result()["text"]


@pytest.mark.asyncio
async def test_async_saved_job_recovery_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_async_client import client as async_client

    monkeypatch.setenv("MACHINERA_API_KEY", CREDENTIAL)
    module = load_example("async_submit_and_resume")
    state = tmp_path / "state.json"
    ready = False
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            assert (
                json.loads(state.read_text())["operation_key"] == request.headers["idempotency-key"]
            )
            return httpx.Response(202, json={"id": "job-1"})
        if ready:
            return completed()
        return httpx.Response(200, json={"id": "job-1", "status": "processing"})

    monkeypatch.setattr(module, "AsyncMachinera", lambda: async_client(handler, Clock()))
    args = ["--state", str(state)]
    assert await module.main([*args, "submit", "https://audio.example/clip.wav"]) == 0
    assert json.loads(state.read_text())["job_id"] == "job-1"
    capsys.readouterr()
    ready = True
    assert await module.main([*args, "resume"]) == 0
    assert capsys.readouterr().out == result()["text"]
    assert calls.count("POST") == 1


@pytest.mark.parametrize("command", ["status", "resume"])
def test_missing_state_file_means_no_job_id(
    command: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    example_client: Callable[..., None],
) -> None:
    module = load_example("submit_and_resume")
    example_client(module, lambda request: pytest.fail("unexpected HTTP"))
    assert module.main(["--state", str(tmp_path / "missing.json"), command]) == 2
    assert "No job ID saved" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["status", "resume"])
async def test_async_missing_state_file_means_no_job_id(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from test_async_client import client as async_client

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("unexpected HTTP")

    module = load_example("async_submit_and_resume")
    monkeypatch.setattr(module, "AsyncMachinera", lambda: async_client(handler))
    assert await module.main(["--state", str(tmp_path / "missing.json"), command]) == 2
    assert "No job ID saved" in capsys.readouterr().err


def test_error_example_reports_ambiguous_interrupt() -> None:
    from machinera import TranscriptionInterrupted

    module = load_example("handle_errors")
    ambiguous = module.recovery_guidance(TranscriptionInterrupted("x", ambiguous=True))
    assert ambiguous.startswith("Execution may have started")
    assert not module.recovery_guidance(TranscriptionInterrupted("x")).startswith("Execution")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"text": 7}), "Reconcile"),
        (httpx.Response(202, json={"status": "queued"}), "operation key"),
    ],
)
def test_error_example_response_validation_guidance(
    response: httpx.Response,
    expected: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    example_client: Callable[..., None],
) -> None:
    module = load_example("handle_errors")
    source = tmp_path / "recording.wav"
    source.write_bytes(b"audio")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            return response if response.status_code == 200 else httpx.Response(413)
        return response

    example_client(module, handler)
    assert module.main([str(source)]) == 1
    assert expected in capsys.readouterr().err
    assert len(calls) == (1 if response.status_code == 200 else 2)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("NotFoundError", "cannot be found or replayed"),
        ("ConflictError", "cannot be found or replayed"),
        ("DeadlineExceededError", "resume that job"),
        ("InternalServerError", "resume that job"),
    ],
)
def test_error_example_known_job_guidance(error: str, expected: str) -> None:
    import machinera

    module = load_example("handle_errors")
    failure = getattr(machinera, error)("x", job_id="job-1", phase="poll")
    assert expected in module.recovery_guidance(failure)


def test_error_example_does_not_resume_missing_admitted_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], example_client: Callable[..., None]
) -> None:
    module = load_example("handle_errors")
    source = tmp_path / "recording.wav"
    source.write_bytes(b"audio")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(413)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        return httpx.Response(404, json={"error": {"code": "job_not_found"}})

    example_client(module, handler)
    assert module.main([str(source)]) == 1
    err = capsys.readouterr().err
    assert "cannot be found or replayed" in err
    assert "resume" not in err
