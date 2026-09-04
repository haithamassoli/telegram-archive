"""M2 — transcribe every unique audio binary (plan Phase 2).

One `partTranscripts` row per (sha256, configHash), one JSON artifact per row at
`transcripts/{sha256}/{configHash}.json`. The GPU is the clock; everything here
exists so that no second of it is ever spent twice.

Three gates, cheapest first:
  * §4.2 fast path — a `done` Convex row is skipped without touching R2 or the GPU;
  * §4.3 crash window — a pending/failed/stale row whose artifact is already in R2
    is validated and promoted to `done`, never re-inferred;
  * only what is left reaches the model, in batches, under the stage lock.

Identity is `sha256 + configHash`, so an artifact the pinned config did not
produce must never be filed under it: `validate_artifact` compares the JSON's own
provenance against the pin and a mismatch is a failure, never a new identity.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import convex, r2
from .config import CONFIG_HASH, PINNED_CONFIG, REPO_ROOT

STAGE = "transcribe"
BATCH = 500  # files per transcriber call, per plan Phase 2
SCAN_PAGE = 500
FAILURE_ATTEMPT_CAP = 5  # §4.6 — past this a sha256 waits for a human
STALE_PROCESSING_MS = 5 * 60 * 1000  # §4.3, same window as the stage lock
PREFIX = "transcripts/"
TMP_DIR = REPO_ROOT / ".tmp" / "asr"
OUT_DIR = TMP_DIR / "out"

# Fallback for the rare binary Telegram handed over without a mime type.
AUDIO_EXTS = {
    "mp3", "m4a", "oga", "ogg", "opus", "wav", "flac", "aac", "mpga", "amr", "wma",
}  # fmt: skip


def _log(message: str) -> None:
    # A GPU campaign is measured in days and watched through a pipe.
    print(message, flush=True)


class ProvenanceMismatch(RuntimeError):
    """The artifact was not produced by the pinned config (§0.5 / Phase 2)."""


def transcript_key(sha256: str, config_hash: str = CONFIG_HASH) -> str:
    return f"{PREFIX}{sha256}/{config_hash}.json"


def key_sha256(key: str) -> str | None:
    """The sha256 a transcript key belongs to, or None if it is not one of ours."""
    parts = key.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == PREFIX.rstrip("/") else None


def is_audio(row: dict) -> bool:
    """Whether this binary is one of the archive's audio parts.

    Either signal is enough: Telegram sends some audio as `application/octet-stream`,
    and some without a mime type at all, and a binary missing from M2's queue is
    also missing from the denominator its ≥99% exit criterion is measured against.
    No non-audio container in the archive carries an audio extension.

    ponytail: video is out of M2's scope by the plan's wording ("unique audio
    sha256s"); `coverage()` reports what that excluded, so the decision stays
    visible instead of silent.
    """
    mime = (row.get("mimeType") or "").lower()
    return mime.startswith("audio/") or (row.get("ext") or "").lower() in AUDIO_EXTS


def validate_artifact(data: dict) -> dict:
    """Check a transcript JSON against the pinned config; return its facts.

    The artifact records every pinned field itself, so this is the same check for
    a transcript that just came off the GPU and for one recovered from R2.
    """
    asr = (data.get("models") or {}).get("asr") or {}
    found = {
        "model": asr.get("id"),
        "modelRevision": asr.get("revision"),
        "language": data.get("language"),
        "vad": data.get("segmentation"),
        "vadMerge": (data.get("segmentation_details") or {}).get("merge"),
        "alignment": data.get("timing"),
    }
    drift = {
        key: (value, PINNED_CONFIG[key])
        for key, value in found.items()
        if value != PINNED_CONFIG[key]
    }
    if drift:
        detail = ", ".join(
            f"{k}: {got!r} != pinned {want!r}" for k, (got, want) in drift.items()
        )
        raise ProvenanceMismatch(detail)
    duration = (data.get("source") or {}).get("duration_seconds")
    return {
        "model": found["model"],
        "modelRevision": found["modelRevision"],
        "durationMs": round(duration * 1000) if duration else None,
        "segmentCount": len(data.get("segments") or []),
    }


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


def scan(config_hash: str = CONFIG_HASH, page: int = SCAN_PAGE):
    """Every `mediaObjects` row with its transcript status, in sha256 order."""
    cursor = ""
    while True:
        result = convex.query(
            "queries:mediaObjectsPage",
            configHash=config_hash,
            cursor=cursor,
            limit=page,
        )
        yield from result["objects"]
        cursor = result["nextCursor"]
        if cursor is None:
            return


def needs_work(row: dict, now_ms: int | None = None) -> bool:
    """§4.2: only a `done` record is skipped without a look at R2."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    status = row.get("status")
    if status == "done":
        return False
    if status == "processing":
        # Fresh `processing` belongs to a run that died less than the stale window
        # ago; leave it to the next run rather than racing its artifact.
        return now_ms - (row.get("processingStartedAt") or 0) > STALE_PROCESSING_MS
    return True


# --------------------------------------------------------------------------- #
# Convex writes
# --------------------------------------------------------------------------- #


def _mark(sha256: str, config_hash: str, status: str, **fields) -> None:
    convex.mutation(
        "mutations:upsertPartTranscript",
        sha256=sha256,
        configHash=config_hash,
        status=status,
        **fields,
    )


def _fail(sha256: str, config_hash: str, exc: Exception, counts, log) -> None:
    error = f"{type(exc).__name__}: {exc}"[:600]
    counts.failure += 1
    _mark(sha256, config_hash, "failed", error=error)
    convex.mutation("mutations:recordFailure", stage=STAGE, refKey=sha256, error=error)
    log(f"  !! {sha256[:12]}: {error}")


def _succeed(sha256, config_hash, key, facts, counts, retrying, log, how) -> None:
    """Write-order law §4.1: this is only ever called with the artifact in R2."""
    _mark(sha256, config_hash, "done", rawR2Key=key, **facts)
    if sha256 in retrying:
        convex.mutation("mutations:resolveFailure", stage=STAGE, refKey=sha256)
    counts.audio_ms += facts["durationMs"] or 0
    log(
        f"  {sha256[:12]} {how} {facts['segmentCount']} segment(s), "
        f"{(facts['durationMs'] or 0) / 60000:.1f} min"
    )


# --------------------------------------------------------------------------- #
# the GPU batch
# --------------------------------------------------------------------------- #


def open_transcriber():
    """A persistent `Transcriber` on the pinned config, publishing JSON."""
    from cohere_transcribe import PublicationOptions, Transcriber, TranscriptionOptions

    from .config import transcriber_kwargs

    return Transcriber(
        TranscriptionOptions(
            **transcriber_kwargs(),
            publication=PublicationOptions(
                formats=("json",),
                output_dir=str(OUT_DIR),
                # §4.3's same-machine belt: a file already published in this run
                # is reused instead of erroring the whole batch.
                existing="skip",
            ),
        )
    )


def _cleanup(sha256: str, blob: Path | None) -> None:
    if blob is not None:
        blob.unlink(missing_ok=True)
    (OUT_DIR / f"{sha256}.json").unlink(missing_ok=True)
    for sidecar in OUT_DIR.glob(f".{sha256}.*"):
        sidecar.unlink(missing_ok=True)


def process_batch(
    transcriber, s3, bucket, rows, counts, retrying, config_hash, log=_log
) -> None:
    """Materialize, transcribe, then per file: R2 artifact, then Convex `done`."""
    blobs: dict[str, Path] = {}
    for row in rows:
        sha256 = row["sha256"]
        try:
            blob = TMP_DIR / f"{sha256}.{row['ext']}"
            s3.download_file(bucket, row["r2Key"], str(blob))
            blobs[sha256] = blob
        except Exception as exc:
            _fail(sha256, config_hash, exc, counts, log)

    for sha256 in blobs:
        _mark(sha256, config_hash, "processing", processingRunId=counts.run_id)

    log(f"  transcribing {len(blobs)} file(s)")
    try:
        run = transcriber.transcribe([str(path) for path in blobs.values()])
    except Exception as exc:
        # A run-level error (OOM, a poison file the decoder dies on) blames no
        # single file, and recording nothing would re-select this exact batch
        # forever at zero progress. Charge every file in it an attempt so the
        # §4.6 cap eventually surfaces the batch instead of looping on it.
        log(f"  !! batch failed: {type(exc).__name__}: {exc}")
        for sha256, blob in blobs.items():
            _fail(sha256, config_hash, exc, counts, log)
            _cleanup(sha256, blob)
        return
    results = {Path(result.path).stem: result for result in run.results}

    for sha256, blob in blobs.items():
        try:
            result = results.get(sha256)
            if result is None:
                raise RuntimeError("the transcriber returned no result for this file")
            if result.status == "failed":
                raise RuntimeError(result.error or "transcription failed")
            artifact = OUT_DIR / f"{sha256}.json"
            body = artifact.read_bytes()
            facts = validate_artifact(json.loads(body))
            key = transcript_key(sha256, config_hash)
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                Metadata={"sha256": sha256, "configHash": config_hash},
            )
            _succeed(sha256, config_hash, key, facts, counts, retrying, log, "done")
            counts.success += 1
        except Exception as exc:
            _fail(sha256, config_hash, exc, counts, log)
        finally:
            _cleanup(sha256, blob)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def coverage(rows: list[dict], config_hash: str = CONFIG_HASH) -> dict:
    """M2's exit criterion: the share of unique audio binaries that are `done`."""
    audio = [row for row in rows if is_audio(row)]
    done = [row for row in audio if row.get("status") == "done"]
    return {
        "objects": len(rows),
        "audio": len(audio),
        "notAudio": len(rows) - len(audio),
        "done": len(done),
        "coverage": round(len(done) / len(audio), 5) if audio else 0.0,
        "configHash": config_hash,
    }


def transcribe(
    limit: int | None = None,
    batch_size: int = BATCH,
    sha256s: tuple[str, ...] = (),
    config_hash: str = CONFIG_HASH,
    log=_log,
) -> dict:
    """M2's batch command: drive every un-transcribed audio binary to `done`."""
    from . import pipeline

    with pipeline.stage(STAGE) as counts:
        # ponytail: the whole batch is materialized before inference, so peak
        # scratch is batch_size x file size (~5 GB at 500 lesson-sized parts).
        # Lower --batch-size if the disk is smaller than that.
        pipeline.clear_dir(TMP_DIR)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        s3, bucket = r2.client(), r2.bucket()

        # §4.6: this stage's unresolved failures are retried before new work.
        failures = {
            row["refKey"]: row
            for row in convex.query("queries:unresolvedFailures", stage=STAGE)
        }
        capped = {k for k, v in failures.items() if v["attempts"] >= FAILURE_ATTEMPT_CAP}
        retrying = set(failures) - capped

        rows = list(scan(config_hash))
        stats = coverage(rows, config_hash)
        log(
            f"{stats['audio']} unique audio binaries, {stats['done']} already done "
            f"({stats['coverage']:.1%}), {stats['notAudio']} non-audio object(s) skipped"
        )
        if capped:
            log(f"  {len(capped)} failure(s) past the {FAILURE_ATTEMPT_CAP}-attempt cap")

        wanted = set(sha256s)
        candidates = [
            row
            for row in rows
            if is_audio(row)
            and needs_work(row)
            and row["sha256"] not in capped
            and (not wanted or row["sha256"] in wanted)
        ]
        # Failures first (§4.6), then deterministic sha256 order.
        candidates.sort(key=lambda row: (row["sha256"] not in retrying, row["sha256"]))
        if limit is not None:
            candidates = candidates[:limit]

        # One LIST instead of a HEAD per candidate — 11k round trips is a
        # quarter of an hour of nothing.
        published = r2.list_keys(s3, bucket, PREFIX)
        queue: list[dict] = []
        recovered = 0
        for row in candidates:
            counts.processed += 1
            sha256 = row["sha256"]
            key = transcript_key(sha256, config_hash)
            if key not in published:
                queue.append(row)
                continue
            try:  # §4.3: the artifact is already there — validate and promote.
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                facts = validate_artifact(json.loads(body))
            except Exception as exc:
                log(f"  {sha256[:12]} published artifact rejected ({exc}) — redoing")
                queue.append(row)
                continue
            _succeed(sha256, config_hash, key, facts, counts, retrying, log, "recovered")
            counts.skipped += 1
            recovered += 1

        if recovered:
            log(f"  recovered {recovered} artifact(s) from R2 without inference")
        log(f"  {len(queue)} file(s) to transcribe")

        transcriber = None
        try:
            for start in range(0, len(queue), batch_size):
                if transcriber is None:
                    log("  loading the model")
                    transcriber = open_transcriber()
                process_batch(
                    transcriber,
                    s3,
                    bucket,
                    queue[start : start + batch_size],
                    counts,
                    retrying,
                    config_hash,
                    log,
                )
        finally:
            if transcriber is not None:
                transcriber.close()
            shutil.rmtree(TMP_DIR, ignore_errors=True)

        done_now = stats["done"] + counts.success + recovered
        counts.notes.append(
            f"audio={stats['audio']} done={done_now} failed={counts.failure}"
        )
        return {
            **stats,
            "candidates": len(candidates),
            "recovered": recovered,
            "transcribed": counts.success,
            "failed": counts.failure,
            "doneNow": done_now,
            "coverageNow": (
                round(done_now / stats["audio"], 5) if stats["audio"] else 0.0
            ),
        }


# --------------------------------------------------------------------------- #
# reconcile-artifacts (§4.4), part-transcript mode
# --------------------------------------------------------------------------- #


def reconcile(config_hash: str = CONFIG_HASH, dry_run: bool = False, log=_log) -> dict:
    """A) done + no artifact -> reprocess. B) artifact + not done -> repair.
    C) an artifact nothing references is reported, never deleted.

    Under the same stage lock as `transcribe`, because it patches the same rows:
    without it, a running worker's freshly-`done` row would be flipped to
    `failed` by a reconcile that listed R2 a second before the upload landed.
    """
    from . import pipeline

    with pipeline.stage(STAGE) as counts:
        return _reconcile(config_hash, dry_run, log, counts)


def _reconcile(config_hash: str, dry_run: bool, log, counts) -> dict:
    s3, bucket = r2.client(), r2.bucket()
    # Convex first, R2 second: an artifact uploaded between the two reads then
    # shows up as "present but not done" (repairable) rather than as a `done`
    # row with a missing artifact (a false alarm that costs a re-transcription).
    rows = list(scan(config_hash))
    published = r2.list_keys(s3, bucket, PREFIX)
    stats = coverage(rows, config_hash)
    known = {row["sha256"] for row in rows}

    missing, repaired, invalid = [], [], []
    for row in rows:
        if not is_audio(row):
            continue
        sha256 = row["sha256"]
        key = transcript_key(sha256, config_hash)
        if row["status"] == "done" and key not in published:
            missing.append(sha256)
            if not dry_run:
                _mark(
                    sha256,
                    config_hash,
                    "failed",
                    error="artifact missing from R2 (reconcile)",
                )
                convex.mutation(
                    "mutations:recordFailure",
                    stage=STAGE,
                    refKey=sha256,
                    error="artifact missing from R2 (reconcile)",
                )
        elif row["status"] != "done" and key in published:
            try:
                facts = validate_artifact(
                    json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
                )
            except Exception as exc:
                invalid.append((sha256, str(exc)[:200]))
                continue
            repaired.append(sha256)
            if not dry_run:
                _mark(sha256, config_hash, "done", rawR2Key=key, **facts)
                # Otherwise the ops page keeps showing a failure for a sha256
                # that is now done, and no later run will ever clear it.
                convex.mutation("mutations:resolveFailure", stage=STAGE, refKey=sha256)

    superseded, orphans = [], []
    for key in sorted(published):
        sha256 = key_sha256(key)
        if sha256 is None or sha256 not in known:
            orphans.append(key)
        elif not key.endswith(f"/{config_hash}.json"):
            superseded.append(key)

    for sha256 in missing:
        log(f"  !! {sha256}: Convex says done, R2 has no artifact — marked for redo")
    for sha256, why in invalid:
        log(f"  !! {sha256}: artifact present but rejected — {why}")
    for key in orphans:
        log(f"  ?? orphan artifact, no mediaObject references it: {key}")
    # Coverage is M2's exit criterion, so it is reported after the repairs, not
    # before: a `done` row whose artifact is gone is not coverage.
    done_now = stats["done"] - len(missing) + len(repaired)
    log(
        f"{stats['audio']} audio binaries, {done_now} done "
        f"({done_now / stats['audio']:.2%}) for configHash {config_hash[:12]}"
        if stats["audio"]
        else "no audio binaries archived yet"
    )
    log(
        f"  {len(missing)} missing artifact(s), {len(repaired)} repaired, "
        f"{len(invalid)} invalid, {len(superseded)} superseded, {len(orphans)} orphan(s)"
        + (" (dry run — nothing written)" if dry_run else "")
    )
    counts.processed = stats["audio"]
    counts.success = len(repaired)
    counts.failure = len(missing) + len(invalid)
    counts.notes.append(f"audio={stats['audio']} done={done_now} orphans={len(orphans)}")
    return {
        **stats,
        "doneNow": done_now,
        "missing": missing,
        "repaired": repaired,
        "invalid": invalid,
        "superseded": superseded,
        "orphans": orphans,
        "dryRun": dry_run,
    }
