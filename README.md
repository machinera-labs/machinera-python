# Machinera Python SDK

Python client for the Machinera speech-to-text API: a blocking client (`Machinera`)
and a native asyncio client (`AsyncMachinera`) with the same methods. The
[API reference](https://github.com/machinera-labs/machinera-python/blob/main/api.md)
is the authority for every behavior summarized here.

## Install and pin

```sh
pip install "machinera==0.1.3"
export MACHINERA_API_KEY="your-api-key"
```

- Python 3.10 through 3.14. No audio decoder or conversion tools are needed.
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
  model-listing call; another ID fails with `BadRequestError`, code `unknown_model`.
- **English only:** the SDK checks the `language` hint locally: it accepts `None`,
  `"en"`, or an `en-*` tag, and any other value raises `ValueError` before any request;
  treat it as a permanent failure. The SDK never inspects the audio. The service may
  reject audio it detects as not English with code `non_english_audio`, which is
  permanent: `UnprocessableEntityError` at submission, or `TerminalJobError` for an
  accepted job. Whether the service applies that check is decided by the service, not
  the SDK; see the
  [error reference](https://api.machinera.com/docs/errors#non_english_audio). Send
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
| `transcribe_file(...)` with `idempotency_key`, or a client with `transport="job"` | Durable job: inline up to `Limits.job_inline_body_bytes` (52,428,800) encoded bytes, a staged upload above it. |
| `transcribe_file(...)` without a key, default `transport="auto"` | One synchronous request up to `Limits.sync_inline_body_bytes` (26,214,400) encoded bytes; above that, as with a key. |

Encoded size is the file plus a few hundred bytes of multipart framing; a size equal to
a limit takes the smaller route. About 13 minutes of 16 kHz 16-bit mono WAV, or about
27 minutes of 128 kbit/s MP3, fit under the synchronous limit. Changing `Limits` changes
only the SDK's choice, never the service's limits.

- **Latency:** a synchronous request returns in one round trip. A durable job adds
  submission, waiting to start, and polling (every `RetryPolicy.poll_interval`, 1 second by
  default).
- **Automatic fallback:** when the synchronous route refuses an unkeyed request before
  doing any work (too large, or no capacity right now), `"auto"` submits the same body
  once as a durable job under an SDK-generated key; `result.job_id` is then set.
- **Duplicate charges:** if an unkeyed synchronous request fails after its body was sent,
  it may already have run and been billed. With the default `sync_replay="never"` the
  SDK does not replay it: a lost response raises `AmbiguousSubmissionError`. With
  `Machinera(sync_replay="always")` the SDK replays it under the normal retry policy, so
  a call may be billed twice. Keyed calls and durable jobs never have this trade-off.
- **Unkeyed durable jobs are new submissions:** `transcribe_url` and every durable job
  without `idempotency_key` get a fresh random key, so repeating such a call submits and
  bills a new job. Retry durable-job calls only under a key, as the
  [harness recipe](https://github.com/machinera-labs/machinera-python/blob/main/README.md#evaluation-harnesses) does.
- **`transport` has two unrelated meanings.** The string `"auto"` or `"job"` selects the
  route; an `httpx.BaseTransport` object (`httpx.AsyncBaseTransport` for
  `AsyncMachinera`) replaces the HTTP layer, for example in offline tests, and fixes
  the route at `"auto"`. With an injected transport, pass keys to force durable jobs.

## Failure handling

- The SDK already retries what is safe to retry; see [Deadlines and
  retries](https://github.com/machinera-labs/machinera-python/blob/main/README.md#deadlines-and-retries).
- Use the first matching row. Rows 1–5 are for code that owns recovery.
- Under an outer retry, `is_transient` means repeating the identical call, as made, is safe and may
  succeed; remembered `job_id` or key context also matters (rows 3 and 5). Pass `idempotency_key` on
  every call so a retry replays an accepted job instead of submitting a new one, and re-raise
  interrupts as plain `KeyboardInterrupt`, as the [harness
  recipe](https://github.com/machinera-labs/machinera-python/blob/main/README.md#evaluation-harnesses)
  does.

| # | Exception | Action |
| --- | --- | --- |
| 1 | `TranscriptionInterrupted` | Stop; `is_transient` is always `False`. Save `operation_key` and `job_id`, and continue later with the row that matches them. If `ambiguous` is `True`, follow row 4. |
| 2 | `TerminalJobError`, including `TerminalIntegrityError` | The job failed; never `resume` it. Repeating the call with the same key replays this failed job and raises the same error. Record `code`. If `retryable` is `True`, a submission under a new key may succeed, as a new job and a new charge; `is_transient` is then `True` for an unkeyed call, whose repeat is that new submission, and `False` for a keyed call or `resume`. |
| 3 | Any other `APIError` with `job_id` set | The job was accepted. Call `resume(job_id, response_format=...)` with the original format after a pause; it only polls and never charges again. A call with `idempotency_key` may instead be repeated as made when `is_transient` is `True`, which replays this job. If `resume` fails, a `TerminalJobError` follows row 2 and a non-transient 4xx such as `NotFoundError` is final for this job (row 7); any other failure, such as a 5xx, a malformed body, or a connection error, leaves the job unaffected, so `resume` again after a pause, within your own retry limit, whatever `is_transient` says. For a call without `idempotency_key`, `is_transient` is `False` here, because repeating that call would submit and bill a second job while this one may still run. |
| 4 | `AmbiguousSubmissionError` | An unkeyed synchronous request may have run (default `sync_replay="never"` only), and no SDK call can tell whether it did. Repeating it, with or without a key, may bill it again, so treat it as failed for this input unless a second charge is acceptable. A key on every call avoids this case. |
| 5 | `RecoverableJobError` (`DeadlineExceededError`) without `job_id`, or a lost job submission: an `APIConnectionError` or status error with `retryable` `True`, without `job_id`, in `phase` `"job_submit"` or `"submit"` | By `phase`: `"prepare"` or `"concurrency_wait"`: nothing was sent; repeat the call as made. `"sync_submit"`: row 4 under the default, or repeat the call under `sync_replay="always"`. Any other phase: when `is_transient` is `True` (see the [`is_transient` table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#machineraerror)), repeat the call as made; otherwise repeat the identical call with `idempotency_key=error.operation_key`, which replays the job if it was accepted, because a lost submission's job may have been accepted and repeating the call as made could submit and bill a second one. |
| 6 | Any other error with `is_transient` `True` | Repeat the identical call, with the same key, after a pause. The SDK has already retried it. |
| 7 | Anything else (`is_transient` `False`) | Permanent for this input, except for the [caller-keyed recovery exception](https://github.com/machinera-labs/machinera-python/blob/main/api.md#machineraerror). This covers `AuthenticationError`, `PermissionDeniedError`, `BadRequestError`, `UnprocessableEntityError`, `PayloadTooLargeError`, `NotFoundError`, `ConflictError`, `UploadError` and `IntegrityError`, a status error that `is_transient` does not make transient (a bare 500 under the default `sync_replay="never"`, for example), a 5xx on the synchronous route under that default (it may have run; see row 4), and `APIConnectionError` from a closed client, from local file I/O such as a missing path, or with `retryable` `False`. Record the class name, `code`, and `request_id`. A new key is a new submission and a new charge. |

A status error's `retryable` is the service's flag when it sends one, otherwise the
SDK's code table, otherwise `True` only for HTTP 429, 502, 503, and 504. Whether an
error is transient follows the first matching row of the
[`is_transient` table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#machineraerror), which also covers the synchronous route
and a lost job submission. An error built from a service
response, including a failed job, always has `retryable` `True` or `False`, never `None`;
a failed job whose code the SDK does not know, sent without the service's flag, has
`False`.

With your own `idempotency_key`, repeating the identical call is always safe: the
service answers a repeated key with the original job, running or finished, and never
runs or bills it twice. `is_transient` then says only whether the repeat may succeed.
A keyed job submission that was sent but ended without a `job_id` in a non-transient
5xx or `APIResponseValidationError` may have been accepted, so repeating it with the
same key, within your own retry limit, may still return the job.

Bound rows 3, 5, and 6 with your own budget, for example 3 attempts with pauses of 2,
4, and 8 seconds. `str(error)` ends with `(request_id: ...)` when it is known; read
`request_id` with `getattr(error, "request_id", None)`, because a `MachineraError`
that is not an `APIError` has none.

For a failed job's codes, retryability, and meanings, see the
[job-code table](https://github.com/machinera-labs/machinera-python/blob/main/api.md#joberror).

## Evaluation harnesses

Most harnesses call the provider from several threads and retry a failed sample unless
it raises a permanent error. The provider below applies the
[failure table](https://github.com/machinera-labs/machinera-python/blob/main/README.md#failure-handling) for such a harness, for files of any size: it keys
every call, so a harness retry never submits the sample twice, and it resumes a job it
has seen instead of submitting again (row 3).

```python
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from machinera import (
    APIResponseValidationError,
    InternalServerError,
    Machinera,
    MachineraError,
    TerminalJobError,
    TranscriptionInterrupted,
)

RUN = "my-eval-2026-10-05"  # a new value transcribes every sample again
MODEL, FORMAT = "transcribe-v1", "json"
BUDGET = 300  # seconds per attempt, counted from before hashing; hashing is not cut short


class MachineraProvider:
    def __init__(self) -> None:
        self.client = Machinera()
        self.digests: dict[tuple[str, int, int], str] = {}  # (path, size, mtime) -> hash
        self.hashing: dict[str, threading.Lock] = {}  # one thread hashes a path at a time
        self.jobs: dict[str, str] = {}  # key -> a job_id an earlier error carried

    def transcribe(self, audio: str | bytes, language: str = "en") -> str:
        end = time.monotonic() + BUDGET
        try:
            key = self.operation_key(audio, language)
            return self.call(audio, language, key, end)
        except TranscriptionInterrupted as exc:
            if exc.job_id is not None:
                self.jobs[key] = exc.job_id
            raise KeyboardInterrupt from exc  # it is also an Exception, which a harness retries
        except MachineraError as exc:
            job_id = getattr(exc, "job_id", None)
            if job_id is not None and not isinstance(exc, TerminalJobError):
                self.jobs[key] = job_id  # later attempts resume it, which never charges again
            if worth_retrying(exc):
                raise  # the harness's retry repeats or resumes; neither bills twice
            raise PermanentError(str(exc)) from exc
        except (ValueError, TypeError, OSError) as exc:  # such as a missing path
            raise PermanentError(str(exc)) from exc

    def call(self, audio: str | bytes, language: str, key: str, end: float) -> str:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("attempt budget spent")  # the harness retries the sample
        job_id = self.jobs.get(key)
        if job_id is not None:  # poll the job; never upload or submit it again
            return self.client.resume(job_id, response_format=FORMAT, deadline=remaining).text
        name = None if isinstance(audio, bytes) else "upload" + Path(audio).suffix.lower()
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
    if exc.is_transient:
        return True
    if isinstance(exc, TerminalJobError) or 400 <= status < 500:
        return False
    if getattr(exc, "job_id", None) is not None:
        return True  # a status read failed; the job is unaffected (row 3)
    submitting = getattr(exc, "phase", None) in ("job_submit", "submit")
    return submitting and isinstance(exc, (InternalServerError, APIResponseValidationError))
```

`PermanentError` is your harness's do-not-retry exception, and `audio` is a path or the
file's bytes. A path needs a supported suffix. One client per provider object, shared by
the harness's threads, is fine.

- **Billed once:** the [keyed-call guarantee](https://github.com/machinera-labs/machinera-python/blob/main/api.md#machineraerror)
  means a harness retry and two threads given the same path (the same bytes and suffix;
  see **Keys** below) under the same `RUN` and options both reach one job. The recipe's
  `worth_retrying` follows [Failure handling](#failure-handling), including its
  caller-keyed recovery exception. A `job_id` is remembered unless the job failed,
  including before an interruption is re-raised; later attempts `resume` it per row 3.
  The harness's retry limit bounds recovery. Errors that `worth_retrying` rejects
  become `PermanentError`; see rows 2 and 7. Whether a failed job itself was charged
  is decided by the service; the SDK cannot tell.
- **Ctrl-C:** `TranscriptionInterrupted` is a `KeyboardInterrupt` but also an
  `Exception`, so a harness that retries every `Exception` would retry it. Re-raising a
  plain `KeyboardInterrupt` stops the run. Python delivers Ctrl-C only to the main
  thread: a call running in another thread continues until it ends or reaches its
  deadline.
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
  the same `RUN` to collect it, or raise `BUDGET` for long files, or for large files on
  a slow connection, whose upload restarts on every attempt, as does the SDK's own
  hashing pass over a staged file, inside the SDK's `deadline`.
- **Latency:** each sample's time includes job submission, waiting to start, and polling every
  [`RetryPolicy.poll_interval`](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retrypolicy). `inference_seconds` on the result,
  when the service returns it, is the service's own processing time. A key from an
  earlier run replays that run's job, so use a new `RUN` for each run you time. A call
  without a key takes the faster synchronous route, but a harness retry of it is a new
  submission, which may be billed again if the first one ran.
- **Keys:** paths with the same run, model, language, format, suffix, and bytes share a
  key, whatever the rest of their names, and so do identical raw bytes, so a later or
  concurrent sample gets the earlier one's job and result instead of a second charge.
  A submission repeating a running job's key returns that job, and the SDK polls it. A
  path is sent as `upload.<suffix>`; the suffix stays in the key because it sets the
  media type sent with the file, while bytes are named from their content. So every
  submission under a key is the identical request, and the same bytes given as
  `clip.m4a`, `clip.mp4`, and raw bytes are three keys and three jobs. The service keeps
  a key's job for replay for a limited period (see
  [Recovery after a restart](https://github.com/machinera-labs/machinera-python/blob/main/README.md#recovery-after-a-restart)); a retry after that fails with
  `ConflictError`, code `idempotency_replay_unavailable`, which the recipe makes
  permanent.
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
[Failure handling](#failure-handling) rules:

```python
import hashlib
import time

from machinera import (
    APIError,
    APIResponseValidationError,
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
    key = hashlib.sha256(
        f"my-eval:{sample_id}:{options}:{content.hexdigest()}".encode()
    ).hexdigest()
    job_id = None
    for attempt in range(3):
        try:
            if job_id is not None:
                return client.resume(job_id, response_format="json", deadline=DEADLINE).text
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
            if not error.is_transient:
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

An encoded file above `Limits.job_inline_body_bytes` (52,428,800 bytes) is uploaded to
storage and then submitted as a job, automatically. The service sets the upload size
limit, the upload expiry window, and the maximum audio duration (code
`audio_duration_exceeded`); see the [public limits](https://api.machinera.com/docs/limits).

- Do not change the file during the call or its recovery; a change raises
  `IntegrityError`.
- Set `deadline` for the whole job, including waiting to start. A deadline only stops waiting:
  the job keeps running and `resume(error.job_id)` picks it up.
- `upload_expired` (`UploadError`, HTTP 410): the upload window passed and the key can no
  longer be used.
- `upload_already_bound` or `idempotency_payload_mismatch`: the key was reused with
  different input or options; fix the key derivation.

## Recovery after a restart

Save an operation key **before** calling, then save `job_id` from any error.
These two fragments use application-provided durable storage: implement
`save_recovery_state(key=..., job_id=...)`, and load its fields into `saved_key` and
`saved_job_id` before running the restart fragment. Complete programs are linked below:

```python
import uuid

from machinera import APIError, Machinera

key = uuid.uuid4().hex
save_recovery_state(key=key, job_id=None)  # your own durable storage

with Machinera() as client:
    try:
        result = client.transcribe_file("recording.wav", model="transcribe-v1", idempotency_key=key)
    except APIError as error:
        if error.job_id is not None:  # never replace a saved job_id with None
            save_recovery_state(key=key, job_id=error.job_id)
        raise
```

After a restart:

```python
with Machinera() as client:
    if saved_job_id is not None:
        result = client.resume(saved_job_id, response_format="json")  # polls only
    else:
        result = client.transcribe_file(  # replays the original submission
            "recording.wav", model="transcribe-v1", idempotency_key=saved_key
        )
```

- This covers inline and staged files alike. Reuse a key only for the identical
  request: same file bytes, options, credential, and endpoint.
- `resume` has two forms. `resume(job_id)` only polls and defaults to
  `response_format="verbose_json"`. `resume(file=..., model=..., operation_key=...)`
  replays a staged submission and defaults to `"json"`. Pass the original format to
  either.
- A job that failed (`TerminalJobError`) stays failed: the same key replays the failure.
- The service keeps results and replays for a limited time, and recovery does not
  extend it; the periods are in the API's
  [result and idempotency retention](https://api.machinera.com/docs/errors#retention)
  section. A key replays its job while it is retained. A job still running when its
  key's period ends can no longer be replayed by key, so keep its `job_id` and use
  `resume(job_id)`. After that, recovery fails with a permanent error such as `ConflictError`,
  code `idempotency_replay_unavailable`; a new key is a new submission and a new charge.
- The SDK never cancels a server job and keeps no journal on disk.

Complete programs: [blocking](https://github.com/machinera-labs/machinera-python/blob/main/examples/submit_and_resume.py),
[asyncio](https://github.com/machinera-labs/machinera-python/blob/main/examples/async_submit_and_resume.py), and
[large file](https://github.com/machinera-labs/machinera-python/blob/main/examples/transcribe_large_file.py).

## Deadlines and retries

Every call is bounded by a total deadline that covers preparation, waiting for a
concurrency slot, requests, retry pauses, and polling, subject to [async local I/O](https://github.com/machinera-labs/machinera-python/blob/main/api.md#asyncmachinera)
and [recipe hashing](#evaluation-harnesses) caveats. Pass `deadline=seconds` to
`transcribe_file`, `transcribe_url`, or `resume` to change it for one call. See the
canonical [polling and recovery rule](https://github.com/machinera-labs/machinera-python/blob/main/api.md#retrypolicy).

| Setting | Default | Meaning |
| --- | --- | --- |
| `TimeoutPolicy.connect` | `5` | Connect inactivity, seconds. |
| `TimeoutPolicy.write` | `600` | Write inactivity, seconds. |
| `TimeoutPolicy.read` | `600` | Read inactivity, seconds. |
| `TimeoutPolicy.pool` | `600` | Seconds to wait for a connection. |
| `TimeoutPolicy.poll_request` | `30` | Seconds per status read. |
| `TimeoutPolicy.deadline` | `3600` | Total seconds per call. |
| `RetryPolicy.max_attempts` | `3` | Attempts per step, including the first. |
| `RetryPolicy.initial_delay` | `0.5` | First retry pause, seconds. |
| `RetryPolicy.max_delay` | `8` | Longest computed pause, seconds. |
| `RetryPolicy.poll_interval` | `1` | Seconds between status reads. |
| `RetryPolicy.max_polls` | `None` | Optional cap on status reads. |
| `Limits.sync_inline_body_bytes` | `26,214,400` | Largest synchronous request. |
| `Limits.job_inline_body_bytes` | `52,428,800` | Largest inline durable job. |
| `Limits.descriptor_bytes` | `65,536` | Largest URL or job descriptor. |

The SDK retries network failures and the status errors that the rule under
[Failure handling](https://github.com/machinera-labs/machinera-python/blob/main/README.md#failure-handling) makes transient, only when resending is safe or
`sync_replay="always"` allows it. Pauses are
`min(initial_delay * 2**retry, max_delay)` times a random factor in `[0.75, 1]`, and a
`Retry-After` header sets a minimum. A pause that cannot finish before the deadline
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
