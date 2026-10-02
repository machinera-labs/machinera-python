# Machinera Python SDK

Python client for the Machinera speech-to-text API, with a blocking client
(`Machinera`) and a native asyncio client (`AsyncMachinera`). Requires Python 3.10
or newer.

## Quick start

```sh
pip install machinera
export MACHINERA_API_KEY="your-api-key"
```

```python
from machinera import Machinera

with Machinera() as client:
    result = client.transcribe_file("recording.wav", model="transcribe-v1")
    print(result.text)
```

`transcribe-v1` is the model ID. Each call waits for the finished transcript and
returns a `TranscriptionResult`. To transcribe audio that is already online, pass
a direct audio URL instead:

```python
with Machinera() as client:
    result = client.transcribe_url("https://audio.example/recording.wav", model="transcribe-v1")
```

Supported formats are the file suffixes in `machinera.SUPPORTED_MEDIA_SUFFIXES`;
content without a supported name is identified from its MIME type or container
signature (see [File inputs](#file-inputs)). Large files are uploaded automatically (see
[Large files](#large-files)). Live calls submit audio for transcription and may
incur usage charges.

### Asyncio

`AsyncMachinera` takes the same configuration and has the same methods; await them:

```python
import asyncio

from machinera import AsyncMachinera


async def main() -> str:
    async with AsyncMachinera() as client:
        result = await client.transcribe_file("recording.wav", model="transcribe-v1")
        return result.text


text = asyncio.run(main())
```

## Recover an interrupted transcription

A call can end before it returns a transcript (a deadline, Ctrl+C, a network
failure, or task cancellation) while the service keeps working on the job. The SDK
never cancels a server job or deletes its input. To pick the work up again, save
an operation key **before** calling and pass it as `idempotency_key`:

```python
import uuid

from machinera import APIError, Machinera

key = uuid.uuid4().hex
save_recovery_state(key=key, job_id=None)  # your own durable storage

with Machinera() as client:
    try:
        result = client.transcribe_file("recording.wav", model="transcribe-v1", idempotency_key=key)
    except APIError as error:  # includes DeadlineExceededError and TranscriptionInterrupted
        save_recovery_state(key=key, job_id=error.job_id)
        raise
```

Later, even from a new process, continue from the saved state:

```python
with Machinera() as client:
    if saved_job_id is not None:
        result = client.resume(saved_job_id)  # polls the accepted job; never submits
    else:
        result = client.transcribe_file(  # replays the same submission
            "recording.wav", model="transcribe-v1", idempotency_key=saved_key
        )
```

The rules behind this recipe:

- **Keyed calls are durable jobs.** An `idempotency_key` (or `transport="job"`)
  submits a server job that can be resumed. Without one, a file whose encoded
  request fits `Limits.sync_inline_body_bytes` is sent as a single synchronous
  request. If that request's response is lost, the SDK raises
  `AmbiguousSubmissionError`: the transcription may already have run, so reconcile
  (check what you already received or were billed for) instead of resubmitting.
- **Reuse a key only for the identical request:** same file bytes, metadata,
  options, credentials, and endpoint. Use a fresh key for every independent
  transcription. Every `APIError` carries the call's key in `operation_key`, including
  the random key generated when you passed none.
- **Once a job ID is known, use `resume(job_id)`.** It only polls and never creates a
  replacement job. `get_job(job_id)` reads a single status snapshot (`JobSnapshot`).
- **A failed job raises `TerminalJobError`** and is never resubmitted automatically.
- **Save results promptly.** The service retains results for a limited time, and
  recovery does not extend it. An expired result or replay requires reconciliation,
  not a new submission disguised as a retry. The SDK keeps no journal on disk.

What to do after a failure; use the first row that matches:

| Exception | Meaning | Next step |
| --- | --- | --- |
| `TerminalJobError` | The job failed. | Inspect `code` before deciding on a new job. |
| `AmbiguousSubmissionError` | An unkeyed synchronous request may have run. | Reconcile; do not resubmit. |
| `AuthenticationError`, `PermissionDeniedError` | The credential was refused. | Check the API key and its access. |
| `BadRequestError`, `UnprocessableEntityError`, `PayloadTooLargeError` | The request is invalid. | Correct the input. |
| `IntegrityError`, other `UploadError` | The file changed, or storage or the service refused the upload. | Keep the file unchanged and see [Large files](#large-files). |
| `TranscriptionInterrupted` with `ambiguous` true | An unkeyed synchronous request was interrupted and may have run. | Reconcile; do not resubmit. |
| Any other `APIError` except `RateLimitError` with `phase == "sync_submit"` | An unkeyed synchronous request failed after it may have run. | Reconcile; do not resubmit. |
| `NotFoundError`, `ConflictError` | The job is unknown to this credential and endpoint, or the service cannot replay the key. | Reconcile; do not resubmit under a new key. |
| Any other `APIError` with `job_id` set | The job was accepted and may still be running. | `resume(job_id)` |
| `DeadlineExceededError`, `TranscriptionInterrupted`, `APIConnectionError`, `APIResponseValidationError`, `RateLimitError`, `InternalServerError` | Admission was not confirmed. | Repeat the identical call with `idempotency_key=error.operation_key`, after a pause for rate limits and server errors. |
| Any other `APIError` | The service refused the operation. | Inspect `code` and reconcile before any new submission. |

With `AsyncMachinera`, task cancellation propagates the original
`asyncio.CancelledError` with `operation_key`, `upload_id`, `phase`, `job_id`, and
`last_status` attached once the call has started. Read them with
`getattr(error, "job_id", None)` inside the coroutine that directly awaits the SDK
method, save them, and re-raise. On Python 3.10, a task boundary can replace the
cancellation exception, so another task may not see these attributes.

For files above the inline limit, see [Large files](#large-files). Complete
recovery programs: [blocking](https://github.com/machinera-labs/machinera-python/blob/main/examples/submit_and_resume.py),
[asyncio](https://github.com/machinera-labs/machinera-python/blob/main/examples/async_submit_and_resume.py), and
[large file](https://github.com/machinera-labs/machinera-python/blob/main/examples/transcribe_large_file.py).

## Configuration

All constructor arguments are keyword-only. Explicit non-`None` values take
precedence over the environment, and invalid values raise `ValueError` without
falling back.

- `api_key` defaults to `MACHINERA_API_KEY`. A missing or empty credential raises
  `ValueError` before any request.
- `base_url` defaults to `MACHINERA_BASE_URL`, then `https://api.machinera.com/v1`.
  An origin without `/v1` is accepted and normalized.
- `timeout`, `max_retries`, and `retry_policy` are described under
  [Timeouts and deadlines](#timeouts-and-deadlines) and [Retries](#retries).
- `default_headers` is copied and merged into API requests. It can override
  non-reserved defaults such as `User-Agent`; invalid headers and attempts to set a
  header the SDK owns (`_RESERVED` in
  [`_files.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_files.py))
  raise `ValueError` before any request.
- `transport="job"` sends every call as a durable job. The default, `"auto"`, picks
  synchronous, inline durable-job, or staged submission by encoded size.
- `limits=Limits(...)` changes the encoded request sizes behind that choice; it does
  not change service limits. Durable jobs above `Limits.job_inline_body_bytes` use
  staged uploads (see [Large files](#large-files)).
- `max_concurrency` optionally bounds the number of active calls; waiting for a slot
  counts toward the call's deadline.

No dotenv files are loaded and no endpoints are probed. Client configuration is
immutable. One `Machinera` client can be shared by threads, and one
`AsyncMachinera` client by tasks on one event loop. `close()` / `aclose()` (or
leaving the `with` block) waits for active calls and closes only an HTTP client the
SDK created.

## File inputs

`transcribe_file` accepts a path (`str` or `PathLike`; bare strings always mean
paths), `bytes`, a seekable binary handle, or a tuple
`(filename, content[, content_type[, headers]])` whose content is any of those.
Content is read from the handle's current offset to its end and streamed without
decoding or conversion.

```python
with Machinera() as client:
    result = client.transcribe_file(audio_bytes, model="transcribe-v1", filename="clip.wav")
    result = client.transcribe_file(
        ("clip.flac", audio_bytes, "audio/flac", {"X-Part-Label": "recording"}),
        model="transcribe-v1",
    )
```

The upload name is resolved in this order:

1. An explicit `filename=` or tuple filename; an unsupported suffix raises
   `ValueError`. Conflicting non-`None` tuple and keyword values also raise.
2. The basename of a path or handle name, when its suffix is supported. Numeric
   handle names and unsupported suffixes are ignored.
3. For unnamed content, a supplied MIME type, mapped by `_MIME_SUFFIXES` in
   [`_files.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_files.py).
4. Otherwise, a container signature in the leading bytes, read by
   `Multipart.prepare` in [`_multipart.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_multipart.py) and
   matched by `sniff` in `_files.py`.

Content that cannot be identified raises `ValueError` before any request. Names,
content types, and part headers are validated against header injection, and part
headers cannot carry credentials or cookies or replace multipart framing.

Binary handles stay open, and their offset is restored after an ordinary
completion. Do not modify a file during a call or share one handle between
simultaneous calls.

With `Machinera`, a deadline or interruption can end the call while a read or seek on
your handle is still blocked. Call `error.wait_for_file_release(timeout)` on the
raised `APIError` and require `True` before reusing, seeking, or closing the handle
(`timeout=0` only checks). The SDK does not restore the offset in that case.

With `AsyncMachinera`, task cancellation raises `asyncio.CancelledError`, which has
no `wait_for_file_release`. None is needed: before the cancellation propagates, the
SDK awaits any in-flight file operation and restores a caller-owned handle's
original offset (or closes a file it opened). See the
[asyncio cleanup contract](https://github.com/machinera-labs/machinera-python/blob/main/api.md#asyncmachinera).

## Timeouts and deadlines

Every call is bounded by a total deadline; the defaults are declared by the
[`TimeoutPolicy` fields](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_types.py).
The deadline is monotonic and covers preparation, waiting for a concurrency slot,
requests, retry sleeps, and polling. Each poll request also has its own limit.

| `timeout=` | Effect |
| --- | --- |
| omitted | Inherit the client's policy (the method) or `TimeoutPolicy()` (the constructor). |
| seconds | Set the connect, write, read, and pool phases to that value. |
| `httpx.Timeout(...)` | Copy its four phases, including disabled (`None`) ones. |
| `None` | Disable HTTP phase limits only. |
| `TimeoutPolicy(...)` | Replace all six values. |

The scalar, `httpx.Timeout`, and `None` forms keep the inherited poll-request and
total-deadline bounds, so calls stay bounded. `transcribe_file`, `transcribe_url`,
and `resume` also accept `deadline=seconds` to override the total budget for one
call. HTTP phase values follow
[httpx timeout semantics](https://www.python-httpx.org/advanced/timeouts/).

A deadline raises `DeadlineExceededError` (or `AmbiguousSubmissionError` for an
unkeyed synchronous request that may have started) and keeps the operation key and
any accepted job ID for [recovery](#recover-an-interrupted-transcription).

## Retries

Retry defaults are declared by the
[`RetryPolicy` fields](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_types.py).
`max_retries=n` allows `n + 1` attempts per replay-safe step, and `0` disables
retries; pass `retry_policy=RetryPolicy(...)` instead for full control (supplying
both raises `ValueError`). Polling has its own interval and count budget.

Network failures, HTTP 429/502/503/504, and service refusals marked retryable are
retried only when replaying the request is safe. Backoff is
`min(initial_delay * 2**retry_index, max_delay)` times a random factor in
`[0.75, 1]`; a `Retry-After` header sets a minimum wait, and a wait that cannot
finish before the deadline raises `DeadlineExceededError`. Retries resend the same
encoded body and operation key. Authentication and permission failures and failed
jobs are never retried. The service's `retryable` flag takes precedence over the
SDK's error-code table, which in turn takes precedence over the status-code rule.

## Results

`transcribe_file`, `transcribe_url`, and `resume` return `TranscriptionResult`;
`get_job` returns `JobSnapshot`. Both are frozen Pydantic models with attribute
access. Unknown response fields are kept in `raw`, and malformed responses raise
`APIResponseValidationError` rather than a Pydantic `ValidationError`.
`JobSnapshot.status` is a plain string, so a status added by the service later still parses; while
polling, any status other than `queued`, `processing`, or `completed` raises
`TerminalJobError`.

```python
with Machinera() as client:
    snapshot = client.get_job(saved_job_id)
    print(snapshot.status)
```

`result.text` preserves whitespace and empty strings exactly. `output` follows the
requested `response_format`: `to_json()` returns the text and any usage,
`to_text()` the exact text, and `to_verbose_json()` every returned field. Durable
jobs return verbose fields whatever format was requested; a synchronous response
contains only what the service sent. `elapsed_seconds` is the whole call as seen by
the client, including preparation, retries, and polling.

## Errors

Every SDK failure derives from `MachineraError`, and every API or transport failure
from `APIError`. Local argument errors raise `ValueError` or `TypeError` before any
request. `APIError` carries `status_code` (alias `status`), `code`, `retryable`,
`request_id`, a sanitized `body`, and the recovery context `operation_key`,
`job_id`, `upload_id`, `phase`, and `last_status`.

HTTP failures raise `APIStatusError` subclasses (`BadRequestError`,
`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `ConflictError`,
`PayloadTooLargeError`, `UnprocessableEntityError`, `RateLimitError`,
`InternalServerError`). `APIConnectionError` and its subclass `APITimeoutError`
cover transport failures. `APIResponseValidationError` means the service returned a
body the SDK cannot use and is never retried automatically. A failed job whose
uploaded file did not match raises `TerminalIntegrityError`, which is both an
`IntegrityError` and a `TerminalJobError`. `TranscriptionInterrupted` is both an
`APIError` and a `KeyboardInterrupt`. The
[recovery table](#recover-an-interrupted-transcription) gives the next step for each
class, the full hierarchy is in the
[API reference](https://github.com/machinera-labs/machinera-python/blob/main/api.md#exception-subclasses),
and [`handle_errors.py`](https://github.com/machinera-labs/machinera-python/blob/main/examples/handle_errors.py)
prints guidance for a failed call.

Exceptions and their chains never contain service free text, raw HTTP objects,
URLs, HTML, audio, or transcripts. Protect credentials, source URLs, operation
keys, audio, and transcripts in your own logging; see [Logging](#logging) for the
SDK's own records.

## Logging

The SDK logs to the standard `logging` logger named `machinera`: retry decisions at
`DEBUG` (method, path template, HTTP status, error code, attempt, delay, and request
ID) and job status changes while polling at `INFO` (job ID and status). Records
never contain URLs with query strings, credentials, file paths, or transcript text.
Set `MACHINERA_LOG=debug` or `MACHINERA_LOG=info` to set that level when a client is
created; a stderr handler is attached only if the logger has none. Configure the
logger directly for anything else.

## Large files

When the encoded request exceeds the inline limit for durable jobs
(`Limits.job_inline_body_bytes`, which defaults to the SDK's staged-upload threshold
`STAGED_UPLOAD_THRESHOLD_BYTES` in
[`_types.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_types.py)),
`transcribe_file` uploads the file to storage and then submits a job that references
it. This happens with either `transport` setting; a body exactly at the limit is
still sent inline. The service supplies the upload size limit and expiry window. If
the service refuses the upload with `staged_uploads_unavailable` before granting it,
the SDK submits the same body once as an inline durable job under the same operation
key, provided it fits the service's inline limit; a larger file fails with that
error. Byte limits are separate from audio duration and account limits.

File preparation and streaming are implemented in
[`_multipart.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_multipart.py),
and the staged flow and its inline fallback in
[`_core.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_core.py).
Changes to the file's size or content raise `IntegrityError`. Keep the input
unchanged through retries and recovery.

To recover a staged file whose job ID is not yet known, pass the same file, options,
and key to `resume`. It replays initialization, upload, and submission
idempotently and never generates a new key:

```python
with Machinera() as client:
    result = client.resume(
        file="recording.flac",
        model="transcribe-v1",
        operation_key=saved_key,
        upload_id=saved_upload_id,  # optional, from error.upload_id
    )
```

Pass the original `response_format` and `language` as well: when omitted, recovery
before admission uses `"json"` and no language hint. Options that differ from the
original call raise an error with code `idempotency_payload_mismatch` instead of
returning the earlier result. Once a job ID is known, `resume(saved_job_id)` only
polls.

Storage failures raise `UploadError`, whose `storage_code` holds only a sanitized
storage error code. Service errors whose `code` starts with `upload_` also raise
`UploadError`, with `status_code` set, except rate limits, which raise
`RateLimitError`. An expired upload grant is refreshed automatically within the fixed
upload window, and an incomplete upload is retried once. Integrity mismatches,
expired or already-bound uploads, and key mismatches need reconciliation; an expired
upload cannot be reopened under the same key.

## HTTP client

The SDK chooses its HTTP clients in `Core.__init__` in
[`_core.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_core.py).
An injected `http_client` or custom `transport` carries every request, and its own
pool behavior applies. Otherwise uploads and submissions use a client whose pool
(`EXCHANGE_POOL`) keeps no idle connections, so a connection closed at a deadline is
never reused, and job status reads use a separate client with `POLL_POOL`. The
blocking client builds that status-read transport with `keepalive_transport` in
[`_io.py`](https://github.com/machinera-labs/machinera-python/blob/main/src/machinera/_io.py),
which falls back to `EXCHANGE_POOL` when it cannot install its connection hook.
Clients the SDK creates ignore environment proxies, do not follow redirects, and use
httpx's default of [zero transport retries](https://www.python-httpx.org/advanced/transports/).
`close()` and `aclose()` close only the clients the SDK created.

An injected `http_client=httpx.Client(...)` (or `httpx.AsyncClient` for
`AsyncMachinera`) keeps its own pool, proxies, URL mounts, and lifetime, and is used
for both API calls and storage uploads. Upload requests are sent with `auth=None`
and without redirects, so the client's default headers, authentication, and cookies
are not added to them. Request and response hooks still run for uploads: they must
keep the upload headers, add no credentials or cookies, and must not log the signed
upload URL. The SDK suppresses httpx's own request log for uploads.

For offline tests, pass an `httpx.BaseTransport` (or `httpx.AsyncBaseTransport`) as
`transport`; `clock`, `sleeper`, `wall_clock`, and `random_source` are injectable
too.

When a call is cancelled, the SDK closes the in-flight connection or response stream
and makes no further requests. A custom transport that cannot be interrupted may
finish in a background daemon thread, and its late result is discarded. In
`AsyncMachinera`, file hashing, inspection, and reads run in worker threads, so slow
local I/O can delay cancellation without blocking the event loop.

## Versioning and requirements

Releases use semantic versions. During `0.x`, minor releases may change the public
API, and patch releases contain compatible fixes; from `1.0`, incompatible changes
require a major release. Read the
[changelog](https://github.com/machinera-labs/machinera-python/blob/main/CHANGELOG.md)
when upgrading, and pin an exact version where installs must be reproducible.

The SDK is tested on Python 3.10 through 3.14. Its runtime dependencies are declared
in [`pyproject.toml`](https://github.com/machinera-labs/machinera-python/blob/main/pyproject.toml);
it needs no audio decoder or conversion tools.

See the [API reference](https://github.com/machinera-labs/machinera-python/blob/main/api.md),
the [runnable examples](https://github.com/machinera-labs/machinera-python/blob/main/examples/README.md),
[CONTRIBUTING.md](https://github.com/machinera-labs/machinera-python/blob/main/CONTRIBUTING.md)
for development, and
[SECURITY.md](https://github.com/machinera-labs/machinera-python/blob/main/SECURITY.md)
for reporting vulnerabilities.
