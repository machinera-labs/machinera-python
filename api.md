# API reference

Import every documented symbol from `machinera`. The SDK has a blocking client
(`Machinera`) and a native asyncio client (`AsyncMachinera`). Separately, a
*durable job* is a server-side transcription job that can be resumed by ID;
`transport="job"` and `Limits.job_inline_body_bytes` refer to durable jobs.

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
default that is declared in source or represented by an internal sentinel; it is
not a value to pass.
Omitted `timeout` and `max_retries` use the defaults declared in
[TimeoutPolicy and RetryPolicy](src/machinera/_types.py).

| Parameter | Meaning |
| --- | --- |
| `api_key` | Explicit non-`None` credential, otherwise `MACHINERA_API_KEY`. Must be nonempty visible ASCII without whitespace. Missing or invalid credentials raise `ValueError`. |
| `base_url` | Explicit non-`None` endpoint, otherwise `MACHINERA_BASE_URL`, otherwise `https://api.machinera.com/v1`. Requires an HTTP(S) origin, optionally ending in `/v1`, with no credentials, query, or fragment. Normalized to `/v1`. |
| `max_retries` | Nonnegative integer; `n` means `n+1` attempts per replay-safe step, including the initial request. Omission uses `RetryPolicy()`. Cannot be supplied together with `retry_policy`. |
| `default_headers` | String mapping copied into immutable client configuration and merged into API requests. Caller values override non-reserved SDK defaults. |
| `timeout` | Omission uses `TimeoutPolicy()`. Scalar, `httpx.Timeout`, and explicit `None` configure HTTP phases while retaining default poll-request and total-deadline bounds. A policy supplies all settings. See timeout forms below. |
| `retry_policy` | Advanced retry and poll configuration; `None` constructs `RetryPolicy()`. |
| `limits` | `None` constructs `Limits()`. |
| `max_concurrency` | Positive integer limiting active operations, or `None` for no client semaphore. Waits count toward the deadline. |
| `transport` | `"auto"` selects inline sync or a durable job by encoded size, falling back to a durable job when the synchronous route refuses admission (see `transcribe_file`); `"job"` always selects a durable job. A custom `httpx.BaseTransport` injects HTTP behavior while keeping automatic selection. |
| `sync_replay` | Exactly `"never"` (the default) or `"always"`; any other value raises `ValueError`. Controls only an unkeyed synchronous request that fails after its body was sent: a lost response, or a retryable status response such as a 502, 503, or 504. `"never"` raises `AmbiguousSubmissionError` for a lost response and does not replay either case. `"always"` replays the identical request under `RetryPolicy.max_attempts`, the same backoff, and the call deadline; exhausted attempts raise the last error (`APIConnectionError`, `APITimeoutError`, or the status error) with `is_transient` `True`, and an expired deadline raises `DeadlineExceededError`. Responses that are not retryable are never replayed, and the job-fallback refusals (`inline_claim_timeout`, `inline_admission_refused`, `no_serving_capacity`) keep their existing attempts before the fallback. A replayed request may run, and be billed, more than once; see [Replaying synchronous requests](README.md#replaying-synchronous-requests). Keyed calls, durable jobs, polling, uploads, and the auto-mode job fallback are unaffected. |
| `http_client` | Optional caller-owned `httpx.Client`; cannot be combined with a custom transport. Its pool settings and lifetime remain the caller's responsibility. |
| `clock`, `sleeper` | Monotonic time and delay functions, injectable for tests. |
| `wall_clock` | Wall time for HTTP-date `Retry-After` parsing. |
| `random_source` | Random sample in `[0, 1]` for retry jitter. |

`MACHINERA_LOG=debug` or `MACHINERA_LOG=info`, read at construction, sets the level
of the `machinera` logger and attaches a stderr handler if it has none; other
values leave logging unchanged.

Explicit invalid values fail without environment fallback. No dotenv discovery
or endpoint probing occurs. Header names and values are validated before HTTP.
Headers the SDK owns, listed in `_RESERVED` in [`_files.py`](src/machinera/_files.py),
cannot be overridden. Other defaults such as User-Agent can be overridden.

Raises `ValueError` for invalid configuration or conflicting retry controls;
`TypeError` for an unsupported timeout form or HTTP client type. Configuration is
immutable. Public attributes are `default_headers`, `base_url`, `timeout`,
`retry_policy`, `limits`, `max_concurrency`, `transport`, and `sync_replay`; `transport` is
`"auto"` when a custom HTTP transport is injected. Independent calls may share a
client across threads. Do not share a file cursor between concurrent calls.

HTTP clients are selected in `Core.__init__` in [`_core.py`](src/machinera/_core.py).
An injected `http_client` or custom `transport` carries every API and storage
request, so its own pool behavior applies; an injected `http_client` also keeps its
proxy and URL-mount routing, pool configuration and ownership. Otherwise uploads and
submissions use a client with `EXCHANGE_POOL`, which keeps no idle connections, and
job status reads use a separate client with `POLL_POOL`. The blocking client builds
that status-read transport with `keepalive_transport` in [`_io.py`](src/machinera/_io.py),
which falls back to `EXCHANGE_POOL` when it cannot install its connection hook.
Clients the SDK creates, including one wrapping a custom transport, disable
environment proxies and automatic redirects. Storage requests are constructed directly
with `httpx.Request` and sent with `auth=None` and `follow_redirects=False`;
client default headers, authentication and cookies are not merged. Effective
timeouts and redirect refusal are applied per request. Injected client request and
response hooks run for storage too: hooks must preserve the grant headers, add no
credentials or cookies, and avoid logging signed URLs. The SDK suppresses httpx's
request log for each storage exchange without suppressing concurrent API logs.
Transport-level retries use the [httpx default of zero](https://www.python-httpx.org/advanced/transports/).

#### Timeout forms and precedence

- Omitted method `timeout` inherits the resolved client policy.
- `timeout=seconds` sets connect, write, read, and pool phases to the same positive
  finite value, retaining the inherited poll-request and total deadline bounds.
- `timeout=httpx.Timeout(...)` copies its four phase values, including disabled
  (`None`) phases, retaining the inherited poll-request and total deadline bounds.
- `timeout=None` disables HTTP phase inactivity limits, retaining the inherited
  poll-request and total deadline bounds. Calls remain bounded.
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
[SUPPORTED_MEDIA_SUFFIXES](#supported_media_suffixes) and the
[README input rules](README.md#file-inputs). A supplied MIME type is matched
case-insensitively, ignoring parameters, against `_MIME_SUFFIXES` in
[`_files.py`](src/machinera/_files.py); otherwise `Multipart.prepare` in
[`_multipart.py`](src/machinera/_multipart.py) reads a bounded prefix for `sniff`
and restores the handle offset. Content identified this way is named
`upload.<suffix>`.
Unsupported explicit names, unrecognized unnamed content, invalid headers, and
non-seekable handles fail locally. Names, content types, and part headers cannot
inject multipart framing; part headers cannot contain credentials or cookies.

Use `model="transcribe-v1"`. `language` accepts English (`en` or an `en-*` tag);
other hints raise `ValueError` before HTTP.
`response_format` chooses the result projection. Omitted `timeout` inherits the
client policy; other forms follow the precedence above. `deadline` overrides its
total elapsed budget with positive finite seconds. `idempotency_key` is optional,
1–128 visible ASCII characters, and selects a durable job when provided. Preserve
it before a call if restart recovery matters.

Encoded multipart size, including metadata and boundary overhead, determines
transport selection. Files over the durable-job inline limit
(`limits.job_inline_body_bytes`) automatically use staged uploads with both
transport settings. Equality stays inline. Initialization supplies
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
admitting any work is submitted once as a durable job instead, with the same encoded
body, operation key, deadline, and limits; `phase` becomes `"job_submit"`. This
applies to a size refusal (HTTP 413 without a code, `sync_size_cap`, or
`inline_body_over_cap`) and, after any eligible synchronous retries, to the retryable
admission refusals `inline_claim_timeout`, `inline_admission_refused`, and
`no_serving_capacity` when the service has not marked them non-retryable. A response
lost after sending, any other failure, and `transport="job"` never take this path.

Raises the common operation exceptions below, plus `PayloadTooLargeError` for
service upload or local descriptor size limits, `ValueError` for invalid input values or simultaneous use of
one handle, and `TypeError` for unsupported input types. `IntegrityError` reports
changes to file size or content during preparation or streaming. With the default
`sync_replay="never"`, an unanswered sync request that might have executed raises
`AmbiguousSubmissionError`; never blindly retry it. With `sync_replay="always"`, the
SDK replays it instead, as described under the `sync_replay` constructor parameter.

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

Submit a direct audio URL as a durable job and wait for completion. Shared options have
the same meanings as `transcribe_file`. The SDK sends a JSON descriptor; it does
not fetch or host the audio locally. An omitted key generates a fresh random key.
Raises the common operation exceptions, including `PayloadTooLargeError` when the
encoded descriptor exceeds `limits.descriptor_bytes`.

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

`upload_id` is optional saved context and must match initialization replay. Without
a job ID, recovery replays from initialization to recover current server state,
whatever phase the original call reached.
A bound replay skips PUT but still replays keyed job submission, including when
grant refresh discovers the binding and supplies a job ID. This validates the
model, language and response format against the original job descriptor before
polling. Changed options surface `idempotency_payload_mismatch` with the recovered
IDs and `phase="submit"`. A replay in admission also proceeds to keyed submission
without requesting another write grant. `resume(job_id)` only polls.
Saved IDs and the key are attached to subsequent SDK errors. Each continuation has
its own total deadline and elapsed time. Job identifiers and arguments are validated locally.

Initialization uses `sha256(("upload-init:" + operation_key).encode("ascii")).hexdigest()`
as its stable subkey; submission uses the original key. Independent calls generate
fresh random operation keys. Initialization 200 and 201 have identical semantics.
Lost responses retry with unchanged keys and input. Conditional PUT 412 proceeds
to submission to confirm the stored object. An expired PUT grant is refreshed by
replaying initialization within the fixed `upload_expires_at` window; the stored
response limits remain authoritative for that call.

`upload_incomplete` (409) triggers at most one additional PUT and resubmission.
`upload_integrity_mismatch` raises `IntegrityError` without another PUT; when a
failed job reports it, the error is `TerminalIntegrityError`, which is also a
`TerminalJobError`. `upload_limit_exceeded` honors `Retry-After` under
the normal retry policy. `upload_expired`, `upload_already_bound`,
`idempotency_payload_mismatch`, `payload_too_large` and
`staged_uploads_unavailable` are non-retryable by default; explicit envelope
retry guidance takes precedence for replay-safe requests. When initialization is
refused with `staged_uploads_unavailable` before any grant, the call submits the same
body once as an inline durable job under the same operation key (`phase` becomes
`"job_submit"`) if the encoded body fits the service inline limit: the refusal's
`limits.async_inline_body_bytes` when present, otherwise the contract's
`DEFAULT_INLINE_CAP_BYTES`. A larger body raises the refusal. A refusal after a grant
was issued never falls back. Storage failures raise `UploadError`, retain status
and sanitized `storage_code`, and never expose a signed URL or storage message.
Storage 401/403 and redirects are never retried or followed.

#### Common operation exceptions

All four request methods may raise `APIStatusError` or a status-specific subclass
for HTTP failures as mapped below. `APIConnectionError` covers network failures,
closed clients, exhausted connection retries, and local I/O failures.
`APIResponseValidationError` reports a response body or shape the SDK cannot use
and is never retried automatically. `APITimeoutError` covers HTTP phase timeouts where replay is safe;
possible sync execution instead raises `AmbiguousSubmissionError` with the default
`sync_replay="never"`. With `sync_replay="always"`, such a request is replayed; when
replays run out the last `APIConnectionError`, `APITimeoutError`, or status error is
raised with `is_transient` true, and an expired deadline raises
`DeadlineExceededError` (see the `sync_replay` constructor parameter).
`DeadlineExceededError` bounds total work and polling; `TranscriptionInterrupted`
wraps keyboard interruption with recovery context. Both are `RecoverableJobError`. `TerminalJobError` reports an
observed failed job, or a status other than `"queued"`, `"processing"`, or
`"completed"` while polling. Local argument validation raises `ValueError` or `TypeError`.

Safe retries keep the same encoded request and operation key. Authentication,
permission, and terminal job errors are never automatically resubmitted. Service
`retryable` guidance takes precedence over the SDK's error-code table; that table
is the fallback for known codes when guidance is absent. Otherwise, status
heuristics apply, including JSON responses with missing or unknown error codes.
Accepted jobs can outlive a timeout; use their ID with `resume`. Expired replay/result retention requires
caller reconciliation, not a new submission disguised as a retry.

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
Recover an accepted job with `await client.resume(job_id)`; before admission, apply
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

Frozen dataclass with the listed public fields, in seconds; defaults are declared
in [the policy source](src/machinera/_types.py). HTTP phases accept positive finite
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

Frozen dataclass; defaults are declared in [the policy source](src/machinera/_types.py).
`max_attempts` includes the initial request per transient-failure step and must be
a positive integer. `max_polls` is `None` or a positive integer. Delays are
positive finite seconds, with `initial_delay <= max_delay`. Invalid values raise
`ValueError` or `TypeError`. Retry delay is
`min(initial_delay * 2**retry_index, max_delay)` multiplied by uniform jitter in
`[0.75, 1]`. Valid `Retry-After` seconds or HTTP dates set a minimum wait, even above
that cap; a valid `retry-after-ms` header takes precedence over `Retry-After`. A
required wait that cannot fit inside the deadline raises `DeadlineExceededError`. Normal polls use `poll_interval`, subject to
`Retry-After`, without consuming transient attempts.

Polling rule: the call's deadline is the only thing that ends polling of a pending
job. Every call has one, either `deadline=` or the inherited `TimeoutPolicy.deadline`,
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

Frozen dataclass of encoded request limits, in bytes. `sync_inline_body_bytes` and
`descriptor_bytes` default to the service contract, declared in
[the contract source](src/machinera/_contract.py) and the
[public limits](https://api.machinera.com/docs/limits). `job_inline_body_bytes`
defaults to the SDK's staged-upload threshold `STAGED_UPLOAD_THRESHOLD_BYTES`,
declared in [the types source](src/machinera/_types.py); the service's own inline
cap is the contract's `DEFAULT_INLINE_CAP_BYTES`. An unkeyed request up to
`sync_inline_body_bytes` is sent synchronously with `transport="auto"`; a larger one,
up to `job_inline_body_bytes`, is sent inline as a durable job; anything larger is
staged. Multipart limits include
framing overhead. Descriptor size includes JSON encoding. All fields must be
positive integers, with the sync limit no greater than the job limit;
invalid configuration raises `ValueError`. These are independent of service audio
duration constraints; raising a local threshold does not change server limits.

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
warning metadata without changing text or triggering retries. IDs may be absent.
`elapsed_seconds` measures client call time, including preparation, requests,
retries, and polls. Server inference time is separate in `raw` when returned.
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

Read-only `is_transient: bool` is `True` when trying again later may succeed. It is
never `True` for a response the SDK's own retry loop refuses to retry. The first
matching row applies:

| Error | `is_transient` |
| --- | --- |
| `RecoverableJobError` | `True` when `job_id` is set (resume that job; do not resubmit), otherwise `False` |
| `APIConnectionError` from local file I/O or a closed client | `False` |
| Other `APIConnectionError`, including `APITimeoutError` | `True` unless `retryable` is `False`; in the `"sync_submit"` phase only when `retryable` is `True`: nothing was sent, or `sync_replay="always"` replayed a request that failed after sending (`connection_replayable` in [`_exceptions.py`](src/machinera/_exceptions.py)) |
| `AuthenticationError`, `PermissionDeniedError` | `False` |
| Other `APIStatusError`, including `RateLimitError` | `True` only when `retryable` is `True`; in the `"sync_submit"` phase additionally only for HTTP 429, a refusal code proving the request did not run (`SYNC_REPLAYABLE_CODES`), or, with `sync_replay="always"`, any code except the job-fallback refusals (`SYNC_FALLBACK_CODES`); both sets and `retry_eligible` are in [`_exceptions.py`](src/machinera/_exceptions.py) |
| Anything else, including `AmbiguousSubmissionError`, `TerminalJobError`, `UploadError`, and `APIResponseValidationError` | `False` |

### `RecoverableJobError`

`RecoverableJobError` extends `MachineraError` and marks a call that ended while its
job may still run: `DeadlineExceededError` and `TranscriptionInterrupted`. It is not
raised on its own. `job_id: str | None` is the job to pass to `resume`, or `None` when
no job ID was observed. `None` does not prove that no job was accepted: an admission
response can be lost, or the call can end before the ID arrives. Recover by repeating
the identical call with `idempotency_key=error.operation_key`, never a fresh key; the
service replays the original job if it exists. In the `"sync_submit"` phase there is
no key to replay: with the default `sync_replay="never"`, reconcile instead; with
`sync_replay="always"`, the call may be repeated, accepting a possible duplicate
charge (see [Replaying synchronous requests](README.md#replaying-synchronous-requests)).
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
always set `retryable=False`, including when constructed with a different value.
IDs and `phase` describe recovery progress; `last_status` records the last observed
job state. `phase` is one of `"prepare"`, `"concurrency_wait"`, `"sync_submit"`,
`"job_submit"` (durable-job submission of an inline file or a URL), or an
`UploadPhase`. Local classifications need not have a service code or HTTP status.
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
| `ConflictError` | `APIStatusError` | HTTP 409; reconcile the conflicting operation. |
| `PayloadTooLargeError` | `APIStatusError` | HTTP 413, including non-JSON responses, or local size rejection with `status_code=None`. |
| `UnprocessableEntityError` | `APIStatusError` | HTTP 422; correct the request content. |
| `RateLimitError` | `APIStatusError` | HTTP 429 after eligible retries; inspect guidance. |
| `InternalServerError` | `APIStatusError` | HTTP 5xx after eligible retries, including 503. |
| `APIConnectionError` | `APIError` | Network, local I/O, or lifecycle failure. A local I/O failure's message names the original exception class and, when present, its `errno`, never a path. |
| `APITimeoutError` | `APIConnectionError` | HTTP phase timeout where replay is safe. |
| `APIResponseValidationError` | `APIError` | Malformed or unexpected response body or shape; never retried automatically. `status_code` is set when a response exists. |
| `DeadlineExceededError` | `APIError`, `RecoverableJobError` | Call deadline, required wait, or explicit `max_polls` cap exhausted; recover accepted work. |
| `AmbiguousSubmissionError` | `APIError` | Sync execution may have started; reconcile before any resubmission. Not raised for an after-send failure when `sync_replay="always"`. |
| `TerminalJobError` | `APIError` | Failed job observed during a status read, possibly HTTP 200, or an unrecognized status while polling; `last_status` holds the status. |
| `UploadError` | `APIError` | Base upload failure type; every non-429 service error whose `code` starts with `upload_` raises it (or `IntegrityError`) instead of a status-specific class, except a failed job's `TerminalJobError`, so `upload_not_found` means restart the upload. |
| `IntegrityError` | `UploadError` | Input changed during preparation or streaming. |
| `TerminalIntegrityError` | `IntegrityError`, `TerminalJobError` | A job failed with `upload_integrity_mismatch`; resuming the same `job_id` cannot succeed. |
| `TranscriptionInterrupted` | `APIError`, `RecoverableJobError`, `KeyboardInterrupt` | Interrupted operation with safe recovery context. Read-only `ambiguous: bool` is `True` when an unkeyed synchronous request was interrupted and may have run; reconcile instead of resubmitting. |

Other unsuccessful HTTP statuses use `APIStatusError` directly.

## Version

### `__version__`

`__version__: str` is the installed SDK version. It agrees with distribution metadata
and the default `machinera-python/<version>` User-Agent. Release tags prepend `v` to it.
