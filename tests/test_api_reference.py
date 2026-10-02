import inspect
from pathlib import Path

import httpx
import pytest

import machinera


def test_every_export_has_reference_entry() -> None:
    reference = (Path(__file__).resolve().parents[1] / "api.md").read_text(encoding="utf-8")
    for name in machinera.__all__:
        assert f"### `{name}`" in reference or f"| `{name}` |" in reference, name


def test_upload_recovery_surface_is_documented() -> None:
    reference = (Path(__file__).resolve().parents[1] / "api.md").read_text(encoding="utf-8")
    assert "UploadPhase" in machinera.__all__
    for name in ("operation_key", "upload_id", "phase", "job_id", "storage_code"):
        assert hasattr(machinera.UploadError("failure"), name)
        assert f"`{name}`" in reference


def test_response_models_are_exported() -> None:
    from pydantic import BaseModel

    for name in ("TranscriptionResult", "JobSnapshot", "JobError", "TranscriptionWord"):
        assert name in machinera.__all__
        assert issubclass(getattr(machinera, name), BaseModel)


def test_both_clients_are_exported() -> None:
    for name in ("Machinera", "AsyncMachinera"):
        assert name in machinera.__all__
        assert callable(getattr(machinera, name))


def test_job_transport_and_resume_signatures() -> None:
    assert "job_inline_body_bytes" in inspect.signature(machinera.Limits).parameters
    for cls in (machinera.Machinera, machinera.AsyncMachinera):
        assert "phase" not in inspect.signature(cls.resume).parameters
        with pytest.raises(ValueError, match="auto, job"):
            cls(api_key="key", transport="async")  # type: ignore[arg-type]
    with httpx.Client() as http:
        machinera.Machinera(api_key="key", transport="job", http_client=http).close()


def test_interrupted_constructor_forwards_context() -> None:
    error = machinera.TranscriptionInterrupted("stopped", ambiguous=True, job_id="job-1")
    assert error.ambiguous is True and error.job_id == "job-1" and error.message == "stopped"
    assert machinera.TranscriptionInterrupted("stopped").ambiguous is False
