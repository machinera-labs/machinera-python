# Changelog

## Unreleased

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

- Polling a pending job now continues until the call's deadline; a deadline you set
  is the only thing that ends it. `RetryPolicy.max_polls` is now `int | None` and
  defaults to `None` (no count limit); a positive value remains a hard cap on status
  reads. Previously the default count cap could end a long job's polling with
  `DeadlineExceededError` well before a longer `deadline`.
- With `transport="auto"`, a synchronous request refused before any work was admitted
  (`inline_claim_timeout`, `inline_admission_refused`, or `no_serving_capacity`, when
  retryable) is submitted once as a durable job with the same body, operation key,
  and deadline instead of raising. Ambiguous and non-retryable failures never fall
  back.
- `str(error)` for an `APIError` ends with `(request_id: …)` when the request ID is
  known; `message` and `args` are unchanged.

### Added

- `RecoverableJobError`, the shared base of `DeadlineExceededError` and
  `TranscriptionInterrupted`, whose `job_id` names the job to resume, or is `None` when
  no job ID was observed; then repeat the call with the same operation key.
- `MachineraError.is_transient`, which is `True` when trying again later may succeed.
  It follows the same rule as the SDK's automatic retries, so it is never `True` for a
  response the SDK would not retry.
- A "Batch and evaluation harnesses" guide in the README.
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
  inline size limit `Limits.job_inline_body_bytes`, which defaults to the SDK's
  staged-upload threshold `STAGED_UPLOAD_THRESHOLD_BYTES`; inline job submission
  failures report `phase == "job_submit"`.
- Automatic staged uploads for files above that limit with bounded streaming,
  checksums, integrity validation, and safe retries. When the service reports staged
  uploads unavailable before granting an upload, a body within the service inline
  limit is submitted once as an inline durable job under the same operation key. Custom HTTP proxy and URL-mount settings
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
  including an upload initialization response or grant; it is never retried.
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
