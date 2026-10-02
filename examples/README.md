# Examples

From the repository root, install with `python -m pip install -e .` and set
`MACHINERA_API_KEY` in your environment. Each example constructs `Machinera()` or
`AsyncMachinera()`, which read `MACHINERA_API_KEY` and, when set, `MACHINERA_BASE_URL`
from the environment, and uses the `transcribe-v1` model.
Live calls submit audio for transcription and may incur usage charges.

```sh
python examples/transcribe_file.py recording.wav
python examples/transcribe_file.py recording.flac
python examples/transcribe_file.py recording.mp3
python examples/transcribe_url.py https://audio.example/recording.wav
python examples/handle_errors.py recording.wav
```

Replace the sample URL with a direct audio URL you are authorized to use. The scripts
write the exact transcript to stdout, without trimming or adding a newline. Redirect
stdout to a private file if needed; do not send it to application logs. Avoid putting
sensitive signed URLs in shell history. `handle_errors.py` prints only fixed recovery
guidance to stderr and returns exit code 1 on API failures or local argument errors.
It catches `APIError` for service and transport recovery metadata, and
`ValueError`/`TypeError` for local configuration and input validation. The SDK
performs bounded safe retries; applications must distinguish retrying from recovering an accepted job,
resolving an ambiguous submission, and correcting a permanent failure. The README's
recovery table is the complete decision order.

## Asyncio

```sh
python examples/async_transcribe_file.py recording.wav
python examples/async_submit_and_resume.py --state recovery.json submit https://audio.example/recording.wav
python examples/async_submit_and_resume.py --state recovery.json status
python examples/async_submit_and_resume.py --state recovery.json resume
```

These examples await `AsyncMachinera` methods and use `async with` for cleanup.
The recovery example saves the operation key before submission and the job ID on
completion, deadline, or cancellation. Cancellation is re-raised after saving state;
the server job continues. The state-file and replay guidance below applies to both
clients. `submit_and_resume.py` is the corresponding blocking-client example.

## Large files

Save a fresh random operation key before starting each independent transcription.
The same file call automatically stages inputs above the durable-job inline limit
(`Limits.job_inline_body_bytes`):

```sh
python examples/transcribe_large_file.py recording.flac --operation-key YOUR_SAVED_KEY
python examples/transcribe_large_file.py recording.flac --operation-key YOUR_SAVED_KEY --resume
```

On failure, the example writes recovery context to stderr; keep it private.
Resolve the failure before resuming with identical bytes and options. If a job ID
is available, add `--job-id SAVED_JOB_ID` to the `--resume` command so recovery only
polls; optionally pass `--upload-id SAVED_UPLOAD_ID` to preserve context. Before
admission, `--resume` replays staged initialization, PUT and submission with the
saved key. This recovery
form is for staged inputs; inline job recovery uses the original keyed file call
until a job ID is known. Do not reuse the sample key for independent operations.

## Submit, save, and resume

The blocking example uses durable server jobs. There is no submit-only method. The submit
command waits up to its `--deadline` (the default is in `--help`), saving the accepted
job ID if the deadline expires or the call is interrupted. If the job finishes
sooner, it saves the ID and prints the result immediately. A timeout does not cancel the server job.

```sh
python examples/submit_and_resume.py --state recovery.json submit https://audio.example/recording.wav
python examples/submit_and_resume.py --state recovery.json status
python examples/submit_and_resume.py --state recovery.json resume
```

The state file is written before submission with a fresh operation key and updated
atomically with the job ID. On POSIX systems it has owner-only permissions. Keep it
private and use a separate file for each independent transcription. Do not run
simultaneous commands with the same state file. Keep the same credentials and endpoint
for recovery. The file contains no source URL, audio, or transcript.

If no job ID was recovered, repeat `submit` with the same state file and **identical
URL and options** to reuse the saved operation key. The URL's content must also remain
unchanged. Do not discard state to work around a replay-unavailable or terminal error;
reconcile the original operation first. Once an ID is saved, use `resume`, which only
reads that job and never submits another. Use `status` for one read, which prints
the typed snapshot's `status` attribute. An expired result must be reconciled; save
successful output before service retention ends. The command returns 0 on completion
or a saved deadline/interruption, 1 on failure, and 2 when status or resume has no saved ID,
including when the state file does not exist.

Run offline checks with `pytest -q tests/test_examples.py`. They use stubbed HTTP
responses and require no live key or service access.
