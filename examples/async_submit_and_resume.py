from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict, cast

from machinera import (
    APIError,
    AsyncMachinera,
    DeadlineExceededError,
    TranscriptionInterrupted,
    TranscriptionResult,
)


class RecoveryState(TypedDict):
    operation_key: str
    job_id: str | None


def read_state(path: Path) -> RecoveryState:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("operation_key"), str)
        or not data["operation_key"]
        or "job_id" not in data
        or (data["job_id"] is not None and not isinstance(data["job_id"], str))
    ):
        raise ValueError("Invalid recovery state")
    return cast(RecoveryState, data)


def save_state(path: Path, state: RecoveryState) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".recovery-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def submit(
    client: AsyncMachinera, url: str, path: Path, deadline: float
) -> TranscriptionResult | None:
    state: RecoveryState = (
        await asyncio.to_thread(read_state, path)
        if await asyncio.to_thread(path.exists)
        else {"operation_key": uuid.uuid4().hex, "job_id": None}
    )
    if state["job_id"] is not None:
        raise ValueError("A job ID is already saved; use resume")
    await asyncio.to_thread(save_state, path, state)
    try:
        result = await client.transcribe_url(
            url,
            model="transcribe-v1",
            idempotency_key=state["operation_key"],
            deadline=deadline,
        )
    except asyncio.CancelledError as error:
        state["job_id"] = getattr(error, "job_id", None)
        await asyncio.to_thread(save_state, path, state)
        raise
    except APIError as error:
        state["job_id"] = error.job_id
        await asyncio.to_thread(save_state, path, state)
        if isinstance(error, (DeadlineExceededError, TranscriptionInterrupted)):
            return None
        raise
    state["job_id"] = result.job_id
    await asyncio.to_thread(save_state, path, state)
    return result


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist and resume a durable server job.")
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    submission = commands.add_parser("submit")
    submission.add_argument("url")
    submission.add_argument(
        "--deadline", type=float, default=5.0, help="Wait budget in seconds (default: %(default)s)"
    )
    commands.add_parser("resume")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        async with AsyncMachinera() as client:
            if args.command == "submit":
                result = await submit(client, args.url, args.state, args.deadline)
            else:
                state = (
                    await asyncio.to_thread(read_state, args.state)
                    if await asyncio.to_thread(args.state.exists)
                    else None
                )
                if state is None or state["job_id"] is None:
                    print("No job ID saved; repeat submit with the identical URL.", file=sys.stderr)
                    return 2
                if args.command == "status":
                    snapshot = await client.get_job(state["job_id"])
                    print(snapshot.status)
                    return 0
                result = await client.resume(state["job_id"])
    except (APIError, OSError, ValueError, TypeError):
        print(
            "Operation failed; retain the state file and follow "
            "https://github.com/machinera-labs/machinera-python/blob/main/api.md#failure-handling",
            file=sys.stderr,
        )
        return 1
    if result is None:
        print("Recovery state saved; see the example README for the next step.", file=sys.stderr)
        return 0
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
