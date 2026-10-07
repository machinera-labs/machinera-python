# Changelog

## Unreleased

## 0.2.0 — 2026-10-06

### Breaking

- Error and warning codes are integers. String codes and named code aliases are
  removed, with no compatibility layer. SDK 0.1.x cannot talk to the server after
  the numeric-code cut; upgrade to 0.2.0 with the API change.
- `Limits.job_multipart_body_bytes` replaces the previous job body limit name.
  Exception classification and retry decisions use HTTP status and generated
  numeric behavior sets. Retry eligibility follows current service guidance.

### Fixed

- Preserve numeric sync-to-job fallback, including size refusals.
- Accept current upload grants without revision metadata. Grant refresh allows
  a new PUT capability while incomplete, bounded by the absolute `upload_deadline`.
  `submit_expires_at` is null until the server first observes the complete file,
  then supplies that observation time plus the fixed grace period, capped by the
  absolute deadline. The SDK never derives it from file timestamps or local PUT
  completion. Refresh cannot extend that grace or reopen an expired upload.
- Automatically replace an unbound upload after job submission returns HTTP 410
  with code `1003`, retaining identical file bytes under new initialization and
  submission keys only after definite non-acceptance.
  Replacements obey the retry limit and original deadline; exhaustion reports a
  non-transient error with recovery guidance. Ambiguous submissions replay their
  existing descriptor before any replacement, and concurrent callers sharing an
  operation key derive the same replacement keys. Recovery context carries the
  current submission key. After rotation, errors are not transient for an identical
  original call; retry and restart recipes retain the current key and upload ID
  and continue through file recovery instead. Expired PUT capabilities refresh through initialization;
  PUT 412 confirms through submission, and `1002` finishes the upload using the same
  identity. No upload deadline is derived from bandwidth.
- Document `Retry-After: 0` as no known timed minimum; bounded backoff still applies.
- Check numeric code shapes, public copy, and generated contract drift in CI.

## 0.1.4 — 2026-10-06

### Added

- `Machinera(cancel_on_interrupt=True)` installs a SIGINT handler that cancels the
  client before chaining to the prior handler. Construct and close on the main
  thread; `close()` restores the handler. See the [cancellation contract](api.md#cancel).

- `Machinera.cancel()` interrupts active and future blocking operations across
  threads with `TranscriptionInterrupted`, retaining recovery context without
  cancelling server jobs. See the [cancellation contract](api.md#cancel) for the
  latency bound, including scheduling and local-work qualifications. The evaluation harness recipe now
  enables SIGINT cancellation at module import before threads start.

- Public constants `machinera.DEFAULT_IDEMPOTENCY_REPLAY_WINDOW_S` and
  `machinera.DEFAULT_RESULT_RETENTION_S` expose the SDK's snapshot of the service's
  guaranteed minimum idempotency replay period and result retention, in seconds;
  see [Retention defaults](api.md#retention-defaults) for generated values.

## 0.1.3 — 2026-10-02

### Fixed

- Client objects now use identity for equality and hashing; two clients built with the same settings are distinct, and clients can be set members or dictionary keys.

### Changed

- Creating a client is cheap and performs no I/O: TLS contexts and connection pools
  are created on the first request instead of in the constructor.
- Clients the SDK creates share connection pools process-wide by connection settings,
  so a client created per call reuses earlier connections. Blocking pools last for the
  process and close at exit; asyncio pools are per running event loop and close with
  the last client on that loop; a forked child opens its own pools. Closing a client
  releases only its own handle. Injected HTTP clients and transports are unchanged.
- The shared pools no longer cap the total number of connections; `max_concurrency`
  bounds each client separately and does not limit other clients.
- With `sync_replay="always"`, a synchronous 5xx response is now replayed under the
  normal retry policy and deadline, and the error that finally surfaces is transient,
  unless the service marked it `retryable: false` or it is a job-fallback refusal.
  Previously a 5xx without retry guidance, such as a bare 500, ended the call. The
  default `"never"` and durable jobs are unchanged.
- `is_transient` now means that repeating the identical call, as made, is safe and may
  succeed. A failed job (`TerminalJobError`) the service marks `retryable` is transient
  when the SDK generated the operation key, including after the job fallback, because
  repeating the call is the new submission the service asks for. A
  `DeadlineExceededError` without a `job_id` is transient when nothing was sent
  (`phase` `"prepare"` or `"concurrency_wait"`), in `"sync_submit"` under
  `sync_replay="always"`, or when the caller supplied the key, including the
  `operation_key` of a file upload `resume`. Keyed calls and `resume` replay the same job, so
  they stay non-transient when it failed. Once the service accepted a job for a call
  without `idempotency_key`, any other error is non-transient, because repeating that
  call would submit a second job; resume `job_id` instead. `TranscriptionInterrupted`
  is never transient, so retry code that consults `is_transient` will not retry it;
  harnesses that catch every `Exception` need the [Ctrl-C conversion in the recipe](README.md#evaluation-harnesses).
  Previously it was transient whenever it carried a `job_id`. Previously every failed job and every
  `DeadlineExceededError` without a `job_id` was non-transient.
- For a call without `idempotency_key`, an error without a `job_id` is no longer
  transient once a job submission was sent and its response lost, including after the
  job fallback, because the service may have accepted that job and repeating the call
  would submit and bill a second one. This holds whatever error finally ends the call,
  such as an `APIConnectionError` or `APITimeoutError` after the retries run out.
  Repeat the identical call with `idempotency_key=error.operation_key` instead, which
  replays that job if it was accepted. Calls with `idempotency_key` are unchanged.

### Documentation

- Reorganized the README in integration order and paired its failure table with the API reference.
- Added a tested [evaluation-harness recipe](README.md#evaluation-harnesses) with keyed retries and bounded recovery.
- Expanded [restart recovery](README.md#recovery-after-a-restart) and the API reference's input, error, and retention guidance.
- Clarified the two meanings of `transport`: route selection and an injected HTTP transport.

## 0.1.2 — 2026-10-02

### Added

- The `sync_replay="always"` constructor option on both clients replays
  an unkeyed synchronous request that failed after its body was sent (a lost response
  or a retryable error response), under the normal retry policy and deadline, instead
  of raising `AmbiguousSubmissionError` or the error response.
  A replayed request may be billed more than once, so enable it only when finishing a
  long batch matters more than an occasional duplicate charge; the default `"never"`
  keeps the previous behavior.

## 0.1.1 — 2026-10-02

### Changed

- Polling a pending job no longer has a default count cap. `RetryPolicy.max_polls`
  is now `int | None` and defaults to `None` (no count limit); a positive value remains
  a hard cap on status reads. Previously the default count cap could end a long job's polling with
  `DeadlineExceededError` well before a longer `deadline`.
- With `transport="auto"`, a synchronous request refused before any work was admitted
  (`4006`, `4005`, or `4008`, when
  retryable) is submitted once as a durable job with the same body, operation key,
  and deadline instead of raising. Ambiguous and non-retryable failures never fall
  back.
- `str(error)` for an `APIError` ends with `(request_id: …)` when the request ID is
  known; `message` and `args` are unchanged.

### Added

- `RecoverableJobError`, the shared base of `DeadlineExceededError` and
  `TranscriptionInterrupted`, whose `job_id` names the job to resume, or is `None` when
  no job ID was observed. (Superseded advice: this entry said to repeat the call with
  the same operation key when there is no `job_id`. Follow row 5 of the
  failure-handling table instead, which depends on `phase`; an unkeyed synchronous call
  is never repeated with that key.)
- `MachineraError.is_transient`, which is `True` when repeating the same call may
  succeed. It follows the same rule as the SDK's automatic retries, so it is never
  `True` for a response the SDK would not retry, with one addition: a
  `RecoverableJobError` with a `job_id` is transient because the job can be resumed.
  0.1.3 replaces this rule; see its entry.
- An "Evaluation harnesses" guide in the README.
- Homepage and Documentation project URLs and package keywords.

### Fixed

- Local I/O failures raised as `APIConnectionError` name the original exception class
  and `errno` in the message, without the file path.

## 0.1.0 — 2026-10-02

### Added

- Blocking `Machinera` and native asyncio `AsyncMachinera` clients for file and
  URL transcription, job status, polling, and resumption, with context-managed
  lifecycle and configurable concurrency.
- File inputs from paths, binary handles, bytes, and file tuples, with supported
  format detection and local input validation; a handle whose reads return text
  raises `TypeError`.
- Durable server jobs selected by `transport="job"` or an `idempotency_key`, with the
  multipart size limit `Limits.job_multipart_body_bytes`, whose default is the SDK's
  file-upload threshold; multipart job submission
  failures report `phase == "job_submit"`.
- Automatic file uploads for files above that limit with bounded streaming,
  checksums, integrity validation, and safe retries. When the service reports file
  uploads unavailable before granting an upload, a body within the service multipart
  limit is submitted once as a multipart durable job under the same operation key. Custom HTTP proxy and URL-mount settings
  are preserved without forwarding client credentials or cookies to upload storage.
- Idempotent submission and recovery using operation keys, upload context, and job
  IDs, with transcription options validated before resuming bound uploads.
- Configurable elapsed deadlines, HTTP timeouts, retry limits, headers, and
  environment-based credentials and endpoint defaults, with service retry guidance.
  `Retry-After` accepts seconds, including fractional values, or an HTTP date, and a
  `retry-after-ms` header takes precedence when present.
- Cancellation and interruption recovery context, bounded file reads, and safe
  file cleanup that preserves caller ownership and offsets.
  `TranscriptionInterrupted.ambiguous` is `True` when an unkeyed synchronous request
  was interrupted and may have run.
- Typed, frozen Pydantic transcription results and job snapshots that preserve
  exact transcript text and retain unknown response fields in `raw`.
  `JobSnapshot.status` is a plain string with known values in the `JobStatus` type;
  `get_job` returns unrecognized statuses, and polling treats them as terminal and
  raises `TerminalJobError` with the status in `last_status`.
- Status-specific API exceptions, upload errors with sanitized storage codes,
  and explicit deadline, ambiguous-submission, and terminal-job errors with
  sanitized recovery context. Service errors with an `upload_` code raise
  `UploadError`, except rate limits.
- `APIResponseValidationError` for a malformed or unexpected response body,
  including an upload initialization response or grant; it is never retried automatically.
  See the current [caller-keyed recovery policy](api.md#machineraerror).
- `TerminalIntegrityError`, raised when a job fails because the uploaded file did not
  match; it is both an `IntegrityError` and a `TerminalJobError`.
- A `machinera` logger with retry decisions at `DEBUG` and job status changes at
  `INFO`, and an opt-in `MACHINERA_LOG=debug|info` environment variable.
- Pooled, reusable connections for job status polling; uploads and submissions keep
  connections that a deadline can close.
- Support for Python 3.10–3.14 and every `pydantic>=2,<3` release, an API reference,
  and runnable file, URL, large-file, recovery, error-handling, and asyncio examples
  with offline tests. The examples construct `Machinera()` and `AsyncMachinera()`, so
  `MACHINERA_API_KEY` and `MACHINERA_BASE_URL` apply.
- Contribution and security policies, documented versioning, reproducible
  development dependencies, and release artifact validation.
