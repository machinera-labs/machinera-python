import dataclasses
import hashlib
import inspect
import io
import json
import re
import threading
import time
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
import support
from support import Clock, Service, accepted, completed, failed_job, queued, unavailable

import machinera
from machinera._contract import (
    DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S,
    DEFAULT_MULTIPART_CAP_BYTES,
    DEFAULT_RESULT_RETENTION_S,
    ERROR_CODES,
    SYNC_ACCEPTANCE_AMBIGUOUS_CODES,
)
from machinera._exceptions import SYNC_FALLBACK_CODES, SYNC_REPLAYABLE_CODES
from machinera._files import _MIME_SUFFIXES, _RESERVED
from machinera._io import _CANCEL_INTERVAL

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
REFERENCE = (ROOT / "api.md").read_text(encoding="utf-8")
PUBLIC_DOCS = (
    "README.md",
    "api.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "examples/README.md",
)
ROW = re.compile(r"^\| `([A-Za-z_.]+)` \| `([^`]+)` \|", re.MULTILINE)


def expected_defaults() -> dict[str, str]:
    values: dict[str, object] = {}
    for cls in (machinera.TimeoutPolicy, machinera.RetryPolicy, machinera.Limits):
        for field in dataclasses.fields(cls):
            values[f"{cls.__name__}.{field.name}"] = field.default
    return {name: str(value) for name, value in values.items()}


def table(text: str, header: str) -> str:
    start = text.index(header)
    assert text.count(header) == 1, header
    return text[start : text.index("\n\n", start)]


def documented(text: str) -> dict[str, str]:
    rows = ROW.findall(table(text, "| Setting | Default | Meaning |"))
    return {name: value.replace(",", "") for name, value in rows}


def test_documented_defaults_match_the_sdk() -> None:
    assert documented(REFERENCE) == expected_defaults()


def test_readme_links_to_authoritative_defaults() -> None:
    section = README.split("## Deadlines and retries\n", 1)[1].split("\n## ", 1)[0]
    assert "api.md#defaults)" in section
    assert "| Setting | Default | Meaning |" not in README


def test_retention_defaults_are_reexported_from_the_contract() -> None:
    assert machinera.DEFAULT_RESULT_RETENTION_S is machinera._contract.DEFAULT_RESULT_RETENTION_S
    assert (
        machinera.DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S
        is machinera._contract.DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S
    )
    assert {"DEFAULT_RESULT_RETENTION_S", "DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S"} <= set(
        machinera.__all__
    )


def test_readme_retention_cites_constants_without_copied_durations() -> None:
    recovery = README.split("## Recovery after a restart\n", 1)[1].split("\n## ", 1)[0]
    assert "`machinera.DEFAULT_RESULT_RETENTION_S`" in recovery
    assert "`machinera.DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S`" in recovery
    assert not re.search(
        r"\b(?:\d+(?:[.,]\d+)*|(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten)\b)"
        r"[\s-]*(?:days?|hours?|seconds?|d|h|s)\b",
        recovery,
        re.IGNORECASE,
    )


def test_retention_constants_availability_cites_changelog() -> None:
    symbols = ("DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S", "DEFAULT_RESULT_RETENTION_S")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    added_sections = re.findall(r"(?ms)^### Added\n(.*?)(?=^#{1,3} |\Z)", changelog)
    matches = [section for section in added_sections if all(name in section for name in symbols)]
    assert len(matches) == 1, "one Added section must document both retention constants"

    recovery = README.split("- Recovery is limited", 1)[1].split("\n- ", 1)[0]
    changelog_url = "https://github.com/machinera-labs/machinera-python/blob/main/CHANGELOG.md"
    sections = [(recovery, changelog_url)]
    for symbol in symbols:
        section = REFERENCE.split(f"### `{symbol}`\n", 1)[1].split("\n##", 1)[0]
        sections.append((section, "CHANGELOG.md"))
    for section, target in sections:
        assert "availability" in section.lower()
        assert re.search(rf"\[[^\]]+\]\({re.escape(target)}\)", section)
        assert not re.search(r"\bunreleased\b|\bv?\d+\.\d+(?:\.\d+)?\b", section, re.IGNORECASE)


def test_readme_long_jobs_qualifies_collection_with_retention() -> None:
    section = README.split("- **Long jobs:**", 1)[1].split("\n- **", 1)[0]
    assert "to collect it while the [replay binding and result retention]" in section
    assert "api.md#retention-defaults) hold" in section


@pytest.mark.parametrize(
    ("section", "start", "end"),
    [
        ("- **Billed once:**", "are guaranteed:", "Include any time"),
        ("- Recovery is limited", "For accepted jobs, size", "Recovery does not extend retention."),
    ],
)
def test_retention_recovery_capture_scopes_accepted_jobs_and_cites_upload_expiry(
    section: str, start: str, end: str
) -> None:
    text = README.split(section, 1)[1].split("\n- ", 1)[0]
    capture = " ".join((start + text.split(start, 1)[1].split(end, 1)[0]).split())
    assert "whole retry sequence" in capture
    assert "accepted job" in capture
    assert "work not yet submitted instead follows" in capture
    target = (
        "https://github.com/machinera-labs/machinera-python/blob/main/"
        "api.md#file-upload-recovery-and-expiry"
    )
    assert f"[file upload recovery and expiry]({target})" in capture


def test_grouped_numbers_in_docs_are_documented_defaults() -> None:
    allowed = {
        *expected_defaults().values(),
        str(DEFAULT_MULTIPART_CAP_BYTES),
        str(DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S),
        str(DEFAULT_RESULT_RETENTION_S),
    }
    for text in (README, REFERENCE):
        for number in re.findall(r"\b\d{1,3}(?:,\d{3})+\b", text):
            assert number.replace(",", "") in allowed, number


def failure_table(text: str) -> str:
    return table(text, "| # | Exception | Action |")


def test_readme_links_to_authoritative_failure_handling() -> None:
    section = README.split("## Failure handling\n", 1)[1].split("\n## ", 1)[0]
    assert "api.md#failure-handling)" in section
    assert "| # | Exception | Action |" not in README
    assert failure_table(REFERENCE).count("\n| ") == 8


def test_lost_submission_row_precedes_the_rows_it_overrides() -> None:
    rows = table(REFERENCE, "| Error | `is_transient` |").splitlines()[2:]
    lost = [i for i, row in enumerate(rows) if "response lost" in row]
    # First-match order: after the interrupt row, before the rows it overrides.
    assert lost == [1], lost
    assert '`"job_submit"` or `"submit"`' in rows[1]
    assert "idempotency_key=error.operation_key" in rows[1]
    recovery = failure_table(REFERENCE).splitlines()[2:][4]
    assert recovery.startswith("| 5 |") and '`"job_submit"` or `"submit"`' in recovery
    # Only a replayable error can be a lost submission; retryable=False stays in row 7.
    assert "`APIConnectionError` or status error with `retryable` `True`" in recovery
    for text in (README, REFERENCE):
        assert "always transient" not in text and "Retried, and transient" not in text


def test_readme_lists_the_supported_suffixes() -> None:
    line = re.search(r"`SUPPORTED_MEDIA_SUFFIXES` \(([^)]*)\)", README)
    assert line is not None
    listed = set(re.findall(r"`([a-z0-9]+)`", line.group(1)))
    assert listed == machinera.SUPPORTED_MEDIA_SUFFIXES


def test_job_error_codes_match_the_contract() -> None:
    codes = table(REFERENCE, "| Code | Retryable | Meaning |")
    rows = re.findall(r"^\| `([0-9]+)` \| `(True|False)` \|", codes, re.MULTILINE)
    job_codes = {code for code, entry in ERROR_CODES.items() if entry.status == 200}
    assert {int(code) for code, _ in rows} == job_codes
    for code, retryable in rows:
        assert str(ERROR_CODES[int(code)].retryable) == retryable
    assert (
        "[job-code table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#joberror)"
        in README
    )


class PermanentError(Exception):
    pass


Handler = Callable[[httpx.Request], httpx.Response]


def code_block(text: str, marker: str) -> str:
    start = text.rindex("```python\n", 0, text.index(marker)) + len("```python\n")
    return text[start : text.index("\n```", start)]


def readme_provider(
    handler: Handler,
    clock: Clock | None = None,
    key_seconds: float = 0.0,
    open_file: Callable[..., Any] = open,
) -> Any:
    """The README provider with a fake clock and transport; open_file sees its own reads."""
    clock = clock or Clock()
    namespace: dict[str, object] = {"PermanentError": PermanentError}
    exec(code_block(README, "class MachineraProvider"), namespace)
    namespace["time"] = types.SimpleNamespace(monotonic=clock.monotonic)
    namespace["open"] = open_file
    provider_class = namespace["MachineraProvider"]
    assert isinstance(provider_class, type)
    provider = provider_class.__new__(provider_class)
    provider.digests = {}
    provider.hashing = {}
    provider.jobs = {}
    provider.uploads = {}
    derive = provider.operation_key

    def slow_key(audio: str | bytes, language: str) -> object:
        clock.sleep(key_seconds)  # a slow read while hashing a path
        return derive(audio, language)

    provider.operation_key = slow_key
    provider.client = machinera.Machinera(
        api_key="key",
        transport=httpx.MockTransport(handler),
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    return provider


class ChunkedFile(io.BytesIO):
    """A file that records each read size, to prove a recipe never reads it whole."""

    def __init__(self, content: bytes, sizes: list[int | None]) -> None:
        super().__init__(content)
        self.sizes = sizes

    def read(self, size: int | None = -1) -> bytes:
        self.sizes.append(size)
        return super().read(size)


def chunked_open(content: bytes, sizes: list[int | None]) -> Callable[..., ChunkedFile]:
    def open_file(path: str, mode: str = "r") -> ChunkedFile:
        assert mode == "rb"
        return ChunkedFile(content, sizes)

    return open_file


def assert_bounded_reads(sizes: list[int | None]) -> None:
    assert sizes, "the recipe must read the file"
    assert all(isinstance(size, int) and 0 < size <= 1 << 20 for size in sizes), sizes


def readme_operation_key(
    audio: str | bytes, language: str, open_file: Callable[..., Any] = open
) -> str:
    provider = readme_provider(lambda request: accepted(), open_file=open_file)
    return str(provider.operation_key(audio, language))


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(64)
ATTEMPTS = machinera.RetryPolicy().max_attempts
HARNESS_ATTEMPTS = 10
HARNESS_PAUSE = 1.0


class Harness:
    """Retry every exception except PermanentError after a pause, as the harness does."""

    def __init__(self, provider: object, clock: Clock) -> None:
        self.provider = provider
        self.clock = clock
        self.attempts: list[float] = []

    def run(self, audio: str | bytes = WAV, attempts: int = HARNESS_ATTEMPTS) -> str:
        for attempt in range(attempts):
            started = self.clock.now
            try:
                return self.provider.transcribe(audio)  # type: ignore[attr-defined,no-any-return]
            except PermanentError:
                raise
            except Exception:
                if attempt == attempts - 1:
                    raise
            finally:
                self.attempts.append(self.clock.now - started)
            self.clock.sleep(HARNESS_PAUSE)
        raise AssertionError("unreachable")


Answer = Callable[[int], httpx.Response]


def job_service(requests: list[httpx.Request], submit: Answer, poll: Answer) -> Handler:
    """Answer the nth job submission with submit(n) and the nth status read with poll(n)."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path != "/v1/audio/transcriptions", "the recipe never sends unkeyed"
        if request.method == "POST":
            return submit(sum(r.method == "POST" for r in requests))
        return poll(sum(r.method == "GET" for r in requests))

    return handle


def submission_keys(requests: list[httpx.Request]) -> list[str | None]:
    return [r.headers.get("Idempotency-Key") for r in requests if r.method == "POST"]


def test_readme_provider_recipe_classifies_failures() -> None:
    def answer(submit: Answer) -> Handler:
        return job_service([], submit, lambda n: completed("hi"))

    assert readme_provider(answer(lambda n: accepted())).transcribe(WAV) == "hi"  # type: ignore[attr-defined]
    # A bare 500 to a sent keyed submission may hide an accepted job; repeating it is safe.
    for status, permanent in ((429, False), (503, False), (500, False), (400, True)):
        provider = readme_provider(answer(lambda n, s=status: httpx.Response(s, json={})))
        expected = PermanentError if permanent else machinera.APIStatusError
        with pytest.raises(expected):
            provider.transcribe(WAV)  # type: ignore[attr-defined]
    provider = readme_provider(answer(lambda n: accepted()))
    with pytest.raises(PermanentError):
        provider.transcribe(WAV, language="fr")  # type: ignore[attr-defined]
    with pytest.raises(PermanentError):
        provider.transcribe(str(ROOT / "missing-sample.wav"))  # type: ignore[attr-defined]


@pytest.mark.parametrize("accepted", [False, True])
def test_readme_provider_recipe_lets_interrupts_through(accepted: bool) -> None:
    requests: list[httpx.Request] = []
    ready = False

    def interrupt(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and (accepted or ready):
            return support.accepted()
        if ready:
            return completed("hi")
        raise KeyboardInterrupt

    provider = readme_provider(interrupt)
    key = provider.operation_key(WAV, "en")
    with pytest.raises(KeyboardInterrupt) as interrupted:
        provider.transcribe(WAV)
    # A harness that retries every Exception must not catch it.
    assert not isinstance(interrupted.value, Exception)
    cause = interrupted.value.__cause__
    assert isinstance(cause, machinera.TranscriptionInterrupted)
    assert cause.operation_key == key
    assert provider.jobs == ({key: "job-1"} if accepted else {})
    before = len(requests)
    ready = True
    assert provider.transcribe(WAV) == "hi"
    assert len(set(submission_keys(requests))) == 1
    if accepted:
        assert [request.method for request in requests[before:]] == ["GET"]


def lost_response(n: int) -> httpx.Response:
    if n <= ATTEMPTS:
        raise httpx.ReadError("response lost after the body was sent")
    return accepted()


@pytest.mark.parametrize(
    ("submit", "poll"),
    [
        pytest.param(lost_response, lambda n: completed("hi"), id="lost-submission-response"),
        pytest.param(
            lambda n: accepted(),
            lambda n: unavailable() if n <= ATTEMPTS else completed("hi"),
            id="status-reads-fail-after-acceptance",
        ),
        pytest.param(
            lambda n: unavailable() if n <= ATTEMPTS else accepted(),
            lambda n: completed("hi"),
            id="submission-refused",
        ),
    ],
)
def test_readme_provider_recipe_retries_replay_the_same_job(submit: Answer, poll: Answer) -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    harness = Harness(readme_provider(job_service(requests, submit, poll), clock), clock)
    assert harness.run() == "hi"
    # The first attempt failed transiently. The harness's retry either carried the same key,
    # so the service replays the job it may already have accepted, or resumed the job.
    assert len(harness.attempts) == 2
    keys = submission_keys(requests)
    assert len(set(keys)) == 1 and keys[0] is not None


def test_unsent_unkeyed_job_submission_is_repeated_as_made() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        refusal = support.refused_sync(request)
        if refusal is not None:
            return refusal
        if request.method == "GET":
            return completed("hi")
        posts.append(request)
        if len(posts) <= ATTEMPTS:
            raise httpx.ConnectError("unreachable before anything was sent")
        return accepted()

    with support.client(handler) as sdk:
        with pytest.raises(machinera.APIConnectionError) as caught:
            support.submit(sdk, "file", None)
        error = caught.value
        assert (error.phase, error.job_id, error.retryable) == ("job_submit", None, True)
        # Nothing was sent, so no job can exist: row 5 repeats the call as made.
        assert error.is_transient is True
        assert support.submit(sdk, "file", None).text == "hi"
    assert len(posts) == ATTEMPTS + 1
    row = failure_table(REFERENCE).splitlines()[2:][4]
    assert row.startswith("| 5 |") and "when `is_transient` is `True`" in row
    assert "is `True` exactly where" not in row


@pytest.mark.parametrize(
    "poll",
    [
        pytest.param(lambda n: failed_job(5006, True), id="retryable-failed-job"),
        pytest.param(lambda n: failed_job(5011, False), id="failed-job"),
        pytest.param(
            lambda n: unavailable() if n <= ATTEMPTS else failed_job(5006, True),
            id="failed-job-after-transient-reads",
        ),
        pytest.param(lambda n: httpx.Response(404, json={}), id="job-not-found"),
    ],
)
def test_readme_provider_recipe_fails_a_non_transient_error_after_acceptance(poll: Answer) -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    service = job_service(requests, lambda n: accepted(), poll)
    harness = Harness(readme_provider(service, clock), clock)
    with pytest.raises(PermanentError):
        harness.run()
    assert len(submission_keys(requests)) == 1 and len(harness.attempts) <= 2


@pytest.mark.parametrize("key_seconds,attempts", [(0, 3), (40, 2)])
def test_readme_provider_recipe_bounds_each_attempt_and_pauses_between_them(
    key_seconds: int, attempts: int
) -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    service = job_service(requests, lambda n: accepted(), lambda n: queued())
    harness = Harness(readme_provider(service, clock, key_seconds=key_seconds), clock)
    with pytest.raises(machinera.DeadlineExceededError) as caught:
        harness.run(attempts=attempts)
    # A job still running at the deadline is transient: each retry polls the same job.
    assert caught.value.is_transient is True and caught.value.job_id == "job-1"
    assert len(harness.attempts) == attempts
    assert all(290 <= seconds <= 300 for seconds in harness.attempts)
    assert clock.now == pytest.approx(sum(harness.attempts) + (attempts - 1) * HARNESS_PAUSE)
    assert len(set(submission_keys(requests))) == 1


def test_readme_provider_recipe_resumes_a_seen_job_instead_of_submitting() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    polls = lambda n: queued() if n <= 400 else completed("hi")  # noqa: E731
    service = job_service(requests, lambda n: accepted(), polls)
    harness = Harness(readme_provider(service, clock), clock)
    assert harness.run() == "hi"
    assert len(harness.attempts) > 1
    assert len(submission_keys(requests)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: httpx.Response(500, json={}), id="bare-500"),
        pytest.param(lambda: httpx.Response(200, content=b"not json"), id="malformed-body"),
    ],
)
@pytest.mark.parametrize("recovers", [True, False])
def test_readme_provider_recipe_keeps_resuming_after_a_failed_status_read(
    recovers: bool, failure: Callable[[], httpx.Response]
) -> None:
    requests: list[httpx.Request] = []
    clock = Clock()

    def poll(n: int) -> httpx.Response:
        return completed("hi") if recovers and n > 2 else failure()

    service = job_service(requests, lambda n: accepted(), poll)
    provider = readme_provider(service, clock)
    harness = Harness(provider, clock)
    if recovers:
        assert harness.run() == "hi"
        assert len(harness.attempts) == 3
    else:
        with pytest.raises(machinera.APIError) as caught:
            harness.run()
        # Each failure is a non-transient status read ("poll") on the accepted job.
        assert caught.value.phase == "poll" and caught.value.job_id == "job-1"
        assert caught.value.is_transient is False
        assert len(harness.attempts) == HARNESS_ATTEMPTS
    # Row 3: the job may have finished and been billed, so a failed status read goes back
    # to the harness, whose retries resume the job; the clip is never submitted again.
    assert len(submission_keys(requests)) == 1
    assert sum(r.method == "GET" for r in requests) == len(harness.attempts)


def test_readme_provider_recipe_resumes_after_a_rate_limited_status_read() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()

    def poll(n: int) -> httpx.Response:
        if n <= ATTEMPTS:
            return httpx.Response(429, json={"error": {"code": 3999}})
        return completed("hi")

    provider = readme_provider(job_service(requests, lambda n: accepted(), poll), clock)
    with pytest.raises(machinera.RateLimitError) as caught:
        provider.transcribe(WAV)
    # A transient 4xx on an accepted job goes back to the harness, not PermanentError.
    assert (caught.value.phase, caught.value.job_id) == ("poll", "job-1")
    assert caught.value.is_transient is True
    assert provider.transcribe(WAV) == "hi"
    assert len(submission_keys(requests)) == 1
    assert sum(r.method == "GET" for r in requests) == ATTEMPTS + 1


def test_readme_provider_recipe_repeats_an_unanswered_submission_under_its_key() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()

    def submit(n: int) -> httpx.Response:
        return httpx.Response(500, json={}) if n <= 3 else accepted()

    service = job_service(requests, submit, lambda n: completed("hi"))
    harness = Harness(readme_provider(service, clock), clock)
    assert harness.run() == "hi"
    # A bare 500 to a sent keyed submission is not transient but may hide an accepted job;
    # the harness's retries repeat it under the same key, bounded by their own limit.
    keys = submission_keys(requests)
    assert len(harness.attempts) == 4 and len(keys) == 4 and len(set(keys)) == 1


def test_reference_states_the_phase_and_scope_of_a_status_read_failure() -> None:
    def poll_fails(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return accepted()
        return httpx.Response(500, json={})

    with machinera.Machinera(api_key="key", transport=httpx.MockTransport(poll_fails)) as client:
        errors = []
        for call in (
            lambda: client.transcribe_file(WAV, model="transcribe-v1", idempotency_key="k"),
            lambda: client.resume("job-1", response_format="json"),
        ):
            with pytest.raises(machinera.InternalServerError) as caught:
                call()
            errors.append(caught.value)
        # Every job status read reports "poll", and resume(job_id) is outside the unkeyed row.
        assert [(e.phase, e.job_id, e.is_transient) for e in errors] == [
            ("poll", "job-1", False)
        ] * 2


def test_reference_phrases_and_changelog_constants() -> None:
    flat = " ".join(REFERENCE.split())
    assert '`"poll"` is the phase of every job status read' in flat
    assert "(not `resume`, whose errors follow the rows below)" in flat
    assert "`language` accepts `None` (the default, which sends no hint)" in flat
    assert "the only thing that ends polling" not in flat
    for name in re.findall(r"`([A-Z][A-Z_]+_BYTES)`", (ROOT / "CHANGELOG.md").read_text()):
        assert f"`{name}`" in REFERENCE, name


def test_readme_provider_recipe_hashes_a_path_in_one_thread(tmp_path: Path) -> None:
    sample = tmp_path / "shared.wav"
    sample.write_bytes(WAV)
    opened: list[str] = []
    start = threading.Barrier(4)

    def slow_open(path: str, mode: str = "r") -> Any:
        opened.append(path)
        time.sleep(0.05)  # keep the first hash running while the others arrive
        return open(path, mode)

    provider = readme_provider(lambda request: accepted(), open_file=slow_open)

    def derive() -> str:
        start.wait()
        return str(provider.operation_key(str(sample), "en"))

    with ThreadPoolExecutor(4) as pool:
        keys = list(pool.map(lambda _: derive(), range(4)))
    assert len(set(keys)) == 1 and opened == [str(sample)]


def test_readme_provider_recipe_shares_one_key_for_identical_bytes(tmp_path: Path) -> None:
    first, second = tmp_path / "first take.WAV", tmp_path / "copy.wav"
    first.write_bytes(WAV)
    second.write_bytes(WAV)
    requests: list[httpx.Request] = []
    provider = readme_provider(
        job_service(requests, lambda n: accepted(), lambda n: completed("hi"))
    )
    for audio in (str(first), str(second), WAV, WAV):
        assert provider.transcribe(audio) == "hi"
    posts = [r for r in requests if r.method == "POST"]
    # Same suffix and bytes: one key and one identical request, so one job and one charge.
    # Raw bytes are named from their content, so they get a key of their own.
    keys = submission_keys(requests)
    assert keys[0] == keys[1] != keys[2] == keys[3]
    assert all(b'filename="upload.wav"' in r.content for r in posts), posts[0].content[:300]
    boundaries = [r.headers["content-type"].split("boundary=")[1].encode() for r in posts]
    bodies = [r.content.replace(b, b"") for r, b in zip(posts, boundaries, strict=True)]
    assert bodies[0] == bodies[1] and bodies[2] == bodies[3]


def test_readme_provider_recipe_keys_cover_file_upload_upload_metadata(tmp_path: Path) -> None:
    # One MP4-container file under two suffixes and as bytes: the suffix sets the media
    # type of a file upload, so each input must get its own key, and repeating an input
    # must reproduce the same upload initialization exactly.
    container = (16).to_bytes(4, "big") + b"ftypisom" + bytes(4) + bytes(64)
    inputs: list[str | bytes] = []
    for name in ("clip.m4a", "clip.mp4", "copy.m4a"):
        (tmp_path / name).write_bytes(container)
        inputs.append(str(tmp_path / name))
    inputs += [container, container]
    service = Service()
    provider = readme_provider(lambda request: accepted())
    with support.client(service, limits=machinera.Limits(1, 2)) as sdk:
        provider.client = sdk
        for audio in inputs:
            assert "exact" in provider.transcribe(audio)
    keys = [r.headers["Idempotency-Key"] for r in service.calls if r.url.path == "/v1/uploads"]
    sent = [init["content_type"] for init in service.initializations]
    assert sent == ["audio/mp4", "video/mp4", "audio/mp4", "video/mp4", "video/mp4"]
    by_key: dict[str, set[str]] = {}
    for key, init in zip(keys, service.initializations, strict=True):
        by_key.setdefault(key, set()).add(json.dumps(init, sort_keys=True))
    assert len(by_key) == 3 and all(len(inits) == 1 for inits in by_key.values())


def test_readme_provider_recipe_hashing_is_outside_any_bound(tmp_path: Path) -> None:
    sample = tmp_path / "slow.wav"
    sample.write_bytes(WAV)
    clock = Clock()

    def slow_open(path: str, mode: str = "r") -> Any:
        clock.sleep(400)  # a disk slower than the whole budget
        return open(path, mode)

    requests: list[httpx.Request] = []
    service = job_service(requests, lambda n: accepted(), lambda n: completed("hi"))
    harness = Harness(readme_provider(service, clock, open_file=slow_open), clock)
    assert harness.run(str(sample)) == "hi"
    # Hashing is not cut short: the first attempt outlasts the budget, sends nothing, and
    # ends with an ordinary exception; the retry reuses the hash and gets a full budget.
    assert harness.attempts[0] == pytest.approx(400) and len(harness.attempts) == 2
    assert harness.attempts[1] <= 300 and len(submission_keys(requests)) == 1


def test_readme_provider_recipe_hashes_a_path_once_across_attempts(tmp_path: Path) -> None:
    sample = tmp_path / "sample.wav"
    sample.write_bytes(WAV)
    opened: list[str] = []

    def counting_open(path: str, mode: str = "r") -> Any:
        opened.append(path)
        return open(path, mode)

    requests: list[httpx.Request] = []
    clock = Clock()
    refused_once = lambda n: unavailable() if n <= ATTEMPTS else accepted()  # noqa: E731
    service = job_service(requests, refused_once, lambda n: completed("hi"))
    harness = Harness(readme_provider(service, clock, open_file=counting_open), clock)
    assert harness.run(str(sample)) == "hi"
    assert len(harness.attempts) == 2 and opened == [str(sample)]


def test_readme_operation_key_identifies_the_request(tmp_path: Path) -> None:
    key = readme_operation_key(WAV, "en")
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert readme_operation_key(WAV, "en") == key
    assert readme_operation_key(WAV, "en-US") != key
    assert readme_operation_key(WAV + b"\0", "en") != key
    for name in ("any-name.wav", "other.WAV", "other.flac"):
        (tmp_path / name).write_bytes(WAV)
    named = readme_operation_key(str(tmp_path / "any-name.wav"), "en")
    # Only the suffix of a path is in the key; raw bytes are keyed apart from paths.
    assert readme_operation_key(str(tmp_path / "other.WAV"), "en") == named != key
    assert readme_operation_key(str(tmp_path / "other.flac"), "en") != named


def test_readme_recipes_hash_a_path_in_bounded_chunks(tmp_path: Path) -> None:
    content = WAV * 40_000  # several chunks
    sample = tmp_path / "clip.wav"
    sample.write_bytes(content)
    sizes: list[int | None] = []
    key = readme_operation_key(str(sample), "en", chunked_open(content, sizes))
    assert_bounded_reads(sizes)
    assert len(sizes) > 2
    (tmp_path / "copy.wav").write_bytes(content)
    assert key == readme_operation_key(str(tmp_path / "copy.wav"), "en")

    sizes.clear()
    block = code_block(README, "def transcribe(client: Machinera")
    namespace: dict[str, object] = {"open": chunked_open(content, sizes)}
    exec(block[: block.index("\n\nwith Machinera(")], namespace)
    keys: list[str] = []

    class Client:
        def transcribe_file(self, path: str, **options: object) -> object:
            keys.append(str(options["idempotency_key"]))
            return type("Result", (), {"text": "hello"})()

    transcribe = namespace["transcribe"]
    assert callable(transcribe)
    assert transcribe(Client(), "sample-0001", "sample-0001.wav") == "hello"
    assert_bounded_reads(sizes)
    digest = hashlib.sha256(content).hexdigest()
    options = "transcribe-v1:en:json"
    expected = hashlib.sha256(f"my-run:sample-0001:{options}:{digest}".encode()).hexdigest()
    assert keys == [expected]


def test_public_docs_never_link_to_source() -> None:
    for name in PUBLIC_DOCS:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"\]\([^)]*\bsrc/", text), name
    for text in (README, REFERENCE, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")):
        assert "src/" not in text
        assert "_contract" not in text
        assert not re.search(r"`_[A-Za-z]\w*`", text)


def listed(text: str, before: str, after: str) -> set[str]:
    start = text.index(before) + len(before)
    return set(re.findall(r"`([^`]+)`", text[start : text.index(after, start)]))


def test_reference_lists_match_the_sdk() -> None:
    headers = listed(REFERENCE, "Headers the SDK owns cannot be overridden:", "matched")
    assert {name.lower() for name in headers} == _RESERVED
    assert listed(REFERENCE, "the recognized types are", "Without a type") == set(_MIME_SUFFIXES)
    replayable = listed(REFERENCE, "proving the request did not run (", ")")
    assert {int(code) for code in replayable} == SYNC_REPLAYABLE_CODES - SYNC_FALLBACK_CODES
    assert {
        int(code) for code in listed(REFERENCE, "exclude the job-fallback refusals (", ")")
    } == SYNC_FALLBACK_CODES
    assert {
        int(code) for code in listed(REFERENCE, "and acceptance-ambiguous codes (", ")")
    } == SYNC_ACCEPTANCE_AMBIGUOUS_CODES


def test_every_export_has_reference_entry() -> None:
    for name in machinera.__all__:
        assert f"### `{name}`" in REFERENCE or f"| `{name}` |" in REFERENCE, name


@pytest.mark.parametrize("status", [400, 404, 409])
def test_readme_recovery_loop_stops_on_first_final_4xx(status: int, tmp_path: Path) -> None:
    sample = tmp_path / "clip.wav"
    sample.write_bytes(WAV)
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return accepted()
        return httpx.Response(status, json={"error": {"retryable": False}})

    block = code_block(README, "def transcribe(client: Machinera")
    namespace: dict[str, Any] = {}
    exec(block[: block.index("\n\nwith Machinera(")], namespace)
    namespace["time"] = types.SimpleNamespace(sleep=sleeps.append)
    with machinera.Machinera(api_key="key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(machinera.APIStatusError) as caught:
            namespace["transcribe"](client, "sample", str(sample))
    assert caught.value.job_id == "job-1" and not caught.value.is_transient
    assert [request.method for request in requests] == ["POST", "GET"]
    assert sleeps == []
    frame = next(entry.frame for entry in caught.traceback if entry.name == "transcribe")
    assert frame.f_locals["job_id"] == "job-1"


@pytest.mark.parametrize("phase", ["job_submit", "submit", "prepare", "poll"])
@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        (machinera.InternalServerError, 500),
        (machinera.APIResponseValidationError, 200),
        (machinera.APIResponseValidationError, 400),
        (machinera.APIConnectionError, None),
    ],
)
def test_readme_recipes_share_the_keyed_recovery_exception(
    phase: str, error_type: type[machinera.APIError], status: int | None
) -> None:
    error = error_type("failed", status_code=status, phase=phase, retryable=False)
    assert not error.is_transient
    expected = phase in ("job_submit", "submit") and status in (200, 500)
    provider_namespace: dict[str, Any] = {}
    exec(code_block(README, "class MachineraProvider"), provider_namespace)
    assert provider_namespace["worth_retrying"](error) is expected

    calls: list[str] = []
    sleeps: list[float] = []

    class Client:
        def transcribe_file(self, path: str, **options: object) -> object:
            calls.append(str(options["idempotency_key"]))
            if len(calls) == 1:
                raise error
            return types.SimpleNamespace(text="hi")

    block = code_block(README, "def transcribe(client: Machinera")
    namespace: dict[str, Any] = {"open": lambda *args: io.BytesIO(WAV)}
    exec(block[: block.index("\n\nwith Machinera(")], namespace)
    namespace["time"] = types.SimpleNamespace(sleep=sleeps.append)
    if expected:
        assert namespace["transcribe"](Client(), "sample", "clip.wav") == "hi"
        assert len(calls) == 2 and len(set(calls)) == 1 and sleeps == [2]
    else:
        with pytest.raises(error_type) as caught:
            namespace["transcribe"](Client(), "sample", "clip.wav")
        assert caught.value is error and len(calls) == 1 and sleeps == []


def test_readme_deadline_headline_links_exceptions_and_polling_rule() -> None:
    section = README.split("## Deadlines and retries\n", 1)[1].split("\n## ", 1)[0]
    assert "ends only" not in section
    assert "api.md#deadline-exceptions)" in section
    assert "canonical [polling and recovery rule]" in section
    assert "api.md#retrypolicy" in section
    interrupt = README.split("- **Ctrl-C:**", 1)[1].split("\n- **", 1)[0]
    assert "api.md#deadline-exceptions)" in interrupt
    assert "caller-keyed recovery exception" in failure_table(REFERENCE)


def test_reference_groups_deadline_exceptions() -> None:
    section = REFERENCE.split("#### Deadline exceptions\n", 1)[1].split("\n#### ", 1)[0]
    assert "**Async local I/O:**" in section
    assert "**Blocking local work:**" in section
    assert "**Blocking deferred cleanup:**" in section
    assert "`Machinera.cancel()`" in section
    assert "[cancellation latency contract](#cancel)" in section
    assert "**Recipe pre-call hashing:**" in section
    cancellation = REFERENCE.split("#### `cancel`\n", 1)[1].split("\n#### ", 1)[0]
    assert "[deadline exceptions](#deadline-exceptions)" in cancellation
    assert "**Deadline and cancellation exceptions:**" not in REFERENCE


def test_api_reference_section_links_resolve() -> None:
    headings = re.findall(r"^#{1,6} (.+)$", REFERENCE, re.MULTILINE)
    anchors = {re.sub(r"[^\w -]", "", heading).lower().replace(" ", "-") for heading in headings}
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for text in (README, REFERENCE, changelog):
        for anchor in re.findall(r"\]\([^)]*api\.md#([^)]*)\)", text):
            assert anchor in anchors, anchor
    for anchor in re.findall(r"\]\(#([^)]*)\)", REFERENCE):
        assert anchor in anchors, anchor


def test_changelog_qualifies_interruption_retry_behavior() -> None:
    text = " ".join((ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split())
    assert "an outer retry never swallows Ctrl-C" not in text
    assert "retry code that consults `is_transient` will not retry it" in text
    assert (
        "harnesses that catch every `Exception` need the [Ctrl-C conversion in the recipe]" in text
    )
    assert "(README.md#evaluation-harnesses)" in text


def test_upload_recovery_surface_is_documented() -> None:
    assert "UploadPhase" in machinera.__all__
    for name in ("operation_key", "upload_id", "phase", "job_id", "storage_code"):
        assert hasattr(machinera.UploadError("failure"), name)
        assert f"`{name}`" in REFERENCE


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
    assert "job_multipart_body_bytes" in inspect.signature(machinera.Limits).parameters
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


def test_recovery_and_timeout_qualifications_are_linked() -> None:
    reference = REFERENCE
    flat = " ".join(reference.split())
    never = next(line for line in reference.splitlines() if line.startswith('| `"never"` |'))
    assert "does not replay an error response" not in never
    assert "[`is_transient`](#machineraerror) and the [retry rules](#retrypolicy)" in never
    conflict = next(line for line in reference.splitlines() if line.startswith("| `ConflictError`"))
    assert "apply the ordered" in conflict
    assert "[`is_transient`](#machineraerror)" in conflict
    assert "[Failure handling](#failure-handling)" in conflict
    assert "permanent" not in conflict
    assert (
        "Calls remain bounded, subject to the [deadline exceptions](#deadline-exceptions)" in flat
    )
    assert "for duplicate-charge and replay-retention guarantees" in flat
    assert "caller-keyed call with no `job_id` and `is_transient=False`" in flat
    assert "row 7 is an `InternalServerError` or `APIResponseValidationError` in `phase`" in flat
    assert '`"job_submit"` or `"submit"`, excluding HTTP 4xx' in flat
    assert "`APIConnectionError` with `retryable=False` does not qualify" in flat


def test_cancellation_contract_interval_matches_the_sdk() -> None:
    contract = REFERENCE.split("#### `cancel`\n", 1)[1].split("\n#### ", 1)[0]
    intervals = re.findall(r"\*\*(\d+(?:\.\d+)?) ms\*\*", contract)
    assert len(intervals) == 1, "The cancellation contract must state its interval once"
    assert all(float(value) / 1000 == pytest.approx(_CANCEL_INTERVAL) for value in intervals)


@pytest.mark.parametrize("document", ["README.md", "CHANGELOG.md"])
def test_cancellation_summaries_link_to_contract_without_copied_durations(document: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    if document == "README.md":
        summary = text.split("- **Ctrl-C:**", 1)[1].split("\n- ", 1)[0]
        target = "https://github.com/machinera-labs/machinera-python/blob/main/api.md#cancel"
    else:
        summary = text.split("- `Machinera.cancel()`", 1)[1].split("\n\n", 1)[0]
        target = "api.md#cancel"
    assert f"[cancellation contract]({target})" in summary
    assert not re.search(
        r"\b\d+(?:[.,]\d+)*[\s*-]*(?:milliseconds?|seconds?|ms|s)\b",
        summary,
        re.IGNORECASE,
    )


def test_readme_import_time_client_owns_interrupts() -> None:
    import signal

    namespace: dict[str, Any] = {"PermanentError": PermanentError}
    exec(code_block(README, "class MachineraProvider"), namespace)
    clients = []

    def construct(**options: Any) -> machinera.Machinera:
        assert options == {"cancel_on_interrupt": True}
        client = machinera.Machinera(api_key="key", **options)
        clients.append(client)
        return client

    namespace["Machinera"] = construct
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        exec(code_block(README, "CLIENT = Machinera(cancel_on_interrupt=True)"), namespace)
        provider = namespace["provider"]
        assert provider.client is clients[0]
        # No harness hook calls cancel: the provider owns SIGINT at import time.
        with pytest.raises(KeyboardInterrupt) as caught:
            signal.raise_signal(signal.SIGINT)
        assert type(caught.value) is KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt) as thread_error:
            provider.transcribe(WAV)
        assert type(thread_error.value) is KeyboardInterrupt
        assert isinstance(thread_error.value.__cause__, machinera.TranscriptionInterrupted)
    finally:
        for client in clients:
            client.close()
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize(
    ("error_type", "status", "job_id", "phase", "retryable", "caller_key", "row", "repeat"),
    [
        (machinera.TranscriptionInterrupted, None, "job-1", "poll", False, True, 1, False),
        (machinera.TerminalJobError, 200, "job-1", "poll", True, True, 2, False),
        # Even a transient failed job needs a new submission, not same-job recovery.
        (machinera.TerminalJobError, 200, "job-1", "poll", True, False, 2, False),
        (machinera.NotFoundError, 404, "job-1", "poll", False, True, 7, False),
        (machinera.NotFoundError, 404, "job-1", "poll", False, None, 7, False),
        (machinera.NotFoundError, 404, None, "job_submit", False, True, 7, False),
        (machinera.DeadlineExceededError, None, "job-1", "poll", None, True, 3, True),
        (machinera.DeadlineExceededError, None, None, "job_submit", None, True, 5, True),
        (machinera.InternalServerError, 500, None, "submit", False, True, 7, True),
        (machinera.InternalServerError, 500, "job-1", "poll", False, True, 3, True),
        (machinera.RateLimitError, 429, None, "upload_init", True, True, 6, True),
        (machinera.AmbiguousSubmissionError, None, None, "sync_submit", False, False, 4, False),
    ],
)
def test_readme_worth_retrying_matches_ordered_failure_rows(
    error_type: type[machinera.APIError],
    status: int | None,
    job_id: str | None,
    phase: str,
    retryable: bool | None,
    caller_key: bool | None,
    row: int,
    repeat: bool,
) -> None:
    error = error_type(
        "failed", status_code=status, job_id=job_id, phase=phase, retryable=retryable
    )
    error._caller_key = caller_key
    # Bind the cases to the reference's ordered conditions and actions, including
    # the row 3 exclusion and row 7's narrowly scoped keyed recovery exception.
    rows = [line.split(" | ") for line in failure_table(REFERENCE).splitlines()[2:]]
    assert [int(cells[0].removeprefix("| ")) for cells in rows] == list(range(1, 8))
    conditions = {
        1: "`TranscriptionInterrupted`",
        2: "`TerminalJobError`, including `TerminalIntegrityError`",
        3: "Any other `APIError` with `job_id` set, excluding non-transient 4xx errors (row 7)",
        4: "`AmbiguousSubmissionError`",
        5: "`RecoverableJobError` (`DeadlineExceededError`) without `job_id`",
        6: "Any other error with `is_transient` `True`",
        7: "Anything else (`is_transient` `False`)",
    }
    actions = {
        1: "Stop;",
        2: "never `resume` it",
        3: "Call `resume(job_id, response_format=...)`",
        4: "treat it as failed for this input",
        5: "repeat the call as made",
        6: "Repeat the identical call",
        7: "Other errors are permanent for this input, including a non-transient 4xx",
    }
    for number, cells in enumerate(rows, 1):
        assert cells[1].startswith(conditions[number])
        assert actions[number] in cells[2]
    if row == 7 and repeat:
        assert "[caller-keyed recovery exception]" in rows[row - 1][2]
    if row == 2 and caller_key is False:
        assert error.is_transient  # row 2 must win over row 6
    if error_type is machinera.DeadlineExceededError:
        assert error.is_transient  # rows 3/5 win over row 6

    namespace: dict[str, Any] = {}
    exec(code_block(README, "def worth_retrying"), namespace)
    assert namespace["worth_retrying"](error) is repeat, f"Failure handling row {row}"


def test_readme_provider_stops_on_initial_poll_not_found() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    provider = readme_provider(
        job_service(
            requests,
            lambda n: accepted(),
            lambda n: httpx.Response(404, json={"error": {"code": 1036}}),
        ),
        clock,
    )
    harness = Harness(provider, clock)
    with pytest.raises(PermanentError) as caught:
        harness.run()
    error = caught.value.__cause__
    assert isinstance(error, machinera.NotFoundError)
    assert error.job_id == "job-1" and error.phase == "poll" and not error.is_transient
    assert len(harness.attempts) == 1
    assert [request.method for request in requests] == ["POST", "GET"]
