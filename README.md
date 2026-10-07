# Machinera Python SDK

Python client for the Machinera speech-to-text API: a blocking client (`Machinera`)
and a native asyncio client (`AsyncMachinera`) with the same methods. The
[API reference](https://github.com/machinera-labs/machinera-python/blob/main/api.md)
is the authority for every behavior summarized here.

SDK 0.2.0 requires the numeric-code API. Error and warning `code` values are
integers; string codes are unsupported. SDK 0.1.x cannot talk to the server after
this API cut. Record the integer `error.code` and `error.request_id` for support.

## Install and pin

```sh
pip install "machinera==0.2.0"
export MACHINERA_API_KEY="your-api-key"
```

APIs listed under [CHANGELOG “Unreleased”](https://github.com/machinera-labs/machinera-python/blob/main/CHANGELOG.md#unreleased)
require the next release and are not available in the pin above.

- Python 3.10 through 3.14. No audio decoding or conversion tools are needed.
- Pin an exact version where installs must be reproducible. During `0.x`, minor
  releases may change the public API and patch releases are compatible fixes; read the
  [changelog](https://github.com/machinera-labs/machinera-python/blob/main/CHANGELOG.md)
  when upgrading.
- `api_key` defaults to `MACHINERA_API_KEY` and `base_url` to `MACHINERA_BASE_URL`,
  then `https://api.machinera.com/v1`. A missing or invalid value makes the
  constructor raise `ValueError`.

## Quickstart

```python
from machinera import Machinera

with Machinera() as client:
    result = client.transcribe_file("recording.wav", model="transcribe-v1")
    print(result.text)
    result = client.transcribe_url("https://audio.example/recording.wav", model="transcribe-v1")
```

Each call blocks until the transcript is finished and returns a `TranscriptionResult`.
Every submission may incur usage charges.

- **Model:** `transcribe-v1` is the only model ID this SDK documents. There is no
  model-listing call; another ID fails with `BadRequestError`, code `1009`.
- **English only:** the SDK checks the `language` hint locally: it accepts `None`,
  `"en"`, or an `en-*` tag, and any other value raises `ValueError` before any request;
  treat it as a permanent failure. The SDK never inspects the audio. The service may
  reject audio it detects as not English with code `1029`, which is
  permanent: `UnprocessableEntityError` at submission, or `TerminalJobError` for an
  accepted job. Whether the service applies that check is decided by the service, not
  the SDK; see the
  [error reference](https://api.machinera.com/docs/errors/1029). Send
  `language="en"` to assert English.
- **No prompt:** there is no prompt, vocabulary, or context parameter. Drop such inputs.
- **Formats:** the suffixes in `SUPPORTED_MEDIA_SUFFIXES` (`flac`, `m4a`, `mp3`, `mp4`,
  `mpeg`, `mpga`, `ogg`, `wav`, `webm`). Paths, `bytes`, seekable binary handles, and
  `(filename, content)` tuples are accepted. A supported file name, from `filename`, the
  tuple, or the path, identifies the format and takes precedence over `content_type`.
  Content without one is identified from `content_type`, or from its container signature
  when none is given, otherwise `ValueError`. For such content, a `content_type` the SDK
  does not recognize raises `ValueError` rather than falling back to the signature, so
  pass none when unsure.
- **Output:** `response_format` is `"json"` (default), `"text"`, or `"verbose_json"`.
  The result has `text`; `job_id` (`None` for a synchronous request);
  `request_id`, from the last HTTP response the call received, or `None` when the
  service sent none; `elapsed_seconds`, the client's wall time including waiting to start
  and polling; and `duration` and `inference_seconds` when the service returns them.
- **Direct audio URL:** the service fetches the URL itself; the SDK sends only the URL
  string (at most 65,536 bytes of JSON) and no credentials or headers for it. Use an
  `http` or `https` URL that returns the audio bytes with no further authentication,
  such as a pre-signed URL. A job may not start immediately, so a signed URL must stay valid
  until the job runs. Redirect handling is not specified; pass the final URL. The same
  audio limits apply as for files. The
  [URL failures](https://github.com/machinera-labs/machinera-python/blob/main/api.md#transcribe_url)
  are permanent for that URL.

## How requests are routed

| Call | Route |
| --- | --- |
| `transcribe_url(...)` | Always a durable job (a server-side job resumable by ID). |
| `transcribe_file(...)` with `idempotency_key`, or a client with `transport="job"` | Durable job: multipart up to `Limits.job_multipart_body_bytes` (52,428,800) encoded bytes, a file upload above it. |
| `transcribe_file(...)` without a key, default `transport="auto"` | One synchronous request up to `Limits.sync_inline_body_bytes` (26,214,400) encoded bytes; above that, as with a key. |

Encoded size is the file plus a few hundred bytes of multipart framing; a size equal to
a limit takes the smaller route. About 13 minutes of 16 kHz 16-bit mono WAV, or about
27 minutes of 128 kbit/s MP3, fit under the synchronous limit. Changing `Limits` changes
only the SDK's choice, never the service's limits.

- **Latency:** a synchronous request returns in one round trip. A durable job adds
  submission, waiting to start, and polling (every `RetryPolicy.poll_interval`, 1 second by
  default).
- **Automatic fallback:** see the API reference for
  [sync-to-job fallback conditions](https://github.com/machinera-labs/machinera-python/blob/main/api.md#transcribe_file).
- **Duplicate charges:** an unkeyed synchronous request may already have run and been
  billed when it fails. See [synchronous replay](https://github.com/machinera-labs/machinera-python/blob/main/api.md#synchronous-replay)
  for replay eligibility and [failure handling](https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling)
  for caller recovery guidance.
- **Unkeyed durable jobs are new submissions:** `transcribe_url` and every durable job
  without `idempotency_key` get a fresh random key, so repeating such a call submits and
  bills a new job. Retry durable-job calls only under a key, as the
  [harness recipe](https://github.com/machinera-labs/machinera-python/blob/main/README.md#evaluation-harnesses) does.
- **`transport` has two unrelated meanings.** The string `"auto"` or `"job"` selects the
  route; an `httpx.BaseTransport` object (`httpx.AsyncBaseTransport` for
  `AsyncMachinera`) replaces the HTTP layer, for example in offline tests, and fixes
  the route at `"auto"`. With an injected transport, pass keys to force durable jobs.

## Failure handling

The SDK already retries what is safe to retry. For recovery, use the first matching
row of the API reference's [Failure handling table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling),
which covers interrupts, failed or accepted jobs, ambiguous submissions, and other
errors. Under an outer retry, `is_transient` means repeating the identical call, as
made, is safe and may succeed; remembered `job_id` or key context also matters.
Pass `idempotency_key` on every call and re-raise interrupts as plain
`KeyboardInterrupt`, as the [harness recipe](#evaluation-harnesses) does.

A status error's `retryable` is the service's flag when it sends one, otherwise the
SDK's code table, otherwise `True` only for HTTP 429, 502, 503, and 504. Whether an
error is transient follows the first matching row of the
[`is_transient` table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#machineraerror), which also covers the synchronous route
and a lost job submission. An error built from a service
response, including a failed job, always has `retryable` `True` or `False`, never `None`;
a failed job whose code the SDK does not know, sent without the service's flag, has
`False`.

With your own `idempotency_key`, repeating the identical call never runs or bills it
twice; returning the original job/result depends on replay retention (see
[Recovery after a restart](#recovery-after-a-restart)). `is_transient` then says only whether the repeat may succeed.
After upload replacement, `is_transient` is `False`: retain the current
`operation_key` and `upload_id` and use file `resume(...)` (or `resume(job_id)`
when known). The recipes below retain this context across outer retries.
A keyed job submission that was sent but ended without a `job_id` in a non-transient
5xx or `APIResponseValidationError` may have been accepted, so repeating it with the
current key and upload ID, within your own retry limit, may still return the job.

Bound rows 3, 5, and 6 with your own budget, for example 3 attempts with pauses of 2,
4, and 8 seconds. `str(error)` ends with `(request_id: ...)` when it is known; read
`request_id` with `getattr(error, "request_id", None)`, because a `MachineraError`
that is not an `APIError` has none.

For a failed job's codes, retryability, and meanings, see the
[job-code table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#joberror).

## Evaluation harnesses

Most harnesses call the provider from several threads and retry a failed sample unless
it raises a permanent error. The provider below applies the
[failure table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling) for files of any size: within a run, retries of
identical input (same bytes, suffix and options; see **Keys** below) never run or bill
a job twice, and known jobs are resumed (row 3). Recovery may repeat HTTP submissions
under the same key; see [`resume`](https://github.com/machinera-labs/machinera-python/blob/main/api.md#resume).

```python
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from machinera import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    DeadlineExceededError,
    InternalServerError,
    Machinera,
    MachineraError,
    TerminalJobError,
    TranscriptionInterrupted,
)

RUN = "my-run-2026-10-05"  # a new value transcribes every sample again
MODEL, FORMAT = "transcribe-v1", "json"
BUDGET = 300  # seconds per attempt, counted from before hashing; hashing is not cut short


class MachineraProvider:
    def __init__(self) -> None:
        self.client = CLIENT
        self.digests: dict[tuple[str, int, int], str] = {}  # (path, size, mtime) -> hash
        self.hashing: dict[str, threading.Lock] = {}  # one thread hashes a path at a time
        self.jobs: dict[str, str] = {}  # key -> a job_id an earlier error carried
        self.uploads: dict[str, tuple[str, str | None]] = {}

    def transcribe(self, audio: str | bytes, language: str = "en") -> str:
        end = time.monotonic() + BUDGET
        try:
            key = self.operation_key(audio, language)
            return self.call(audio, language, key, end)
        except TranscriptionInterrupted as exc:
            self.remember(key, exc)
            raise KeyboardInterrupt from exc  # it is also an Exception, which a harness retries
        except MachineraError as exc:
            self.remember(key, exc)
            if worth_retrying(exc):
                raise  # the harness's retry repeats or resumes; neither bills twice
            raise PermanentError(str(exc)) from exc
        except (ValueError, TypeError, OSError) as exc:  # such as a missing path
            raise PermanentError(str(exc)) from exc

    def remember(self, key: str, error: MachineraError) -> None:
        job_id = getattr(error, "job_id", None)
        if job_id is not None and not isinstance(error, TerminalJobError):
            self.jobs[key] = job_id
        operation_key = getattr(error, "operation_key", None)
        upload_id = getattr(error, "upload_id", None)
        if operation_key and (
            upload_id is not None
            or getattr(error, "phase", None) in ("upload_init", "upload_put", "submit")
        ):
            self.uploads[key] = (operation_key, upload_id)

    def call(self, audio: str | bytes, language: str, key: str, end: float) -> str:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("attempt budget spent")  # the harness retries the sample
        job_id = self.jobs.get(key)
        if job_id is not None:  # poll the job; never upload or submit it again
            return self.client.resume(job_id, response_format=FORMAT, deadline=remaining).text
        name = None if isinstance(audio, bytes) else "upload" + Path(audio).suffix.lower()
        if key in self.uploads:
            operation_key, upload_id = self.uploads[key]
            return self.client.resume(
                file=audio,
                model=MODEL,
                language=language,
                response_format=FORMAT,
                filename=name,
                operation_key=operation_key,
                upload_id=upload_id,
                deadline=remaining,
            ).text
        return self.client.transcribe_file(
            audio,
            model=MODEL,
            language=language,
            response_format=FORMAT,
            filename=name,  # the same name for every path with this suffix
            idempotency_key=key,
            deadline=remaining,
        ).text

    def operation_key(self, audio: str | bytes, language: str) -> str:
        """The run, options, suffix, and bytes give the key; the rest of the name does not."""
        if isinstance(audio, bytes):
            suffix = "bytes"  # the SDK names bytes from their content
            digest = hashlib.sha256(audio).hexdigest()
        else:
            suffix = Path(audio).suffix.lower()  # it sets the media type the SDK sends
            stat = os.stat(audio)
            cached = (os.path.abspath(audio), stat.st_size, stat.st_mtime_ns)
            with self.hashing.setdefault(cached[0], threading.Lock()):
                if cached not in self.digests:  # hash a path once, not on every attempt
                    content = hashlib.sha256()
                    with open(audio, "rb") as file:
                        for chunk in iter(lambda: file.read(1 << 20), b""):  # 1 MiB at a time
                            content.update(chunk)
                    self.digests[cached] = content.hexdigest()
            digest = self.digests[cached]
        parts = [RUN, MODEL, language, FORMAT, suffix, digest]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def worth_retrying(exc: MachineraError) -> bool:
    """Transient, or a keyed request that may still succeed; never a failed job or a non-transient 4xx."""
    status = getattr(exc, "status_code", None) or 0
    if isinstance(exc, (TranscriptionInterrupted, TerminalJobError)):
        return False  # rows 1 and 2 take precedence even if a failed job is transient
    if exc.is_transient:
        return True
    upload = getattr(exc, "operation_key", None) and getattr(exc, "phase", None) in (
        "upload_init",
        "upload_put",
        "submit",
    )
    if upload and (
        isinstance(exc, DeadlineExceededError)
        or (
            isinstance(exc, (APIConnectionError, APIStatusError))
            and exc.retryable is True
            and status not in (401, 403)
        )
    ):
        return True  # resume saved upload context; do not repeat the original key
    if 400 <= status < 500:
        return False  # row 7, even with a job_id from the initial call
    if getattr(exc, "job_id", None) is not None:
        return True  # a status read failed; the job is unaffected (row 3)
    submitting = getattr(exc, "phase", None) in ("job_submit", "submit")
    return submitting and isinstance(exc, (InternalServerError, APIResponseValidationError))
```

`PermanentError` is your harness's do-not-retry exception, and `audio` is a path or the
file's bytes. This recipe requires a supported path suffix because its key includes
the suffix and it sends an explicit filename with that suffix. The SDK itself also
accepts other inputs, including paths identified by container inspection; see
[`transcribe_file`](https://github.com/machinera-labs/machinera-python/blob/main/api.md#transcribe_file).
Every provider object and the harness's threads share one module-level `CLIENT`.
Calling `cancel()` affects every active and future operation on that shared instance;
see the [cancellation contract](https://github.com/machinera-labs/machinera-python/blob/main/api.md#cancel).
Construct the client at module import on the main thread, before the harness starts
its threads:

```python
CLIENT = Machinera(cancel_on_interrupt=True)
provider = MachineraProvider()
```

The provider now owns SIGINT cancellation without requiring a harness interrupt
hook; see the [cancellation contract](https://github.com/machinera-labs/machinera-python/blob/main/api.md#cancel).
When you also own the executor, its cleanup can look like this:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_samples(samples: list[str]) -> list[str]:
    executor = ThreadPoolExecutor(8)
    try:
        futures = [executor.submit(provider.transcribe, sample) for sample in samples]
        return [future.result() for future in as_completed(futures)]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        provider.client.close()  # interrupted SDK calls unwind promptly
```

- **Billed once:** the [keyed-call guarantee](https://github.com/machinera-labs/machinera-python/blob/main/api.md#machineraerror)
  means a harness retry and two threads given the same path (the same bytes and suffix;
  see **Keys** below) under the same `RUN` and options both reach one job. The recipe's
  `worth_retrying` follows [Failure handling](https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling), including its
  caller-keyed recovery exception. A `job_id` is remembered unless the job failed,
  including before an interruption is re-raised; later attempts `resume` it per row 3.
  The harness's retry limit bounds recovery. The
  [retention minimums](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retention-defaults)
  are guaranteed: a harness whose whole retry sequence (attempts × budget + backoff)
  finishes inside the replay-period minimum can recover an accepted job by key;
  work not yet submitted instead follows
  [file upload recovery and expiry](https://github.com/machinera-labs/machinera-python/blob/main/api.md#file-upload-recovery-and-expiry).
  Include any time beyond the budget caused by pre-call hashing or the documented
  deadline exceptions.
  Errors that `worth_retrying` rejects
  become `PermanentError`; see rows 2 and 7. Whether a failed job itself was charged
  is decided by the service; the SDK cannot tell.
- **Ctrl-C:** the import-time client uses `cancel_on_interrupt=True` to call
  `cancel()` before chaining to the previous SIGINT handler (normally a plain
  `KeyboardInterrupt` in the main thread). No harness cancellation hook is needed.
  This interrupts calls in running threads with `TranscriptionInterrupted`,
  preserving known job IDs. The provider converts that error to plain
  `KeyboardInterrupt` so a harness's `except Exception` does not retry it.
  For the latency bound, including scheduling and local-work qualifications, see the
  [cancellation contract](https://github.com/machinera-labs/machinera-python/blob/main/api.md#cancel).
  Pending futures are cancelled; running SDK calls unwind. The interpreter still
  joins executor threads at exit; see the
  [deadline exceptions](https://github.com/machinera-labs/machinera-python/blob/main/api.md#deadline-exceptions)
  for work that can delay exit. Cancellation is permanent for this client; use a
  new client to resume. Close this client on the main thread to restore the previous
  signal handler; construct a new client for another run.
- **One budget per attempt:** the attempt's clock starts before hashing, so the SDK call
  and any `resume` get only what hashing left of `BUDGET`, as their `deadline`. The
  SDK's `deadline` covers its own preparation, waiting for a concurrency slot, the
  upload, its own retry pauses, and polling. Hashing a path is local file I/O that nothing interrupts: it
  takes as long as the disk needs, so on a slow disk an attempt can last longer than
  `BUDGET`. It runs once per path, size, and modification time, in one thread at a time
  per path, so threads given the same new path wait for one hash. An attempt whose budget
  is spent before the call raises an ordinary exception without sending anything, and
  the harness's retry uses the stored hash. A job still
  running at the deadline raises a transient `DeadlineExceededError` with its `job_id`,
  and the next attempt resumes it, so a long file can finish over several attempts.
  The pause between attempts is the harness's own.
- **Long jobs:** a job outlives the harness. If the harness gives up first (for example
  after 10 attempts of 300 seconds), the job keeps running and is billed; rerun with
  the same `RUN` to collect it while the [replay binding and result retention](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retention-defaults) hold,
  or raise `BUDGET` for long files, or for large files on
  a slow connection, whose upload restarts on every attempt, as does the SDK's own
  hashing pass over a uploaded file, inside the SDK's `deadline`. Size these attempts
  and pauses using the [file upload recovery and expiry contract](https://github.com/machinera-labs/machinera-python/blob/main/api.md#file-upload-recovery-and-expiry),
  which supplies no universal minimum upload duration.
- **Latency:** each sample's time includes job submission, waiting to start, and polling every
  [`RetryPolicy.poll_interval`](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retrypolicy). `inference_seconds` on the result,
  when the service returns it, is the service's own processing time. A key from an
  earlier run replays that run's job, so use a new `RUN` for each run you time. A call
  without a key takes the faster synchronous route, but a harness retry of it is a new
  submission, which may be billed again if the first one ran.
- **Keys:** paths with the same run, model, language, format, suffix, and bytes share a
  key, whatever the rest of their names, and so do identical raw bytes, so a later or
  concurrent sample gets the earlier one's job and result instead of a second charge.
  Repeating a running job's key returns that job for polling. Paths are sent as `upload.<suffix>`;
  the suffix sets the media type and stays in the key. Raw bytes are named from their content.
  Thus each key identifies an identical request: `clip.m4a`, `clip.mp4`, and raw bytes yield
  three keys/jobs. Replay expiry is permanent; see [Recovery after a restart](#recovery-after-a-restart).
- **Raw samples:** bytes must be a whole file in a supported format. Headerless PCM has
  no signature the SDK can identify, so it raises `ValueError`, and there is no
  sample-rate parameter. Wrap it in WAV with the standard library first:

```python
import io
import wave

# pcm: your mono, 16-bit, 16 kHz PCM bytes.
buffer = io.BytesIO()
with wave.open(buffer, "wb") as out:
    out.setnchannels(1)
    out.setsampwidth(2)  # 16-bit samples
    out.setframerate(16000)
    out.writeframes(pcm)
audio = buffer.getvalue()
```

When the harness hands over a URL, download it and pass the bytes, so the key is
derived from the audio itself. `httpx` is installed with the SDK. A 4xx from the
download, other than 408 and 429, is permanent for that sample; every other download
failure propagates for the harness to retry. `follow_redirects` here applies only to
your own download; the service still needs the final URL when you do pass a URL to
`transcribe_url`.

```python
import httpx

# url: the audio URL; provider: your MachineraProvider; PermanentError: your harness error.
response = httpx.get(url, follow_redirects=True, timeout=60)
if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
    raise PermanentError(f"audio URL returned HTTP {response.status_code}")
response.raise_for_status()
text = provider.transcribe(response.content)
```

The recipe also survives a crash: its keys are derived again after a restart, so a
rerun with the same `RUN` replays every job the service still keeps. Code that owns its
retries instead of a harness uses the failure table and keys:

- **Long files:** an `idempotency_key` on every call, derived from the dataset, sample
  ID, a hash of the file content, and the options, plus a `deadline` sized for the
  longest file. Save the key and any `job_id`.
- **URL inputs:** `transcribe_url` with a key derived from the URL and options.

A client may be shared by threads, created per call or per provider (construction does
no I/O, and connections are shared process-wide), or left unclosed for the life of the
process. `max_concurrency` limits only its own client. A closed client raises
`APIConnectionError` with `is_transient` `False` on every later call, so never close a
client you keep for reuse. A recovery loop that owns retries follows the same
[Failure handling](https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling) rules:

```python
import hashlib
import time

from machinera import (
    APIError,
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    DeadlineExceededError,
    InternalServerError,
    Machinera,
    MachineraError,
    TerminalJobError,
    TranscriptionInterrupted,
)

DEADLINE = 4 * 60 * 60  # longest expected job, in seconds


def transcribe(client: Machinera, sample_id: str, path: str) -> str:
    content = hashlib.sha256()
    with open(path, "rb") as audio:
        for chunk in iter(lambda: audio.read(1 << 20), b""):  # 1 MiB at a time
            content.update(chunk)
    options = "transcribe-v1:en:json"  # model, language, and format
    key = hashlib.sha256(f"my-run:{sample_id}:{options}:{content.hexdigest()}".encode()).hexdigest()
    job_id = None
    upload = None
    for attempt in range(3):
        try:
            if job_id is not None:
                return client.resume(job_id, response_format="json", deadline=DEADLINE).text
            if upload is not None:
                operation_key, upload_id = upload
                return client.resume(
                    file=path,
                    model="transcribe-v1",
                    language="en",
                    response_format="json",
                    operation_key=operation_key,
                    upload_id=upload_id,
                    deadline=DEADLINE,
                ).text
            return client.transcribe_file(
                path,
                model="transcribe-v1",
                language="en",
                response_format="json",
                idempotency_key=key,
                deadline=DEADLINE,
            ).text
        except (TranscriptionInterrupted, TerminalJobError):
            raise
        except MachineraError as error:
            accepted = error.job_id if isinstance(error, APIError) else None
            status = getattr(error, "status_code", None) or 0
            if accepted and job_id is None:
                job_id = accepted  # row 3: poll the accepted job from now on
            phase = getattr(error, "phase", None)
            if (
                isinstance(error, APIError)
                and error.operation_key
                and (
                    error.upload_id is not None or phase in ("upload_init", "upload_put", "submit")
                )
            ):
                upload = (error.operation_key, error.upload_id)
            recover_upload = upload is not None and (
                isinstance(error, DeadlineExceededError)
                or (
                    isinstance(error, (APIConnectionError, APIStatusError))
                    and error.retryable is True
                    and status not in (401, 403)
                )
            )
            if not error.is_transient and not recover_upload:
                submitting = getattr(error, "phase", None) in ("job_submit", "submit")
                recoverable = submitting and isinstance(
                    error, (InternalServerError, APIResponseValidationError)
                )
                if 400 <= status < 500 or (job_id is None and not recoverable):
                    raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"{sample_id}: attempts exhausted (key {key}, job_id {job_id})")


with Machinera(max_concurrency=8) as client:
    text = transcribe(client, "sample-0001", "sample-0001.wav")
```

Every call here is keyed, so rows 4 and 5's `"sync_submit"` case cannot occur.

## Long files

An encoded file above `Limits.job_multipart_body_bytes` (52,428,800 bytes) is uploaded to
storage and then submitted as a job, automatically. The service sets the upload size
limit, the upload expiry period, and the maximum audio duration (code
`1015`); see the [public limits](https://api.machinera.com/docs/limits).

- Keeping the file immutable across attempts is the caller's obligation. Changes
  detected during SDK preparation or streaming raise `IntegrityError`; see
  [`transcribe_file`](https://github.com/machinera-labs/machinera-python/blob/main/api.md#transcribe_file).
- Set `deadline` for the whole job, including waiting to start. A deadline only stops waiting:
  the job keeps running and `resume(error.job_id)` picks it up.
- A definitive `1003` at job submission starts a fresh upload within the retry
  budget and original deadline. Exhaustion raises non-transient `UploadError`; see [file upload recovery and expiry](https://github.com/machinera-labs/machinera-python/blob/main/api.md#file-upload-recovery-and-expiry)
  for the separate upload deadline and how to size retries.
- `1005` or `1031`: the key was reused with
  different input or options; fix the key derivation.

## Recovery after a restart

Save an operation key **before** calling, then save the current `operation_key`,
`upload_id`, and `job_id` from any error. A replacement upload rotates the key.
These two fragments use application-provided durable storage: implement
`save_recovery_state(key=..., upload_id=..., job_id=...)`, and load its fields into
`saved_key`, `saved_upload_id`, and `saved_job_id` before running the restart fragment.
The storage update must preserve any already-known job ID. Complete programs are linked below:

```python
import uuid

from machinera import APIError, Machinera

key = uuid.uuid4().hex
save_recovery_state(key=key, upload_id=None, job_id=None)  # your own durable storage

with Machinera() as client:
    try:
        result = client.transcribe_file("recording.wav", model="transcribe-v1", idempotency_key=key)
    except APIError as error:
        save_recovery_state(
            key=error.operation_key or key, upload_id=error.upload_id, job_id=error.job_id
        )
        raise
```

After a restart:

```python
with Machinera() as client:
    if saved_job_id is not None:
        result = client.resume(saved_job_id, response_format="json")  # polls only
    elif saved_upload_id is not None:
        result = client.resume(
            file="recording.wav",
            model="transcribe-v1",
            operation_key=saved_key,
            upload_id=saved_upload_id,
            response_format="json",
        )
    else:
        result = client.transcribe_file(  # replays the saved operation
            "recording.wav", model="transcribe-v1", idempotency_key=saved_key
        )
```

- This covers multipart and uploaded files alike. Reuse a key only for the identical
  request: same file bytes, options, credential, and endpoint.
- `resume` has two forms. `resume(job_id)` only polls and defaults to
  `response_format="verbose_json"`. `resume(file=..., model=..., operation_key=...)`
  replays a file upload submission and defaults to `"json"`. Pass the original format to
  either.
- A job that failed (`TerminalJobError`) stays failed: the same key replays the failure.
- Recovery is limited for accepted jobs by the public API's
  [guaranteed retention minimums](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retention-defaults):
  the SDK's snapshot of these minimums is
  `machinera.DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S` and `machinera.DEFAULT_RESULT_RETENTION_S`
  (in seconds). Availability is listed in the [CHANGELOG entry](https://github.com/machinera-labs/machinera-python/blob/main/CHANGELOG.md)
  that adds these constants.
  A finished job and its binding are kept at least the longer of the two.
  For accepted jobs, size retries and pauses against the cited minimums so that the
  whole retry sequence finishes inside the guaranteed replay period; work not yet
  submitted instead follows [file upload recovery and expiry](https://github.com/machinera-labs/machinera-python/blob/main/api.md#file-upload-recovery-and-expiry).
  Recovery does not extend retention.
  After expiry a replay fails with `ConflictError`, code `1030`.
  Keep `job_id` to `resume(job_id)` if a running job's binding expires;
  a new key submits and charges anew.
- The SDK never cancels a server job and keeps no journal on disk.

Complete programs: [blocking](https://github.com/machinera-labs/machinera-python/blob/main/examples/submit_and_resume.py),
[asyncio](https://github.com/machinera-labs/machinera-python/blob/main/examples/async_submit_and_resume.py), and
[large file](https://github.com/machinera-labs/machinera-python/blob/main/examples/transcribe_large_file.py).

## Deadlines and retries

Every call is bounded by a total deadline that covers preparation, waiting for a
concurrency slot, requests, retry pauses, and polling, subject to the
[deadline exceptions](https://github.com/machinera-labs/machinera-python/blob/main/api.md#deadline-exceptions).
Pass `deadline=seconds` to `transcribe_file`, `transcribe_url`, or `resume` to change
it for one call. See the canonical [polling and recovery rule](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retrypolicy).

`TimeoutPolicy`, `RetryPolicy`, and `Limits` configure timeouts, retries, polling,
and request sizes. See the API reference's [Defaults table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#defaults)
for every setting, its default value, and its meaning.

The SDK retries network failures and the status errors that the rule under
[Failure handling](https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling) makes transient, only when resending is safe or
`sync_replay="always"` allows it. Pauses are
`min(initial_delay * 2**retry, max_delay)` times a random factor in `[0.75, 1]`, and a
`Retry-After` header with a positive value sets a minimum. `Retry-After: 0` means
no known timed minimum; bounded backoff still applies. A pause that cannot finish before the deadline
raises `DeadlineExceededError`. `max_retries=n` is shorthand for `n + 1` attempts.

## Logging

The SDK logs to the `logging` logger named `machinera`: retry decisions at `DEBUG` and
job status changes at `INFO`. Records never contain URLs with query strings,
credentials, file paths, or transcript text. `MACHINERA_LOG=debug` or
`MACHINERA_LOG=info` sets that level when a client is created.

## Asyncio client

```python
import asyncio

from machinera import AsyncMachinera


async def main() -> str:
    async with AsyncMachinera() as client:
        result = await client.transcribe_file("recording.wav", model="transcribe-v1")
        return result.text


text = asyncio.run(main())
```

`AsyncMachinera` has the same configuration, methods, and errors; await them and close
it with `async with` or `aclose()`. Use one client per event loop, shared by its tasks.
Task cancellation re-raises `asyncio.CancelledError` with `operation_key`, `job_id`,
and `phase` attached; read them with `getattr(error, "job_id", None)` in the coroutine
that awaits the SDK method, save them, and re-raise.

## Reference

The [API reference](https://github.com/machinera-labs/machinera-python/blob/main/api.md)
covers file input rules, HTTP client injection and connection sharing, timeout forms,
result models, and the exception hierarchy. See also the
[runnable examples](https://github.com/machinera-labs/machinera-python/blob/main/examples/README.md),
[CONTRIBUTING.md](https://github.com/machinera-labs/machinera-python/blob/main/CONTRIBUTING.md),
and [SECURITY.md](https://github.com/machinera-labs/machinera-python/blob/main/SECURITY.md).
