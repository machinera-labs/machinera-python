# API reference

Import every documented symbol from `machinera`. The SDK has a blocking client
(`Machinera`) and a native asyncio client (`AsyncMachinera`). Separately, a
*durable job* is a server-side transcription job that can be resumed by ID;
`transport="job"` and `Limits.job_inline_body_bytes` refer to durable jobs. This
file is the authority; the README summarizes it, and its failure-handling table is
identical to [Failure handling](#failure-handling).

## Clients

### `Machinera`

```python
Machinera(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    max_retries: int = ...,
    default_headers: Mapping[str, str] | None = None,
    timeout: float | httpx.Timeout | TimeoutPolicy | None = ...,
    retry_policy: RetryPolicy | None = None,
    limits: Limits | None = None,
    max_concurrency: int | None = None,
    transport: Literal["auto", "job"] | httpx.BaseTransport = "auto",
    sync_replay: Literal["never", "always"] = "never",
    http_client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], float] = time.time,
    random_source: Callable[[], float] = random.random,
) -> Machinera
```

All constructor arguments are keyword-only. In this reference, `= ...` marks a
default listed under [Defaults](#defaults); it is not a value to pass.
Omitted `timeout` and `max_retries` use the [defaults](#defaults).

| Parameter | Meaning |
| --- | --- |
| `api_key` | Explicit non-`None` credential, otherwise `MACHINERA_API_KEY`. Must be nonempty visible ASCII without whitespace. Missing or invalid credentials raise `ValueError`. |
| `base_url` | Explicit non-`None` endpoint, otherwise `MACHINERA_BASE_URL`, otherwise `https://api.machinera.com/v1`. Requires an HTTP(S) origin, optionally ending in `/v1`, with no credentials, query, or fragment. Normalized to `/v1`. |
| `max_retries` | Nonnegative integer; `n` means `n+1` attempts per replay-safe step, including the initial request. Omission uses `RetryPolicy()`. Cannot be supplied together with `retry_policy`. |
| `default_headers` | String mapping copied into immutable client configuration and merged into API requests. Caller values override non-reserved SDK defaults. |
| `timeout` | Omission uses `TimeoutPolicy()`. Scalar, `httpx.Timeout`, and explicit `None` configure HTTP phases while retaining default poll-request and total-deadline bounds. A policy supplies all settings. See timeout forms below. |
| `retry_policy` | Advanced retry and poll configuration; `None` constructs `RetryPolicy()`. |
| `limits` | `None` constructs `Limits()`. |
| `max_concurrency` | Positive integer limiting active operations on this client, or `None` for no client semaphore. Waits count toward the deadline. Other clients are not limited, so a client built per call gets no cross-client cap. |
| `transport` | Two unrelated meanings share this parameter. The route mode: `"auto"` selects inline sync or a durable job by encoded size, falling back to a durable job when the synchronous route refuses the request before starting work (see `transcribe_file`); `"job"` always selects a durable job. An HTTP transport: a custom `httpx.BaseTransport` replaces the HTTP layer and fixes the route mode at `"auto"`; pass `idempotency_key` to force durable jobs with it. |
| `sync_replay` | Exactly `"never"` (the default) or `"always"`; any other value raises `ValueError`. See [Synchronous replay](#synchronous-replay). |
| `http_client` | Optional caller-owned `httpx.Client`; cannot be combined with a custom transport. Its pool settings and lifetime remain the caller's responsibility. |
| `clock`, `sleeper` | Monotonic time and delay functions, injectable for tests. |
| `wall_clock` | Wall time for HTTP-date `Retry-After` parsing. |
| `random_source` | Random sample in `[0, 1]` for retry jitter. |

`MACHINERA_LOG=debug` or `MACHINERA_LOG=info`, read at construction, sets the level
of the `machinera` logger and attaches a stderr handler if it has none; other
values leave logging unchanged.

Explicit invalid values fail without environment fallback. No dotenv discovery
or endpoint probing occurs. Header names and values are validated before HTTP.
Headers the SDK owns cannot be overridden: `Authorization`, `Host`, `Content-Length`,
`Content-Type`, `Idempotency-Key`, `Transfer-Encoding`, and `X-Content-MD5`, matched
case-insensitively; supplying one raises `ValueError`. Other defaults such as
User-Agent can be overridden.

Raises `ValueError` for invalid configuration or conflicting retry controls;
`TypeError` for an unsupported timeout form or HTTP client type. Configuration is
immutable. Public attributes are `default_headers`, `base_url`, `timeout`,
`retry_policy`, `limits`, `max_concurrency`, `transport`, and `sync_replay`; `transport` is
`"auto"` when a custom HTTP transport is injected. Independent calls may share a
client across threads. Do not share a file cursor between concurrent calls.

An injected `http_client` or custom `transport` carries every API and storage
request, so its own pool behavior applies; an injected `http_client` also keeps its
proxy and URL-mount routing, pool configuration and ownership. Otherwise uploads and
submissions use connections that are never kept idle, and job status reads use a
separate pool that keeps idle connections for reuse.
Construction performs no I/O and creates no TLS context or pool. The pools behind
clients the SDK creates are shared process-wide by connection settings: blocking
pools last for the process and close
at exit, asyncio pools are per running event loop and close with the last client on
that loop, and a forked child never reuses its parent's pools. Closing a client
releases only its own handle. Injected clients and custom transports are never shared.
Clients the SDK creates, including one wrapping a custom transport, disable
environment proxies and automatic redirects. Storage requests are constructed directly
with `httpx.Request` and sent with `auth=None` and `follow_redirects=False`;
client default headers, authentication and cookies are not merged. Effective
timeouts and redirect refusal are applied per request. Injected client request and
response hooks run for storage too: hooks must preserve the grant headers, add no
credentials or cookies, and avoid logging signed URLs. Signed storage URLs are never logged.
Transport-level retries use the [httpx default of zero](https://www.python-httpx.org/advanced/transports/).

#### Synchronous replay

`sync_replay` controls only an unkeyed synchronous request that fails after its body
was sent: a lost response, a retryable status response, or any other 5xx the service
did not mark `retryable: false`.

| Value | Behavior |
| --- | --- |
| `"never"` | Raises `AmbiguousSubmissionError` for a lost response; error responses follow [`is_transient`](#machineraerror) and the [retry rules](#retrypolicy). |
| `"always"` | Replays the identical request under `RetryPolicy.max_attempts`, the same backoff, and the call deadline. Exhausted attempts raise the last `APIConnectionError`, `APITimeoutError`, or status error with `is_transient` `True`; an expired deadline raises `DeadlineExceededError`. A replayed request may run and be billed more than once. |

A 5xx marked `retryable: false` and other non-retryable responses are never replayed.
The [job-fallback refusals](#transcribe_file) use their eligible synchronous retries
before falling back. Keyed calls, durable jobs, polling, uploads, and the auto-mode
job fallback are unaffected.

#### Timeout forms and precedence

- Omitted method `timeout` inherits the resolved client policy.
- `timeout=seconds` sets connect, write, read, and pool phases to the same positive
  finite value, retaining the inherited poll-request and total deadline bounds.
- `timeout=httpx.Timeout(...)` copies its four phase values, including disabled
  (`None`) phases, retaining the inherited poll-request and total deadline bounds.
- `timeout=None` disables HTTP phase inactivity limits, retaining the inherited
  poll-request and total deadline bounds. Calls remain bounded, subject to [async local I/O](#asyncmachinera) and [recipe hashing](https://github.com/machinera-labs/machinera-python/blob/main/README.md#evaluation-harnesses) caveats.
- `timeout=TimeoutPolicy(...)` replaces all phase and elapsed budgets.
- `deadline=seconds`, where supported, overrides the resolved total call budget.

At construction, scalar/httpx/`None` forms inherit poll/deadline settings from
`TimeoutPolicy()`; on methods, they inherit those settings from the client.

#### Lifecycle

- `__enter__() -> Machinera`: return the client for a `with` block.
- `__exit__(*args: object) -> None`: call `close()`; exceptions are not suppressed.
- `close() -> None`: wait for active calls and close only the HTTP clients the SDK
  created.
  Cleanup of a cancelled exchange can be deferred until its pending I/O finishes.
  Subsequent operations raise `APIConnectionError`. Context exit does not wait for a
  cancelled input read.
- A client may be left unclosed: blocking connection pools close at process exit.
- When a call is cancelled, the SDK closes the in-flight connection or response stream
  and makes no further requests. A custom transport that cannot be interrupted may
  finish in a background daemon thread, and its late result is discarded.

#### `transcribe_file`

```python
transcribe_file(
    file: FileInput,
    *,
    model: str,
    filename: str | None = None,
    content_type: str | None = None,
    language: str | None = None,
    response_format: ResponseFormat = "json",
    timeout: float | httpx.Timeout | TimeoutPolicy | None = ...,
    deadline: float | None = None,
    idempotency_key: str | None = None,
) -> TranscriptionResult
```

`FileInput` is descriptive shorthand for the following accepted forms, not an
additional top-level export:

```python
FileContent = str | os.PathLike[str] | bytes | BinaryIO
FileInput = (
    FileContent
    | tuple[str | None, FileContent]
    | tuple[str | None, FileContent, str | None]
    | tuple[str | None, FileContent, str | None, Mapping[str, str]]
)
```

Bare strings mean paths. Read paths or seekable binary handles from their current
offset through EOF; bytes are accepted directly. Tuple items are filename,
content, optional content type, and optional part headers. `filename=` and
`content_type=` supply explicit metadata; conflicting non-`None` tuple and keyword
values raise `ValueError`.

Name resolution uses an explicit supported filename, then a safe basename from a
path or handle when its suffix is supported. Numeric handle names and unsupported
derived suffixes are ignored. Unnamed input uses a supplied recognized MIME type,
or bounded container-signature inspection when no type is supplied; see
[SUPPORTED_MEDIA_SUFFIXES](#supported_media_suffixes). A supplied MIME type is matched
case-insensitively, ignoring parameters; the recognized types are `audio/wav`,
`audio/x-wav`, `audio/flac`, `audio/x-flac`, `audio/ogg`, `application/ogg`,
`audio/mpeg`, `audio/mp4`, `audio/x-m4a`, `video/mp4`, `audio/webm`, and `video/webm`.
Without a type, the SDK reads at most the first 4096 bytes, recognizes a WAV, FLAC,
Ogg, MP3, MP4/M4A, or WebM container signature, and restores the handle offset.
Headerless audio such as raw PCM has no signature and fails. Content identified this
way is named `upload.<suffix>`.
Unsupported explicit names, unrecognized unnamed content, invalid headers, and
non-seekable handles fail locally. A supported name takes precedence over a supplied
MIME type, which is then not matched against known types. For unnamed input, a supplied MIME type that is
not recognized fails with `ValueError` and does not fall back to signature inspection.
Names, content types, and part headers cannot
inject multipart framing; part headers cannot contain credentials or cookies.

Use `model="transcribe-v1"`; other IDs fail with code `unknown_model`, and there is no
model-listing call. `language` accepts `None` (the default, which sends no hint) or
English (`en` or an `en-*` tag); other hints raise `ValueError` before HTTP. There is no prompt or vocabulary parameter.
The service's `non_english_audio` rejection is permanent: `UnprocessableEntityError`
at submission or `TerminalJobError` after acceptance; see the [public error reference](https://api.machinera.com/docs/errors#non_english_audio).
`response_format` chooses the result projection. Omitted `timeout` inherits the
client policy; other forms follow the precedence above. `deadline` overrides its
total elapsed budget with positive finite seconds. `idempotency_key` is optional,
1–128 visible ASCII characters, and selects a durable job when provided. Preserve
it before a call if restart recovery matters. Without it, a durable job gets a fresh
random key, so repeating the call submits, and bills, a new job.

Encoded multipart size includes metadata and boundary overhead. With
`transport="auto"` and no key, requests up to `limits.sync_inline_body_bytes` are
synchronous. Other requests use durable jobs: inline up to
`limits.job_inline_body_bytes`, staged above it with either transport setting.
Equality takes the smaller route. Initialization supplies
the authoritative per-call upload limits and fixed upload window. The file MD5 is
base64 of the raw 16-byte digest; SHA-256 is lowercase hex. Both are computed in
one fixed-chunk hashing pass. A fixed-length PUT streams original bytes with only
the grant headers (plus HTTP Host), without API credentials, cookies or redirects.
The caller content type takes precedence, then the resolved filename/container
type, otherwise `application/octet-stream`. Multipart part headers apply only to
inline requests. Initialization and job descriptors obey `limits.descriptor_bytes`.
Bytes are preserved, caller handles remain open, and their original offset is
restored on ordinary completion. With `Machinera`, after a deadline or interruption,
wait for file release before reusing a handle and restore the offset yourself if
needed; `AsyncMachinera` restores it before cancellation propagates. Path-owned files
close when pending reads finish.

With `transport="auto"`, an unkeyed synchronous request the service refuses before
starting any work is submitted once as a durable job instead, with the same encoded
body, operation key, deadline, and limits; `phase` becomes `"job_submit"`. This
applies to a size refusal (HTTP 413 without a code, `sync_size_cap`, or
`inline_body_over_cap`) and, after any eligible synchronous retries, to the retryable
pre-execution refusals `inline_claim_timeout`, `inline_admission_refused`, and
`no_serving_capacity` when the service has not marked them non-retryable. A response
lost after sending, any other failure, and `transport="job"` never take this path.

Raises the common operation exceptions below, plus `PayloadTooLargeError` for
service upload or local descriptor size limits, `ValueError` for invalid input values or simultaneous use of
one handle, and `TypeError` for unsupported input types. `IntegrityError` reports
changes to file size or content during preparation or streaming. After-send
synchronous failures follow [`sync_replay`](#synchronous-replay).

#### `transcribe_url`

```python
transcribe_url(
    url: str,
    *,
    model: str,
    language: str | None = None,
    response_format: ResponseFormat = "json",
    timeout: float | httpx.Timeout | TimeoutPolicy | None = ...,
    deadline: float | None = None,
    idempotency_key: str | None = None,
) -> TranscriptionResult
```

Submit a direct audio URL as a durable job and wait for completion, whatever the
`transport` setting. Shared options have the same meanings as `transcribe_file`. The
SDK sends a JSON descriptor holding the URL, with no credentials or headers for it; it
does not fetch or host the audio locally. The service fetches it, possibly after the
job waits to start, so the URL must return the audio without further authentication
and, if signed, stay valid until the job runs. Redirect handling is not specified;
pass the final URL. An omitted key generates a fresh random key, so repeating an
unkeyed call submits, and bills, a new job; pass a key derived from the URL and options
when retrying around the SDK.

Raises the common operation exceptions, including `PayloadTooLargeError` when the
encoded descriptor exceeds `limits.descriptor_bytes`. A URL the service cannot use
fails with code `unsupported_scheme`, `url_blocked`, `url_unreachable`,
`audio_unavailable`, `audio_ref_expired`, or `media_unprobeable`: as
`BadRequestError` when refused at submission, or as `TerminalJobError` when the fetch
fails after the job was accepted. These failures are permanent for that URL.

#### `get_job`

```python
get_job(job_id: str, *, timeout: float | httpx.Timeout | TimeoutPolicy | None = ...) -> JobSnapshot
```

Read one job snapshot with eligible transient read retries. Omitted `timeout`
inherits the client policy, including its total deadline. Explicit `None` disables
HTTP inactivity limits only; other forms follow the precedence above. The frozen
Pydantic `JobSnapshot` contains
`id` and `status` plus returned fields, such as `result`. A status this SDK version
does not know is returned unchanged. An `"error"` status raises `TerminalJobError`,
even when HTTP status is 200. Unknown fields are retained in `raw`. Access fields as attributes, for example
`snapshot.status` and `snapshot.result.text`. No job is submitted.

#### `resume`

```python
resume(
    job_id: str | None = None,
    *,
    file: FileInput | None = None,
    operation_key: str | None = None,
    upload_id: str | None = None,
    model: str | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    language: str | None = None,
    response_format: ResponseFormat | None = None,
    timeout: float | httpx.Timeout | TimeoutPolicy | None = ...,
    deadline: float | None = None,
) -> TranscriptionResult
```

With `job_id`, poll only that job; no file is opened or submission made. Omitted
`response_format` selects `"verbose_json"` for this form.
Without a job ID, `file`, `model`, and `operation_key` are required. This form always
uses staged initialization, PUT and submission, even if local inline limits have
changed. Omitted `response_format` selects `"json"`; supply the original input,
metadata, language and format. It does not recover ambiguous synchronous submissions.
To recover a staged call, normally repeat `transcribe_file` with the same key and
options, as the [README](README.md#recovery-after-a-restart) shows; that replays the
same staged steps when the client's `Limits` are unchanged. Use this form when they
differ from the original call's.

`upload_id` is optional saved context and must match initialization replay. Without
a job ID, recovery replays from initialization to recover current server state,
whatever phase the original call reached.
If the upload is already submitted or being submitted, recovery skips PUT and repeats keyed submission, even if initialization returns a job ID.
Submission validates the original model, language and format before polling; changed options raise `idempotency_payload_mismatch` with recovered IDs and `phase="submit"`.
No new upload URL is requested when submission is already in progress.
In either form, an SDK error raised once the call starts, including
`TranscriptionInterrupted`, carries the supplied IDs and key: `resume(job_id)` errors
carry that `job_id` with `phase="poll"`, and staged errors carry `operation_key` and
any recovered IDs. A `ValueError` for an invalid argument carries none. Each continuation has
its own total deadline and elapsed time. Job identifiers and arguments are validated locally.

Initialization uses a stable key derived from the operation key; submission uses the operation key.
Lost responses retry with unchanged keys and input. Conditional PUT 412 proceeds
to submission to confirm the stored object. An expired PUT grant is refreshed by
replaying initialization within the fixed `upload_expires_at` window; the stored
response limits remain authoritative for that call.

`upload_incomplete` (409) triggers at most one additional PUT and resubmission.
`upload_integrity_mismatch` raises `IntegrityError` without another PUT; when a
failed job reports it, the error is `TerminalIntegrityError`, which is also a
`TerminalJobError`. `upload_limit_exceeded` is a 429, so it raises `RateLimitError`
rather than `UploadError` and honors `Retry-After` under the normal retry policy. `upload_expired`, `upload_already_bound`,
`idempotency_payload_mismatch`, `payload_too_large` and
`staged_uploads_unavailable` are non-retryable by default; explicit envelope
retry guidance takes precedence for replay-safe requests. When initialization is
refused with `staged_uploads_unavailable` before any grant, the call submits the same
body once as an inline durable job under the same operation key (`phase` becomes
`"job_submit"`) if the encoded body fits the service inline limit: the refusal's
`limits.async_inline_body_bytes` when present, otherwise the service's default inline
limit (99,614,720 bytes). A larger body raises the refusal. A refusal after a grant
was issued never falls back. Storage failures raise `UploadError`, retain status
and sanitized `storage_code`, and never expose a signed URL or storage message.
Storage 401/403 and redirects are never retried or followed.

#### Common operation exceptions

All four request methods may raise `APIStatusError` or a status-specific subclass
for HTTP failures as mapped below. `APIConnectionError` covers network failures,
closed clients, exhausted connection retries, and local I/O failures.
`APIResponseValidationError` reports a response body or shape the SDK cannot use
and is never retried automatically. `APITimeoutError` covers HTTP phase timeouts where replay is safe;
after-send synchronous failures follow [`sync_replay`](#synchronous-replay).
`DeadlineExceededError` bounds total work and polling; `TranscriptionInterrupted`
wraps keyboard interruption with recovery context. Both are `RecoverableJobError`. `TerminalJobError` reports an
observed failed job, or a status other than `"queued"`, `"processing"`, or
`"completed"` while polling. Local argument validation raises `ValueError` or `TypeError`.

Safe retries keep the same encoded request and operation key. Authentication,
permission, and terminal job errors are never automatically resubmitted. See [retryable precedence](#retryable-precedence) and
[`sync_replay`](#synchronous-replay) for retry eligibility.
Accepted jobs can outlive a timeout; use their ID with `resume`. Once the service no
longer keeps a key's replay or result, only a new key can submit again, and that is a
new job and a new charge, not a retry.

### `AsyncMachinera`

Native asyncio equivalent of `Machinera`. Its keyword-only constructor has the
same parameters and defaults, with these type substitutions:

- `http_client: httpx.AsyncClient | None = None`
- `transport: Literal["auto", "job"] | httpx.AsyncBaseTransport = "auto"`
- `sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep`

All configuration validation, environment fallbacks, immutable settings, file forms,
transport selection, retry rules, deadline semantics, errors, and results match
`Machinera`. No event loop is created by either client. Use one async client per
event loop and share it across tasks. `max_concurrency` uses an asyncio semaphore;
waiting for a slot counts toward the call deadline.

| Coroutine | Parameters and return |
| --- | --- |
| `transcribe_file(...)` | Same parameters as `Machinera.transcribe_file`; returns `TranscriptionResult`. |
| `transcribe_url(...)` | Same parameters as `Machinera.transcribe_url`; returns `TranscriptionResult`. |
| `get_job(...)` | Same parameters as `Machinera.get_job`; returns `JobSnapshot`. |
| `resume(...)` | Same parameters as `Machinera.resume`, including staged recovery; returns `TranscriptionResult`. |
| `aclose()` | Wait for active calls and close only the HTTP clients the SDK created; returns `None`. |
| `__aenter__()` | Return the client for `async with`. |
| `__aexit__(*args: object)` | Await `aclose()` without suppressing exceptions. |

After closing starts, new calls raise `APIConnectionError`. Caller-owned HTTP
clients remain open. Request construction, storage credential isolation, redirect
refusal, and logging behavior are identical to the blocking client.

Hashing, container inspection, opening, reading, and releasing files run via
`asyncio.to_thread`. Uploads stream bounded chunks. Deadline watchdogs cancel
awaited operations; cleanup waits for any in-flight local file operation before
restoring the original offset or closing owned files. A stalled local file operation
can delay exception delivery without blocking the event loop.

Task cancellation re-raises `asyncio.CancelledError` with safe recovery attributes
`operation_key`, `upload_id`, `phase`, `job_id`, and `last_status` when the call has
entered an operation. Use `getattr(error, "job_id", None)` and equivalent access
for other attributes. Capture recovery context inside the coroutine directly awaiting
the SDK method. Python 3.10 can replace a cancellation exception at a task boundary;
attributes are not guaranteed on the replacement seen by another task. The SDK
does not alter task cancellation behavior to prevent this replacement. The
exception the SDK re-raises is the native `asyncio.CancelledError`, so task groups
and timeouts retain their normal behavior. `KeyboardInterrupt` is represented by
`TranscriptionInterrupted`. Cancellation stops subsequent HTTP requests and retries;
it never cancels or deletes server work. Save the context, then re-raise cancellation.
Recover an accepted job with `await client.resume(job_id)`; before acceptance, apply
the same keyed replay and synchronous ambiguity rules as `Machinera`.

## Configuration and results

### `ResponseFormat`

`Literal["json", "text", "verbose_json"]`: accepted response formats.

### `TimeoutPolicy`

```python
TimeoutPolicy(
    connect: float | None = ...,
    write: float | None = ...,
    read: float | None = ...,
    pool: float | None = ...,
    poll_request: float = ...,
    deadline: float = ...,
) -> TimeoutPolicy
```

Frozen dataclass with the listed public fields, in seconds; see [Defaults](#defaults). HTTP phases accept positive finite
seconds or `None` to disable inactivity limits. `poll_request` and `deadline`
remain positive and finite. Invalid values raise `ValueError` or `TypeError`.
Each active HTTP phase is capped by the remaining total deadline; a GET is also
capped by `poll_request`. A watchdog bounds the full exchange. The monotonic
`deadline` includes preparation, concurrency waits, HTTP requests, retry sleeps,
and polling. Explicit phase values use
[httpx inactivity semantics](https://www.python-httpx.org/advanced/timeouts/).

### `RetryPolicy`

```python
RetryPolicy(
    max_attempts: int = ...,
    initial_delay: float = ...,
    max_delay: float = ...,
    poll_interval: float = ...,
    max_polls: int | None = ...,
) -> RetryPolicy
```

Frozen dataclass; see [Defaults](#defaults).
`max_attempts` includes the initial request per transient-failure step and must be
a positive integer. `max_polls` is `None` or a positive integer. Delays are
positive finite seconds, with `initial_delay <= max_delay`. Invalid values raise
`ValueError` or `TypeError`. Retry delay is
`min(initial_delay * 2**retry_index, max_delay)` multiplied by uniform jitter in
`[0.75, 1]`. Valid `Retry-After` seconds or HTTP dates set a minimum wait, even above
that cap; a valid `retry-after-ms` header takes precedence over `Retry-After`. A
required wait that cannot fit inside the deadline raises `DeadlineExceededError`. Normal polls use `poll_interval`, subject to
`Retry-After`, without consuming transient attempts.

Polling rule: the call's deadline and a positive `max_polls` bound the polling of a
pending job. A failed status read or an interrupt can end the call earlier while the
job stays pending; recover per [Failure handling](#failure-handling). Every call has a deadline, either `deadline=` or the inherited `TimeoutPolicy.deadline`,
so polling is always bounded. `max_polls=None` adds no count limit; a positive
`max_polls` is an additional hard cap on status reads, and reaching it raises
`DeadlineExceededError` with the accepted `job_id`. A terminal status ends polling
earlier, as described under `TerminalJobError`.

### `Limits`

```python
Limits(
    sync_inline_body_bytes: int = ...,
    job_inline_body_bytes: int = ...,
    descriptor_bytes: int = ...,
) -> Limits
```

Frozen dataclass of encoded request limits, in bytes; see [Defaults](#defaults).
`sync_inline_body_bytes` and `descriptor_bytes` default to the service contract
([public limits](https://api.machinera.com/docs/limits)). `job_inline_body_bytes`
defaults to the SDK's staged-upload threshold, which is below the service's own inline
cap. Routing follows [`transcribe_file`](#transcribe_file). Multipart limits include
framing overhead. Descriptor size includes JSON encoding. All fields must be
positive integers, with the sync limit no greater than the job limit;
invalid configuration raises `ValueError`. These are independent of service audio
duration constraints; raising a local threshold does not change server limits.

### Defaults

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

### `TranscriptionResult`

```python
TranscriptionResult(
    text: str,
    warnings: Any = None,
    request_id: str | None = None,
    job_id: str | None = None,
    elapsed_seconds: float = 0,
    response_format: ResponseFormat = "json",
    task: str | None = None,
    language: str | None = None,
    duration: float | None = None,
    inference_seconds: float | None = None,
    words: list[TranscriptionWord] | None = None,
    segments: Any = None,
    usage: Any = None,
) -> TranscriptionResult
```

Frozen Pydantic model exposing all listed fields; the fields from `task` onward are
set only when the service returns them, as verbose and durable-job responses do.
`text` preserves whitespace and empty strings. `raw` preserves returned result fields; `warnings` preserves optional
warning metadata without changing text or triggering retries. `request_id` comes from
the last HTTP response the call received and is `None` when the service sent none;
`job_id` is `None` for a synchronous request.
`elapsed_seconds` measures client call time, including preparation, requests,
retries, waiting to start, and polls. `inference_seconds` is the service's own processing
time, set when the service returns it (the value also stays in `raw`).
Nested dictionaries and warning objects are not made immutable. Text, word text, and
warning strings are validated strictly and never coerced or trimmed; other fields use
Pydantic's lax mode, so `1.0` is accepted for an integer but `1.5` is not. Unknown fields are
allowed and retained in the read-only `raw: dict[str, Any]` property, which captures the input
dictionary after validation. A response field named `raw` remains inside that
preserved dictionary as an ordinary extension; it never supplies typed fields or
replaces the SDK's `raw` attribute. This also applies to job, word, and error models.
SDK request metadata is added after validation without changing the preserved payload.
Missing verbose fields default to `None` without being added to `raw` or the verbose
projection. Malformed server responses raise sanitized `APIResponseValidationError`,
never a Pydantic validation exception. Direct model construction uses Pydantic validation errors.

| Member | Return and behavior |
| --- | --- |
| `to_json() -> dict[str, Any]` | New dictionary containing exact `text` and `usage` if present in `raw`. |
| `to_text() -> str` | Exact `text`. |
| `to_verbose_json() -> dict[str, Any]` | Shallow copy of `raw`; absent fields are not synthesized. |
| `output: str \| dict[str, Any]` | Read-only property selecting the corresponding projection using `response_format`. |

These projections raise no SDK exceptions for valid result fields. A minimal sync
response cannot provide missing verbose fields. Durable-job responses retain
available verbose fields regardless of the requested output projection.

### `JobSnapshot`

Frozen Pydantic model returned by `get_job`. Required fields are `id: str` and
`status: str`; statuses the service adds later still parse, and `JobStatus` lists
the known ones. Optional fields
are `created_at: int | None`, `updated_at: int | None` (Unix epoch seconds),
`eta_seconds: float | None`, `error: JobError | None`,
`result: TranscriptionResult | None`, and `warnings: Any = None`.
`raw: dict[str, Any]` retains the complete response, including unknown fields;
unknown fields are also allowed as extra attributes. Nested result text and
warnings preserve whitespace and empty strings. `get_job` raises
`TerminalJobError` for a terminal failure; `resume` returns a `TranscriptionResult`.

### `JobStatus`

`Literal["queued", "processing", "completed", "error"]`: job statuses known to this
SDK version, for type hints. `JobSnapshot.status` is a plain `str`. While polling,
`"queued"` and `"processing"` continue, `"completed"` returns the result, and any
other status is terminal and raises `TerminalJobError` with the status in
`last_status`, because a status the service adds later is a final state.

### `JobError`

Frozen Pydantic model with optional `code: str`, `message: str`, `type: str`,
`retryable: bool`, and `details: Any` fields, all defaulting to `None`.
`raw` preserves original fields and extra metadata. Text is validated strictly.

A failed job's code becomes `TerminalJobError.code`, and its `retryable` becomes
`TerminalJobError.retryable` (the SDK's code table supplies it when the service sends
none, and a code the SDK does not know is then `False`). `JobError.retryable` is `None`
only on the snapshot, when the service sent no flag; `TerminalJobError.retryable` is
never `None`. Job-level codes:

| Code | Retryable | Meaning |
| --- | --- | --- |
| `job_attempts_exhausted` | `True` | The service gave up after repeated attempts. |
| `job_placement_lost` | `True` | The service lost this job before it finished. |
| `job_queue_timed_out` | `True` | This job waited too long to start. |
| `job_shed` | `True` | The service dropped this job under load. |
| `job_tombstoned` | `True` | The service ended this job without a result. |
| `result_unavailable` | `True` | This job's result could not be retrieved. |
| `result_unreadable` | `False` | This job's result could not be read. |

A failed job can also carry an input code such as `non_english_audio`,
`audio_duration_exceeded`, `media_unprobeable`, a URL code listed under
`transcribe_url`, or `upload_integrity_mismatch` (`TerminalIntegrityError`). None of
these jobs will complete, whatever the code. `Retryable` describes a new submission, not
the failed job: a retryable code means a submission under a new key may succeed, as a
new job and a new charge; repeating the call with the same key replays the failed job
and raises the same error.

### `TranscriptionWord`

Frozen Pydantic model with required `word: str`, `start: float`, and `end: float`,
plus optional `confidence: float | None`. Timestamps are seconds. `word` retains
empty strings and whitespace exactly; `raw` retains original fields and extras.

## Files and uploads

### `SUPPORTED_MEDIA_SUFFIXES`

`SUPPORTED_MEDIA_SUFFIXES: frozenset[str]` contains the accepted lowercase suffixes
without leading dots. Use this exported constant when validating explicit filenames. It does not imply
that arbitrary bytes with those names are valid audio; the service validates media.

### `UploadPhase`

`Literal["upload_init", "upload_put", "submit", "poll"]`: staged operation phases
reported by staged failures.

## Exceptions

### `MachineraError`

`MachineraError(*args: object) -> MachineraError` extends `Exception` and is the
base of all SDK exceptions. Recovery metadata is defined on its `APIError`
subclass. Local argument validation uses built-in `ValueError` or `TypeError`.

Read-only `is_transient: bool` is `True` when repeating the identical call, as made,
is safe and may succeed, as the SDK classifies it by the rows below. `False` does
not mean a repeat is unsafe or hopeless. For a failed HTTP exchange the rows follow
the SDK's own retry rule, so such an error is never `True` when the retry loop refused
to retry its response. Two errors the SDK never repeats itself follow their own rows: a
`DeadlineExceededError` is transient when the call can be repeated without a second
submission (always for a keyed call), and a retryable `TerminalJobError` is transient
when the SDK generated the operation key, because repeating that call is the new
submission the service asks for. After the service accepted a job for a call whose
key the SDK generated, no other error is transient: repeating that call would submit a
second job, so resume `job_id` instead. The same holds without a `job_id` once a job
submission for such a call was sent and its response lost, because the service may have
accepted that job.

With the caller's own key, repeating the identical call is always safe, because the
service answers a repeated key with the original job, running or finished, and never
runs or bills it twice, subject to [replay retention](https://github.com/machinera-labs/machinera-python/blob/main/README.md#recovery-after-a-restart). So is `resume(job_id)`, which only polls. For those calls,
`is_transient` says only whether the SDK expects the repeat to succeed. For a
caller-keyed call with no `job_id` and `is_transient=False`, the recovery exception to
row 7 is an `InternalServerError` or `APIResponseValidationError` in `phase`
`"job_submit"` or `"submit"`, excluding HTTP 4xx: repeating the identical call under
the same key, within your own retry limit, may recover an accepted job. An
`APIConnectionError` with `retryable=False` does not qualify; `phase` alone does not
prove a request was sent. For accepted jobs, follow row 3 of
[Failure handling](#failure-handling). The first matching row applies:

| Error | `is_transient` |
| --- | --- |
| `TranscriptionInterrupted` | `False`: an interrupt stops the caller; recovery is the explicit decision in row 1 of [Failure handling](#failure-handling) |
| Any error without `job_id` on a call without `idempotency_key`, after a job submission (`phase` `"job_submit"` or `"submit"`) was sent and its response lost | `False`, whatever error then ended the call: the service may have accepted that job, and repeating the call as made would submit a second one. Repeat it with `idempotency_key=error.operation_key` instead, which replays that job if it was accepted (row 5 of [Failure handling](#failure-handling)) |
| `DeadlineExceededError` | With `job_id` set: `True` for a keyed call or `resume`, `False` for a call without `idempotency_key` (resume that job; do not resubmit). Without one: `True` when `phase` is `"prepare"` or `"concurrency_wait"` (nothing was sent), when `phase` is `"sync_submit"` under `sync_replay="always"`, or when the caller supplied the key (`idempotency_key`, or `operation_key` to `resume`), so repeating replays the same submission; otherwise `False`, because repeating without `operation_key` would submit again |
| `TerminalJobError`, including `TerminalIntegrityError` | `True` when `retryable` is `True` and the SDK generated the operation key (an unkeyed `transcribe_file` or `transcribe_url`, including the job fallback), so repeating the call is a new submission; `False` for a keyed call or `resume`, which replay the same failed job |
| Any other `APIError` with `job_id` set, on a `transcribe_file` or `transcribe_url` call without `idempotency_key` (not `resume`, whose errors follow the rows below) | `False` (resume that job; repeating the call would submit a second one) |
| `APIConnectionError` from local file I/O or a closed client | `False` |
| Other `APIConnectionError`, including `APITimeoutError` | `True` unless `retryable` is `False`; in the `"sync_submit"` phase only when `retryable` is `True`: nothing was sent, or `sync_replay="always"` replayed a request that failed after sending |
| `AuthenticationError`, `PermissionDeniedError` | `False` |
| Other `APIStatusError`, including `RateLimitError` and `InternalServerError` | `True` only when `retryable` is `True` (see [retryable precedence](#retryable-precedence)); in the `"sync_submit"` phase additionally only for HTTP 429, a refusal code proving the request did not run (`content_md5_mismatch`, `incomplete_body`, `incomplete_upload`, `inline_admission_refused`, `input_busy`, `no_serving_capacity`, or `request_body_timeout`), or, with `sync_replay="always"`, any code except the job-fallback refusals (`inline_admission_refused`, `inline_claim_timeout`, and `no_serving_capacity`). With `sync_replay="always"`, a `"sync_submit"` 5xx is also `True` whatever `retryable` is, unless the service sent `retryable: false` or a job-fallback code |
| Anything else, including `AmbiguousSubmissionError`, `UploadError`, and `APIResponseValidationError` | `False` |

### `RecoverableJobError`

`RecoverableJobError` extends `MachineraError` and marks a call that ended while its
job may still run: `DeadlineExceededError` and `TranscriptionInterrupted`. It is not
raised on its own. `job_id: str | None` is the job to pass to `resume`, or `None` when
no job ID was observed. `None` does not prove that no job was accepted: a submission
response can be lost, or the call can end before the ID arrives. Without a `job_id`,
recovery depends on `phase`, as in row 5 of [Failure handling](#failure-handling).
The other recovery context comes from `APIError`.

### `APIError`

```python
APIError(
    message: str,
    *,
    status_code: int | None = None,
    body: dict[str, object] | None = None,
    code: str | None = None,
    retryable: bool | None = None,
    request_id: str | None = None,
    job_id: str | None = None,
    operation_key: str | None = None,
    phase: str | None = None,
    last_status: str | None = None,
    upload_id: str | None = None,
    storage_code: str | None = None,
) -> APIError
```

Extends `MachineraError`. Every subclass below inherits this constructor and
these public attributes. `message` is the exception's argument and is also an
attribute. `str(error)` is `message`, followed by ` (request_id: <id>)` when
`request_id` is known; quote that ID when contacting support. `status_code` is the HTTP status when available; `status: int | None`
is a read-only alias. `code` and `retryable` expose service
guidance when available. `DeadlineExceededError` and `AmbiguousSubmissionError`
always set `retryable=False`, including when constructed with a different value: the
SDK never resends them itself. That does not make them permanent; whether repeating
the call is safe is `is_transient`, and a keyed `DeadlineExceededError` is transient.
IDs and `phase` describe recovery progress; `last_status` records the last observed
job state. `phase` is one of `"prepare"`, `"concurrency_wait"`, `"sync_submit"`,
`"job_submit"` (durable-job submission of an inline file or a URL), or an
`UploadPhase`. `"poll"` is the phase of every job status read: after any durable
submission, inline, URL, or staged, and in `resume`. Local classifications need not have a service code or HTTP status.
`upload_id` is set once a staged upload is initialized; `job_id` and `last_status`
once a job is accepted. Preparation of a staged body belongs to `"upload_init"`.
Argument validation before transport selection still uses local
`ValueError`/`TypeError`.
`storage_code` is a sanitized XML `<Code>` token for storage responses, otherwise
`None`; service JSON codes remain in `code`. Storage response bodies are discarded.

For SDK-created errors, `body` is an allowlisted mapping of service code, retry
guidance, and safe identifiers/context, or `None` for non-JSON responses. Service
free text, raw HTTP objects, URLs, HTML, audio, and transcripts are excluded from
exceptions and their chains. Protect credentials, identifiers, URLs, input data,
and transcript content in application logging. The constructors themselves do
not sanitize arbitrary messages or bodies supplied by application code.

`wait_for_file_release(timeout: float | None = None) -> bool` waits until the SDK
stops accessing the input handle. `None` waits indefinitely; `0` checks readiness.
Returns `False` if the wait times out, otherwise `True`, including when no file is
pending. With `Machinera`, require `True` before reusing, seeking, or closing a
caller-owned handle after a deadline or interruption. `AsyncMachinera` awaits pending
file operations itself, and its task cancellation raises `asyncio.CancelledError`,
which does not have this method. This method does not cancel a server job.

### Exception subclasses

All subclasses have the same metadata as `APIError` and raise no additional SDK
exceptions during construction. All except `TranscriptionInterrupted` have the same
signature as `APIError`; `TranscriptionInterrupted(message, *, ambiguous=False, **context)`
adds the keyword-only `ambiguous: bool` and forwards every other keyword to `APIError`.

| Symbol | Direct base | Meaning |
| --- | --- | --- |
| `APIStatusError` | `APIError` | Unsuccessful HTTP response; base for status-specific subclasses and local size rejection. |
| `BadRequestError` | `APIStatusError` | HTTP 400; correct the request. |
| `AuthenticationError` | `APIStatusError` | HTTP 401; check credentials. |
| `PermissionDeniedError` | `APIStatusError` | HTTP 403; check permissions. |
| `NotFoundError` | `APIStatusError` | HTTP 404; check the resource and account. |
| `ConflictError` | `APIStatusError` | HTTP 409; apply the ordered [`is_transient`](#machineraerror) and [Failure handling](#failure-handling) tables. |
| `PayloadTooLargeError` | `APIStatusError` | HTTP 413, including non-JSON responses, or local size rejection with `status_code=None`. |
| `UnprocessableEntityError` | `APIStatusError` | HTTP 422; correct the request content. |
| `RateLimitError` | `APIStatusError` | HTTP 429 after eligible retries; inspect guidance. |
| `InternalServerError` | `APIStatusError` | HTTP 5xx; see [`is_transient`](#machineraerror) and [`sync_replay`](#synchronous-replay). |
| `APIConnectionError` | `APIError` | Network, local I/O, or lifecycle failure. A local I/O failure's message names the original exception class and, when present, its `errno`, never a path. |
| `APITimeoutError` | `APIConnectionError` | HTTP phase timeout where replay is safe. |
| `APIResponseValidationError` | `APIError` | Malformed or unexpected response body or shape; never retried automatically. `status_code` is set when a response exists. |
| `DeadlineExceededError` | `APIError`, `RecoverableJobError` | Call deadline, required wait, or explicit `max_polls` cap exhausted; recover accepted work. |
| `AmbiguousSubmissionError` | `APIError` | Sync execution may have started; see row 4 of [Failure handling](#failure-handling). Controlled by [`sync_replay`](#synchronous-replay). |
| `TerminalJobError` | `APIError` | Failed job observed during a status read, possibly HTTP 200, or an unrecognized status while polling; `last_status` holds the status. |
| `UploadError` | `APIError` | Base upload failure type; every non-429 service error whose `code` starts with `upload_` raises it (or `IntegrityError`) instead of a status-specific class, except a failed job's `TerminalJobError`; `upload_not_found` is an `UploadError`, not a `NotFoundError`. Never transient. |
| `IntegrityError` | `UploadError` | Input changed during preparation or streaming. |
| `TerminalIntegrityError` | `IntegrityError`, `TerminalJobError` | A job failed with `upload_integrity_mismatch`; resuming the same `job_id` cannot succeed. |
| `TranscriptionInterrupted` | `APIError`, `RecoverableJobError`, `KeyboardInterrupt` | Interrupted operation with safe recovery context. Read-only `ambiguous: bool` is `True` when an unkeyed synchronous request was interrupted and may have run; see row 4 of [Failure handling](#failure-handling). |

Other unsuccessful HTTP statuses use `APIStatusError` directly.

### Failure handling

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

#### Retryable precedence

An error built from a service response, including a failed job, always has `retryable`
`True` or `False`, never `None`. A status error's `retryable` is the
service's flag when it sends one, otherwise the SDK's code table, otherwise `True` only
for HTTP 429, 502, 503, and 504. Whether an error is transient follows the first
matching row of the [`is_transient` table](#machineraerror), which also covers the
synchronous route and a lost job submission.

## Version

### `__version__`

`__version__: str` is the installed SDK version. It agrees with distribution metadata
and the default `machinera-python/<version>` User-Agent. Release tags prepend `v` to it.
