from __future__ import annotations

import io
import types

import httpx
import pytest
from support import Clock, client
from test_documented_defaults import README, WAV, code_block, readme_provider
from test_upload_expiry import ExpiringUploads

import machinera as m


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("expired_initialization", [False, True])
@pytest.mark.parametrize("recipe", ["provider", "loop"])
def test_documented_outer_retry_recovers_lost_replacement(
    asynchronous: bool, expired_initialization: bool, recipe: str
) -> None:
    clock = Clock()
    service = ExpiringUploads(clock)
    lost = True
    replay_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads") and service.accepted:
            replay_keys.append(request.headers["idempotency-key"])
            if expired_initialization:
                return httpx.Response(410, json={"error": {"code": 1003, "retryable": False}})
        answer = service(request)
        if request.url.path.endswith("/transcription_jobs") and answer.status_code == 202 and lost:
            raise httpx.ReadError("accepted response lost")
        return answer

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        if recipe == "provider":
            provider = readme_provider(handler, clock)
            provider.client = sdk
            with pytest.raises(m.APIConnectionError) as caught:
                provider.transcribe(WAV)
            error = caught.value
            assert error.operation_key == next(iter(service.accepted))
            assert error.upload_id == "upload-2" and error.job_id is None
            assert error.is_transient is False
            lost = False
            assert "exact" in provider.transcribe(WAV)
        else:
            namespace = {"open": lambda *args: io.BytesIO(WAV)}
            block = code_block(README, "def transcribe(client: Machinera")
            exec(block[: block.index("\n\nwith Machinera(")], namespace)

            def pause(seconds: float) -> None:
                nonlocal lost
                lost = False
                clock.sleep(seconds)

            namespace["time"] = types.SimpleNamespace(sleep=pause)

            class FileClient:
                def transcribe_file(self, path: str, **options: object) -> object:
                    assert path == "recording.wav"
                    return sdk.transcribe_file(WAV, filename=path, **options)

                def resume(self, **options: object) -> object:
                    assert options.pop("file") == "recording.wav"
                    return sdk.resume(file=WAV, filename="recording.wav", **options)

            assert "exact" in namespace["transcribe"](FileClient(), "sample", "recording.wav")
    assert len(service.uploads) == len(service.puts) == 2
    assert len(service.accepted) == 1
    assert replay_keys
    assert service.submits[-1] == service.submits[-2]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_rotated_key_deadline_requires_saved_context(asynchronous: bool) -> None:
    clock = Clock()
    service = ExpiringUploads(clock)

    def handler(request: httpx.Request) -> httpx.Response:
        answer = service(request)
        if request.method == "GET":
            clock.now = 100
        return answer

    with client(handler, clock, asynchronous=asynchronous, limits=m.Limits(1, 2)) as sdk:
        with pytest.raises(m.DeadlineExceededError) as caught:
            sdk.transcribe_file(
                WAV, model="transcribe-v1", idempotency_key="saved-key", deadline=10
            )
    assert caught.value.operation_key != "saved-key"
    assert caught.value.job_id == "job-1"
    assert caught.value.is_transient is False
