"""Batch-command harness: local flock + Convex stage lock + run history (§4.5).

Every batch command in every milestone runs inside `stage()`. It guarantees one
worker per stage (locally and across machines), a 60 s heartbeat so a crashed
run's lock goes stale instead of wedging, and an appended `pipelineRuns` row
with counts whichever way the run ends.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import socket
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import convex
from .config import REPO_ROOT

HEARTBEAT_S = 60
LOCK_DIR = REPO_ROOT / ".locks"


@dataclass
class Counts:
    """The counters a `pipelineRuns` row records."""

    run_id: str
    processed: int = 0
    success: int = 0
    failure: int = 0
    skipped: int = 0
    audio_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def as_run_args(self) -> dict:
        return {
            "processedCount": self.processed,
            "successCount": self.success,
            "failureCount": self.failure,
            "skippedCount": self.skipped,
            "audioDurationMs": self.audio_ms or None,
            "summary": "; ".join(self.notes) or None,
        }


class StageBusy(RuntimeError):
    """Someone else is running this stage."""


@contextlib.contextmanager
def stage(name: str):
    """Hold stage `name` for the duration of the block. Yields its `Counts`."""
    counts = Counts(run_id=uuid.uuid4().hex)
    owner = f"{socket.gethostname()}:{os.getpid()}"
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    handle = (LOCK_DIR / f"{name}.lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise StageBusy(f"another `{name}` run holds the local lock") from None

    try:
        got = convex.mutation(
            "mutations:acquirePipelineStage",
            stage=name,
            runId=counts.run_id,
            owner=owner,
        )
    except Exception:
        handle.close()
        raise
    if not got["acquired"]:
        handle.close()
        raise StageBusy(
            f"stage `{name}` held by run {got['holder']} ({got.get('owner')}) — "
            "wait for it, or for its heartbeat to go stale (5 min)"
        )
    try:
        convex.mutation("mutations:startPipelineRun", runId=counts.run_id, stage=name)
    except Exception:
        # The stage lock is already ours; wedging it for the full stale window
        # over a failed bookkeeping insert would be a bad trade.
        with contextlib.suppress(Exception):
            convex.mutation(
                "mutations:releasePipelineStage", stage=name, runId=counts.run_id
            )
        handle.close()
        raise

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_S):
            # A missed heartbeat is survivable (5 min of slack); a heartbeat
            # thread that dies on a blip is not.
            with contextlib.suppress(Exception):
                convex.mutation(
                    "mutations:heartbeatPipelineStage", stage=name, runId=counts.run_id
                )

    threading.Thread(target=beat, daemon=True).start()

    status = "failed"
    try:
        yield counts
        status = "done"
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        stop.set()
        # The run row and the lock release must both be attempted even when the
        # body blew up; neither failure should mask the original exception.
        with contextlib.suppress(Exception):
            convex.mutation(
                "mutations:finishPipelineRun",
                runId=counts.run_id,
                status=status,
                **counts.as_run_args(),
            )
        with contextlib.suppress(Exception):
            convex.mutation(
                "mutations:releasePipelineStage", stage=name, runId=counts.run_id
            )
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def clear_dir(path: Path) -> Path:
    """A fresh, empty scratch dir. Called under the stage lock, so wiping is safe
    and is what guarantees no temp file outlives a crashed run."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path
