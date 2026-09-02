"""M1 — archive everything (plan Phase 1).

Every message and every unique binary of the in-scope channels, durable in
Convex + R2. This is the irreplaceable milestone: everything downstream is
reproducible from what lands here, and nothing downstream ever needs Telegram
again.

Kill-safety comes from three things, not from a try/except:
  * the checkpoint is a per-channel high-water mark advanced only after the
    meta batch is in R2 (write-order law §4.1);
  * a resumed run replays at most one batch, and the §4.2 skip query means the
    replayed messages cost one Convex read instead of a re-download;
  * binaries are keyed by sha256, so a re-download of a message already
    archived converges on the same blob and the same `mediaObjects` row.
"""

from __future__ import annotations

import json
import shutil
import signal
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from . import convex, r2, telegram
from .config import REPO_ROOT

STAGE = "ingest"
SCHEMA_VERSION = 1
BATCH = 500  # messages per meta batch; also the resume replay window
FAILURE_ATTEMPT_CAP = 5  # §4.6 — past this a refKey waits for a human
TMP_DIR = REPO_ROOT / ".tmp" / "ingest"
# Telegram requires a size cap alongside `files=True`. 4 GiB is the largest a
# Telegram upload can be, so nothing in a channel is excluded by this.
TAKEOUT_MAX_FILE_BYTES = 4 * 1024**3


_stop = False


def _watch_for_interrupt() -> None:
    """Make Ctrl-C stop the run at the next message boundary.

    Telethon downloads through asyncio, which turns a KeyboardInterrupt raised
    mid-download into a cancelled task: the interrupt is swallowed and the
    message is recorded as a failure while the run carries on. Between messages
    is the only place the interrupt is ours to act on.
    """

    def handler(signum, frame):
        global _stop
        _stop = True
        _log("interrupt received — stopping at the next message")

    signal.signal(signal.SIGINT, handler)


def _log(message: str) -> None:
    # A run measured in days is watched through a pipe or a journal, and a
    # block-buffered stdout makes it look hung.
    print(message, flush=True)


# The binary-bearing media containers. Everything else (web page previews,
# polls, geo, contacts) is mediaType "none" — there is nothing to download.
_BINARY_MEDIA = ("MessageMediaPhoto", "MessageMediaDocument")


# --------------------------------------------------------------------------- #
# message -> row
# --------------------------------------------------------------------------- #


def _ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def media_type(msg) -> str:
    """The schema's `mediaType`, decided from the container then the attributes."""
    if type(msg.media).__name__ not in _BINARY_MEDIA:
        return "none"
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.video or msg.video_note or msg.gif:
        return "video"
    if msg.photo:
        return "photo"
    return "document"


def _forward(msg) -> tuple[str | None, int | None]:
    """(channel, message id) the message was forwarded from, best-effort.

    The channel is whatever identifies the source most durably and without a
    network round trip: username, then title, then the sender name Telegram
    left behind when the original account hides its profile.
    """
    fwd = msg.fwd_from
    if fwd is None:
        return None, None
    name = None
    try:
        chat = msg.forward.chat if msg.forward else None
        name = getattr(chat, "username", None) or getattr(chat, "title", None)
    except Exception:
        name = None
    if not name:
        name = getattr(fwd, "from_name", None)
    if not name and fwd.from_id is not None:
        from telethon import utils

        name = str(utils.get_peer_id(fwd.from_id))
    return name, getattr(fwd, "channel_post", None)


def message_args(channel_id: str, username: str, msg) -> dict:
    """The `upsertTelegramMessage` payload for one Telegram message."""
    forwarded_channel, forwarded_msg_id = _forward(msg)
    grouped = getattr(msg, "grouped_id", None)
    return {
        "channelId": channel_id,
        "telegramMessageId": msg.id,
        "date": _ms(msg.date),
        "editDate": _ms(getattr(msg, "edit_date", None)),
        "text": msg.message or None,
        "replyToMessageId": getattr(msg, "reply_to_msg_id", None),
        # int64 in MTProto — a JSON number would lose the low bits.
        "groupedId": str(grouped) if grouped else None,
        "telegramUrl": f"https://t.me/{username}/{msg.id}",
        "mediaType": media_type(msg),
        "isForwarded": msg.fwd_from is not None,
        "forwardedFromChannel": forwarded_channel,
        "forwardedFromMsgId": forwarded_msg_id,
    }


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        import base64

        return base64.b64encode(value).decode("ascii")
    return str(value)


def raw_dict(msg) -> dict:
    """The message exactly as Telegram sent it, for the `.jsonl.zst` batch."""
    return msg.to_dict()


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #


def ffprobe(path: Path) -> dict:
    """durationMs / codec / sampleRate / channelCount, or {} when unprobeable.

    Telegram's own durations are whole-second and unusable for the ms offsets
    §5 builds lesson timelines from, so the numbers have to come from here.
    """
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return {}
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    stream = next(
        (s for s in streams if s.get("codec_type") == "audio"),
        streams[0] if streams else None,
    )
    out: dict = {}
    fmt_duration = (data.get("format") or {}).get("duration")
    duration = fmt_duration or (stream or {}).get("duration")
    if duration:
        out["durationMs"] = round(float(duration) * 1000)
    if stream:
        if stream.get("codec_name"):
            out["codec"] = stream["codec_name"]
        if stream.get("sample_rate"):
            out["sampleRate"] = int(stream["sample_rate"])
        if stream.get("channels"):
            out["channelCount"] = int(stream["channels"])
    return out


def blob_key(sha256: str, ext: str) -> str:
    return f"blobs/{sha256[:2]}/{sha256}.{ext}"


def _download(client, msg, refetch, log):
    """Download `msg`'s binary, renewing an expired file reference once.

    A 500-message batch of lesson-sized audio takes hours to drain, and the file
    reference handed out with the message does not live that long. Re-fetching
    the message is the only way to get a fresh one.
    """
    from telethon.errors import FileReferenceExpiredError

    dest = str(TMP_DIR / f"{msg.id}")
    try:
        return client.download_media(msg, file=dest)
    except FileReferenceExpiredError:
        if refetch is None:
            raise
        log(f"  {msg.id} file reference expired, re-fetching")
        return client.download_media(refetch(msg.id), file=dest)


def ingest_media(
    client, s3, bucket: str, message_id: str, msg, counts, log=_log, refetch=None
) -> dict | None:
    """download -> sha256 -> ffprobe -> blob to R2 -> Convex rows -> delete temp.

    R2 before Convex (§4.1): a `mediaObjects` row always has its blob behind it.
    A repost costs the download — its sha256 is unknowable before then — but the
    existing row decides the key, so the same bytes can never end up under two
    of them, and there is nothing to upload or probe again.
    """
    path = _download(client, msg, refetch, log)
    if path is None:
        # Telethon returns None when it declined to download rather than when it
        # failed. Swallowing it would leave a message row claiming media, no
        # link, and a checkpoint past it — the one way M1 can lose a binary for
        # good. Make it a failure so §4.6 retries it.
        raise RuntimeError(f"download_media returned nothing for {msg.id}")
    path = Path(path)
    try:
        digest = r2.sha256_file(path)
        size = path.stat().st_size
        existing = convex.query("queries:mediaObjectBySha256", sha256=digest)
        if existing is not None:
            key, ext, probe = existing["r2Key"], existing["ext"], {}
            # Cheap self-healing: the row is authoritative, but if its blob is
            # gone the bytes are in hand right now (§4.4 repairs the rest).
            if r2.head(s3, bucket, key) is None:
                s3.upload_file(
                    str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}}
                )
                log(f"  {msg.id} restored missing blob {key}")
        else:
            ext = (path.suffix.lstrip(".") or "bin").lower()
            key = blob_key(digest, ext)
            probe = ffprobe(path)
            if r2.head(s3, bucket, key) is None:
                s3.upload_file(
                    str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}}
                )
        media = convex.mutation(
            "mutations:getOrCreateMediaObject",
            sha256=digest,
            r2Key=key,
            ext=ext,
            sizeBytes=size,
            mimeType=getattr(msg.file, "mime_type", None),
            **probe,
        )
        document = getattr(msg, "document", None)
        convex.mutation(
            "mutations:linkMessageMedia",
            messageId=message_id,
            mediaObjectId=media["id"],
            telegramDocId=str(document.id) if document is not None else None,
            originalFileName=getattr(msg.file, "name", None),
        )
        counts.audio_ms += probe.get("durationMs", 0)
        log(
            f"  {msg.id} {media_type(msg)} {size / 1e6:.1f}MB "
            f"{'new' if media['created'] else 'dedup'} {digest[:12]}"
        )
        return media
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# meta batches
# --------------------------------------------------------------------------- #


def meta_key(username: str, first: int, last: int) -> str:
    return f"meta/{username}/{first:08d}-{last:08d}.jsonl.zst"


def manifest_key(username: str) -> str:
    return f"meta/{username}/manifest.json"


def write_meta_batch(s3, bucket: str, username: str, raws: list[dict]) -> dict:
    """Compress one batch of raw messages to R2 and fold it into the manifest."""
    import hashlib
    import time

    first, last = raws[0]["id"], raws[-1]["id"]
    lines = "\n".join(
        json.dumps(raw, ensure_ascii=False, default=_json_default) for raw in raws
    )
    body = zstandard.ZstdCompressor(level=10).compress(lines.encode("utf-8"))
    key = meta_key(username, first, last)
    digest = hashlib.sha256(body).hexdigest()
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/zstd",
        Metadata={"sha256": digest},
    )
    batch = {
        "key": key,
        "from": first,
        "to": last,
        "count": len(raws),
        "sha256": digest,
        "createdAt": int(time.time() * 1000),
    }

    existing = r2.head(s3, bucket, manifest_key(username))
    manifest = (
        json.loads(
            s3.get_object(Bucket=bucket, Key=manifest_key(username))["Body"].read()
        )
        if existing
        else {
            "schemaVersion": SCHEMA_VERSION,
            "channel": username,
            "createdAt": batch["createdAt"],
            "batches": [],
        }
    )
    # Re-running a range replaces its batch rather than appending a duplicate.
    batches = [b for b in manifest["batches"] if b["key"] != key] + [batch]
    batches.sort(key=lambda b: b["from"])
    manifest["batches"] = batches
    manifest["count"] = sum(b["count"] for b in batches)
    manifest["range"] = {"from": batches[0]["from"], "to": batches[-1]["to"]}
    manifest["updatedAt"] = batch["createdAt"]
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key(username),
        Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return batch


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def _ingest_one(
    client, s3, bucket, channel_id, username, msg, counts, log=_log, refetch=None
) -> None:
    args = message_args(channel_id, username, msg)
    row = convex.mutation("mutations:upsertTelegramMessage", **args)
    if args["mediaType"] != "none":
        ingest_media(client, s3, bucket, row["id"], msg, counts, log=log, refetch=refetch)


def _flush(
    client, s3, bucket, channel_id, username, batch, counts, log, refetch=None
) -> int:
    """Ingest one batch, then publish its meta artifact and advance the checkpoint."""
    first, last = batch[0].id, batch[-1].id
    done = set(
        convex.query(
            "queries:ingestedMessageIds",
            channelId=channel_id,
            fromId=first,
            toId=last,
        )
    )
    for msg in batch:
        if _stop:
            # Before the meta artifact and the checkpoint, so the whole batch
            # replays on resume rather than being silently skipped past.
            raise KeyboardInterrupt
        counts.processed += 1
        if msg.id in done:
            counts.skipped += 1
            continue
        try:
            _ingest_one(
                client, s3, bucket, channel_id, username, msg, counts, log, refetch
            )
            counts.success += 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            counts.failure += 1
            convex.mutation(
                "mutations:recordFailure",
                stage=STAGE,
                refKey=f"{username}/{msg.id}",
                error=f"{type(exc).__name__}: {exc}"[:600],
            )
            log(f"  !! {username}/{msg.id}: {type(exc).__name__}: {exc}")

    # R2 artifact first, then the checkpoint that lets the next run skip it.
    written = write_meta_batch(s3, bucket, username, [raw_dict(m) for m in batch])
    convex.mutation(
        "mutations:checkpointChannel", channelId=channel_id, lastMessageId=last
    )
    log(
        f"  {username} {first}-{last}: {len(batch)} msgs "
        f"({counts.success} ok, {counts.skipped} skipped, {counts.failure} failed) "
        f"-> {written['key']}"
    )
    return last


def _drain_failures(
    client, worker, s3, bucket, entity, channel_id, username, counts, log, refetch=None
) -> None:
    """§4.6 — retry this stage's unresolved failures before touching new work."""
    rows = convex.query("queries:unresolvedFailures", stage=STAGE)
    mine = [
        row
        for row in rows
        if row["refKey"].startswith(f"{username}/")
        and row["attempts"] < FAILURE_ATTEMPT_CAP
    ]
    if not mine:
        capped = [r for r in rows if r["attempts"] >= FAILURE_ATTEMPT_CAP]
        if capped:
            log(f"  {len(capped)} failure(s) past the {FAILURE_ATTEMPT_CAP}-attempt cap")
        return
    log(f"  draining {len(mine)} unresolved failure(s) first")
    for row in mine:
        msg_id = int(row["refKey"].split("/", 1)[1])
        try:
            msg = client.get_messages(entity, ids=msg_id)
            if msg is None:
                raise LookupError("message no longer exists in the channel")
            _ingest_one(
                worker, s3, bucket, channel_id, username, msg, counts, log, refetch
            )
            convex.mutation("mutations:resolveFailure", stage=STAGE, refKey=row["refKey"])
            counts.success += 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            counts.failure += 1
            convex.mutation(
                "mutations:recordFailure",
                stage=STAGE,
                refKey=row["refKey"],
                error=f"{type(exc).__name__}: {exc}"[:600],
            )
            log(f"  !! retry {row['refKey']}: {type(exc).__name__}: {exc}")


def sync_channel(
    client,
    worker,
    s3,
    bucket: str,
    username: str,
    counts,
    limit: int | None = None,
    batch_size: int = BATCH,
    log=_log,
) -> dict:
    """Archive one channel from its checkpoint forward. Returns a summary.

    `client` is the plain session and `worker` the takeout one. Telegram only
    whitelists a few methods inside a takeout — history and file downloads, not
    username resolution — so metadata is looked up on the plain session and the
    entity object is handed to the takeout for the parts that need its limits.
    """
    entity = client.get_entity(username)
    channel = convex.mutation(
        "mutations:upsertChannel",
        username=username,
        title=getattr(entity, "title", username),
    )
    channel_id, start = channel["id"], channel["lastMessageId"]
    log(f"{username}: resuming after message {start}")

    def refetch(msg_id):
        """A message with a fresh file reference, off the plain session."""
        return client.get_messages(entity, ids=msg_id)

    _drain_failures(
        client, worker, s3, bucket, entity, channel_id, username, counts, log, refetch
    )

    batch: list = []
    seen = 0
    # Chronological: the checkpoint is only meaningful as a high-water mark if
    # ids arrive in ascending order.
    for msg in worker.iter_messages(entity, reverse=True, min_id=start, limit=limit):
        batch.append(msg)
        seen += 1
        if len(batch) >= batch_size:
            _flush(worker, s3, bucket, channel_id, username, batch, counts, log, refetch)
            batch = []
    if batch:
        _flush(worker, s3, bucket, channel_id, username, batch, counts, log, refetch)
    counts.notes.append(f"{username}:{seen}")
    return {"channel": username, "fetched": seen, "channelId": channel_id}


def _takeout_file() -> Path:
    """Where the takeout id lives, next to the session it belongs to."""
    return telegram.session_path().with_suffix(".takeout")


def _detach_takeout_id(path: Path) -> int | None:
    """Move a takeout id stock Telethon wrote into the session file out of it.

    Telethon 1.44.0 writes the sessions row in schema order
    (auth_key, takeout_id, tmp_auth_key) but unpacks it as
    (auth_key, tmp_auth_key, takeout_id) — so an open takeout's id is handed to
    `AuthKey()` on the next open and the session file stops loading at all.
    Returns the id that was rescued, if any.
    """
    if not path.exists():
        return None
    with sqlite3.connect(path) as db:
        row = db.execute("select takeout_id from sessions").fetchone()
        if row is None or not isinstance(row[0], int):
            return None
        db.execute("update sessions set takeout_id = null")
    sidecar = _takeout_file()
    if not sidecar.exists():
        sidecar.write_text(str(row[0]))
        sidecar.chmod(0o600)
    return row[0]


def _session():
    """The session file, with the takeout id kept out of it.

    ponytail: a sidecar file instead of the session column, because the column
    cannot survive a round-trip through Telethon 1.44.0 (see
    `_detach_takeout_id`). Delete all of this once Telethon reads its own row
    back in the order it wrote it.
    """
    from telethon.sessions import SQLiteSession

    path = telegram.session_path()
    _detach_takeout_id(path)

    class _Session(SQLiteSession):
        def _update_session_table(self):
            keep, self._takeout_id = self._takeout_id, None
            try:
                super()._update_session_table()
            finally:
                self._takeout_id = keep

    return _Session(str(path))


def _takeout_id(client) -> int | None:
    """The live takeout id, loaded from the sidecar the first time it is asked.

    A session that never held a takeout stores `b''` rather than NULL, and
    Telethon compares it with `is None` — so it thinks a takeout is open,
    refuses to start one, and then fails packing the empty bytes as an int64.
    Anything that is not an int is normalised away here.
    """
    value = getattr(client.session, "takeout_id", None)
    if isinstance(value, int):
        return value
    sidecar = _takeout_file()
    stored = int(sidecar.read_text().strip()) if sidecar.exists() else None
    client.session.takeout_id = stored
    return stored


def _store_takeout_id(client) -> None:
    """Persist the id Telethon just got, so the next run resumes this takeout."""
    value = getattr(client.session, "takeout_id", None)
    if isinstance(value, int):
        sidecar = _takeout_file()
        sidecar.write_text(str(value))
        sidecar.chmod(0o600)


def _finish_takeout(client) -> bool:
    """Close the takeout id stored in the session. Returns True if there was one.

    Done by hand rather than through Telethon's `takeout(...).success = True`:
    `_TakeoutClient.__setattr__` forwards every assignment to the wrapped client,
    so that flag never reaches the takeout and the session is silently left open.
    """
    takeout_id = _takeout_id(client)
    if takeout_id is None:
        return False
    from telethon.tl import functions

    client(
        functions.InvokeWithTakeoutRequest(
            takeout_id, functions.account.FinishTakeoutSessionRequest(success=True)
        )
    )
    client.session.takeout_id = None
    _takeout_file().unlink(missing_ok=True)
    return True


def open_client(takeout: bool, reset_takeout: bool = False):
    """The Telethon client to iterate with, wrapped in a takeout when asked.

    Takeout is the §9 pacing mitigation: Telegram grants an export session higher
    limits. `finalize=False` leaves the takeout id in the session file, so the
    next run continues the same export instead of asking Telegram for a new one
    (and Telegram only allows one open takeout per session anyway). A stored id
    of unknown scope is what `--reset-takeout` is for.
    """
    from telethon.errors import TakeoutInitDelayError
    from telethon.sync import TelegramClient

    from .config import require_env

    creds = require_env("TELEGRAM_API_ID", "TELEGRAM_API_HASH")
    client = TelegramClient(
        _session(),
        int(creds["TELEGRAM_API_ID"]),
        creds["TELEGRAM_API_HASH"],
    )
    client.flood_sleep_threshold = 24 * 3600  # sleep through flood waits, don't die
    client.connect()
    if not client.is_user_authorized():
        client.disconnect()
        raise RuntimeError("session is not signed in — run `archive telegram-login`")
    if not takeout:
        return client, client
    try:
        if reset_takeout:
            _finish_takeout(client)
        if _takeout_id(client) is None:
            worker = client.takeout(
                finalize=False,
                channels=True,
                files=True,
                max_file_size=TAKEOUT_MAX_FILE_BYTES,
            )
        else:
            worker = client.takeout(finalize=False)
        worker.__enter__()
        _store_takeout_id(client)
    except TakeoutInitDelayError as exc:
        client.disconnect()
        raise RuntimeError(
            f"Telegram wants {exc.seconds}s before granting a takeout — approve the "
            "data-export request in the Telegram app, or run with --no-takeout"
        ) from None
    except Exception:
        client.disconnect()
        raise
    return client, worker


def sync(
    channels: list[str],
    limit: int | None = None,
    takeout: bool = True,
    reset_takeout: bool = False,
    batch_size: int = BATCH,
    log=_log,
) -> list[dict]:
    """M1's batch command: archive every in-scope channel under the stage lock."""
    from . import pipeline

    results = []
    # ffprobe is where every ms offset in §5 comes from. Finding it missing one
    # media message at a time would just fill the failure ledger.
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is not on PATH — install ffmpeg before syncing")

    _watch_for_interrupt()
    with pipeline.stage(STAGE) as counts:
        pipeline.clear_dir(TMP_DIR)
        s3 = r2.client()
        bucket = r2.bucket()
        client, worker = open_client(takeout, reset_takeout=reset_takeout)
        try:
            for username in channels:
                results.append(
                    sync_channel(
                        client,
                        worker,
                        s3,
                        bucket,
                        username,
                        counts,
                        limit=limit,
                        batch_size=batch_size,
                        log=log,
                    )
                )
        finally:
            if worker is not client:
                with_exit = getattr(worker, "__exit__", None)
                if with_exit:
                    # finalize=False: leave the takeout open so the next run resumes it.
                    try:
                        with_exit(None, None, None)
                    except Exception:
                        pass
            client.disconnect()
            shutil.rmtree(TMP_DIR, ignore_errors=True)
    return results
