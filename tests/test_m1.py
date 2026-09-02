"""M1 checks. Run: `python tests/test_m1.py` (also collectable by pytest).

The interesting M1 logic is not "does Telethon work" — it is whether a killed
run resumes without duplicating rows, losing messages, or re-downloading what
is already archived. That is exercised here against fakes: an in-memory Convex
that behaves like the real mutations (lookup-then-write, serializable), an
in-memory R2, and a Telegram channel of scripted messages. The fake Convex is
kept honest by `test_fake_convex_matches_the_deployed_signatures`, which reads
the real mutations.ts and asserts every path and argument name lines up.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
import tempfile
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import zstandard

from archive import ingest, pipeline

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeConvex:
    """The deployed mutations, re-implemented over dicts. Same invariants."""

    def __init__(self):
        self.channels: dict[str, dict] = {}
        self.messages: dict[tuple[str, int], dict] = {}
        self.media: dict[str, dict] = {}
        self.links: list[dict] = []
        self.failures: dict[tuple[str, str], dict] = {}
        self.runs: list[dict] = []
        self.calls: list[str] = []
        self._ids = 0

    def _new_id(self, table: str) -> str:
        self._ids += 1
        return f"{table}#{self._ids}"

    def mutation(self, path: str, **args):
        self.calls.append(path)
        name = path.split(":", 1)[1]
        return getattr(self, f"m_{name}")(**args)

    def query(self, path: str, **args):
        self.calls.append(path)
        return getattr(self, f"q_{path.split(':', 1)[1]}")(**args)

    # -- mutations ----------------------------------------------------------
    def m_upsertChannel(self, username, title):
        row = self.channels.get(username)
        if row is None:
            row = {
                "_id": self._new_id("channels"),
                "username": username,
                "title": title,
                "lastMessageId": 0,
            }
            self.channels[username] = row
            return {"id": row["_id"], "lastMessageId": 0, "created": True}
        row["title"] = title
        return {"id": row["_id"], "lastMessageId": row["lastMessageId"], "created": False}

    def m_checkpointChannel(self, channelId, lastMessageId):
        row = next(c for c in self.channels.values() if c["_id"] == channelId)
        row["lastMessageId"] = max(row["lastMessageId"], lastMessageId)
        return {"lastMessageId": row["lastMessageId"]}

    def m_upsertTelegramMessage(self, channelId, telegramMessageId, **rest):
        key = (channelId, telegramMessageId)
        existing = self.messages.get(key)
        if existing is None:
            row = {
                "_id": self._new_id("telegramMessages"),
                "channelId": channelId,
                "telegramMessageId": telegramMessageId,
                "semanticType": None,
                **rest,
            }
            self.messages[key] = row
            return {"id": row["_id"], "created": True}
        # Patch the ingest-owned fields; semanticType survives.
        existing.update(rest)
        return {"id": existing["_id"], "created": False}

    def m_getOrCreateMediaObject(self, sha256, **rest):
        existing = self.media.get(sha256)
        if existing is not None:
            return {"id": existing["_id"], "created": False}
        row = {"_id": self._new_id("mediaObjects"), "sha256": sha256, **rest}
        self.media[sha256] = row
        return {"id": row["_id"], "created": True}

    def m_linkMessageMedia(self, messageId, mediaObjectId, **rest):
        for link in self.links:
            if link["messageId"] == messageId and link["mediaObjectId"] == mediaObjectId:
                return {"id": link["_id"], "created": False}
        link = {
            "_id": self._new_id("messageMedia"),
            "messageId": messageId,
            "mediaObjectId": mediaObjectId,
            **rest,
        }
        self.links.append(link)
        return {"id": link["_id"], "created": True}

    def m_recordFailure(self, stage, refKey, error):
        row = self.failures.setdefault((stage, refKey), {"attempts": 0})
        row.update(
            stage=stage,
            refKey=refKey,
            error=error,
            attempts=row["attempts"] + 1,
            resolved=False,
        )
        return {"attempts": row["attempts"]}

    def m_resolveFailure(self, stage, refKey):
        row = self.failures.get((stage, refKey))
        if row is None:
            return {"resolved": False}
        row["resolved"] = True
        return {"resolved": True}

    def m_startPipelineRun(self, runId, stage):
        self.runs.append({"runId": runId, "stage": stage, "status": "running"})
        return {"id": runId}

    def m_finishPipelineRun(self, runId, **counts):
        run = next(r for r in self.runs if r["runId"] == runId)
        run.update(counts)
        return {"id": runId}

    def m_acquirePipelineStage(self, stage, runId, owner):
        return {"acquired": True, "holder": runId}

    def m_heartbeatPipelineStage(self, stage, runId):
        return {"held": True}

    def m_releasePipelineStage(self, stage, runId):
        return {"released": True}

    # -- queries ------------------------------------------------------------
    def q_ingestedMessageIds(self, channelId, fromId, toId):
        done = []
        for (chan, msg_id), row in self.messages.items():
            if chan != channelId or not (fromId <= msg_id <= toId):
                continue
            if row["mediaType"] == "none":
                done.append(msg_id)
            elif any(link["messageId"] == row["_id"] for link in self.links):
                done.append(msg_id)
        return sorted(done)

    def q_mediaObjectBySha256(self, sha256):
        return self.media.get(sha256)

    def q_unresolvedFailures(self, stage):
        return [
            row
            for (s, _), row in self.failures.items()
            if s == stage and not row["resolved"]
        ]


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body
        self.puts += 1

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def upload_file(self, filename, Bucket, Key, ExtraArgs=None):
        self.objects[Key] = Path(filename).read_bytes()
        self.puts += 1

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


@dataclass
class FakeFile:
    mime_type: str | None = None
    name: str | None = None


class MessageMediaDocument:  # name-matched to MTProto; media_type() reads the class
    def __init__(self, doc_id=1):
        self.id = doc_id


class MessageMediaPhoto:
    pass


@dataclass
class FakeMessage:
    id: int
    message: str | None = None
    payload: bytes | None = None  # the "binary" this message carries
    kind: str = "none"  # the mediaType it should resolve to
    grouped_id: int | None = None
    reply_to_msg_id: int | None = None
    fwd_from: object | None = None
    edit_date: datetime | None = None
    date: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=UTC))
    file: FakeFile = field(default_factory=FakeFile)
    forward: object | None = None

    @property
    def media(self):
        if self.kind == "none":
            return None
        return MessageMediaPhoto() if self.kind == "photo" else MessageMediaDocument()

    @property
    def document(self):
        if self.kind in ("none", "photo"):
            return None
        return MessageMediaDocument(self.id)

    voice = property(lambda self: self.kind == "voice" or None)
    audio = property(lambda self: self.kind == "audio" or None)
    video = property(lambda self: self.kind == "video" or None)
    video_note = property(lambda self: None)
    gif = property(lambda self: None)
    photo = property(lambda self: self.kind == "photo" or None)

    def to_dict(self) -> dict:
        return {
            "_": "Message",
            "id": self.id,
            "message": self.message,
            "date": self.date,
            "grouped_id": self.grouped_id,
        }


class FakeTelegram:
    """A channel of scripted messages, with a download budget we can assert on."""

    def __init__(self, messages, title="Fake Channel"):
        self.messages = messages
        self.title = title
        self.downloads: list[int] = []
        self.raise_on: set[int] = set()

    def get_entity(self, username):
        return self

    def iter_messages(self, entity, reverse=True, min_id=0, limit=None):
        rows = [m for m in self.messages if m.id > min_id]
        rows.sort(key=lambda m: m.id)
        return rows[:limit] if limit else rows

    def get_messages(self, entity, ids=None, limit=None):
        if limit == 0:

            class Total(list):
                total = len(self.messages)

            return Total()
        by_id = {m.id: m for m in self.messages}
        if isinstance(ids, list):
            return [by_id.get(i) for i in ids]
        return by_id.get(ids)

    def download_media(self, msg, file):
        if msg.id in self.raise_on:
            raise OSError(f"scripted download failure for {msg.id}")
        self.downloads.append(msg.id)
        path = Path(f"{file}.bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(msg.payload)
        return str(path)


def patch(monkey: dict):
    """Install fakes on the ingest module; returns an undo callable."""
    saved = {name: getattr(ingest, name) for name in monkey}
    for name, value in monkey.items():
        setattr(ingest, name, value)
    return lambda: [setattr(ingest, n, v) for n, v in saved.items()]


class FakeCounts(pipeline.Counts):
    def __init__(self):
        super().__init__(run_id="test")


def script(n_media=3, n_text=2):
    """A channel: text messages, audio messages, and one repost of audio #1."""
    messages = []
    for i in range(1, n_text + 1):
        messages.append(FakeMessage(id=i, message=f"text {i}"))
    for i in range(n_media):
        messages.append(
            FakeMessage(
                id=n_text + 1 + i,
                message=f"lesson {i}",
                payload=f"AUDIO-{i}".encode() * 100,
                kind="audio",
                file=FakeFile(mime_type="audio/mpeg", name=f"lesson{i}.mp3"),
            )
        )
    # A repost: same bytes as the first audio, different message id.
    messages.append(
        FakeMessage(
            id=n_text + n_media + 1,
            message="repost",
            payload=b"AUDIO-0" * 100,
            kind="audio",
            file=FakeFile(mime_type="audio/mpeg", name="lesson0.mp3"),
        )
    )
    return messages


def run_sync(convex_fake, s3, tg, username="fake", batch_size=2, limit=None, counts=None):
    counts = counts or FakeCounts()
    undo = patch({"convex": convex_fake, "ffprobe": lambda p: {"durationMs": 1000}})
    try:
        pipeline.clear_dir(ingest.TMP_DIR)
        return ingest.sync_channel(
            tg,
            tg,
            s3,
            "bucket",
            username,
            counts,
            limit=limit,
            batch_size=batch_size,
            log=lambda *a: None,
        ), counts
    finally:
        undo()


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_media_type_ignores_non_binary_media():
    class Msg:
        media = None
        voice = audio = video = video_note = gif = photo = None

    assert ingest.media_type(Msg()) == "none"

    class WebPage:
        pass

    msg = Msg()
    msg.media = WebPage()
    msg.photo = object()  # Telethon exposes a webpage's photo here too
    assert ingest.media_type(msg) == "none", "a link preview is not a binary"


def test_blob_key_is_the_sha256_fanout():
    digest = "ab" + "c" * 62
    assert ingest.blob_key(digest, "mp3") == f"blobs/ab/{digest}.mp3"


def test_full_sync_writes_every_row_batch_and_manifest():
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())
    result, counts = run_sync(convex_fake, s3, tg)

    assert result["fetched"] == 6
    assert len(convex_fake.messages) == 6
    assert counts.success == 6 and counts.failure == 0
    # 4 audio messages, but only 3 unique binaries — the repost dedupes.
    assert len(convex_fake.media) == 3, convex_fake.media
    assert len(convex_fake.links) == 4
    assert len([k for k in s3.objects if k.startswith("blobs/")]) == 3

    manifest = json.loads(s3.objects["meta/fake/manifest.json"])
    assert manifest["count"] == 6
    assert manifest["range"] == {"from": 1, "to": 6}
    assert [b["key"] for b in manifest["batches"]] == [
        "meta/fake/00000001-00000002.jsonl.zst",
        "meta/fake/00000003-00000004.jsonl.zst",
        "meta/fake/00000005-00000006.jsonl.zst",
    ]
    # The batch really is zstd-compressed JSONL of the raw messages.
    raw = zstandard.ZstdDecompressor().decompress(
        s3.objects["meta/fake/00000001-00000002.jsonl.zst"]
    )
    rows = [json.loads(line) for line in raw.decode().splitlines()]
    assert [r["id"] for r in rows] == [1, 2]
    for batch in manifest["batches"]:
        assert batch["sha256"], batch
    assert convex_fake.channels["fake"]["lastMessageId"] == 6


def test_rerun_is_a_no_op_and_re_downloads_nothing():
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())
    run_sync(convex_fake, s3, tg)
    before = (len(convex_fake.messages), len(convex_fake.media), len(convex_fake.links))
    downloads = len(tg.downloads)

    _, counts = run_sync(convex_fake, s3, tg)
    after = (len(convex_fake.messages), len(convex_fake.media), len(convex_fake.links))
    assert after == before, "a rerun duplicated rows"
    assert len(tg.downloads) == downloads, "a rerun re-downloaded media"
    assert counts.processed == 0, "the checkpoint should have skipped everything"


def test_kill_mid_channel_resumes_without_gaps_or_duplicates():
    """Kill after the first batch is durable; resume must land on the same archive."""
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())

    boom = RuntimeError("killed")
    real_flush = ingest._flush
    calls = {"n": 0}

    def flush_then_die(*args, **kwargs):
        calls["n"] += 1
        out = real_flush(*args, **kwargs)
        if calls["n"] == 2:
            raise boom
        return out

    undo = patch({"_flush": flush_then_die})
    try:
        try:
            run_sync(convex_fake, s3, tg)
        except RuntimeError as exc:
            assert exc is boom
        else:
            raise AssertionError("the scripted kill did not fire")
    finally:
        undo()

    assert convex_fake.channels["fake"]["lastMessageId"] == 4
    partial = len(convex_fake.messages)
    assert partial == 4

    run_sync(convex_fake, s3, tg)  # resume
    assert len(convex_fake.messages) == 6
    assert sorted(m[1] for m in convex_fake.messages) == [1, 2, 3, 4, 5, 6]
    assert len(convex_fake.media) == 3 and len(convex_fake.links) == 4
    manifest = json.loads(s3.objects["meta/fake/manifest.json"])
    assert manifest["count"] == 6 and manifest["range"] == {"from": 1, "to": 6}


def test_kill_mid_file_leaves_no_orphan_and_only_redoes_that_file():
    """Ctrl-C between a message row and its blob. The §4.2 query reports that
    message as incomplete, so the resume redoes exactly it — not the batch."""
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())
    real_media = ingest.ingest_media
    seen = {"n": 0}

    def die_on_second_media(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt  # a kill, not a download error
        return real_media(*args, **kwargs)

    undo = patch({"ingest_media": die_on_second_media})
    try:
        try:
            run_sync(convex_fake, s3, tg, batch_size=10)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("the scripted kill did not fire")
    finally:
        undo()

    # ids 1-4 have rows; id 4 died between its row and its blob.
    assert sorted(m[1] for m in convex_fake.messages) == [1, 2, 3, 4]
    # Nothing was checkpointed: _flush never ran, so a resume replays the batch.
    assert convex_fake.channels["fake"]["lastMessageId"] == 0
    assert not any(convex_fake.failures), "a kill is not a failure to retry"
    assert not list(ingest.TMP_DIR.glob("*")), "a temp file outlived the crash"

    downloads_before = len(tg.downloads)
    _, counts = run_sync(convex_fake, s3, tg, batch_size=10)
    assert len(convex_fake.messages) == 6
    assert len(convex_fake.media) == 3 and len(convex_fake.links) == 4
    # 1, 2 and 3 were complete and cost nothing; 4, 5, 6 are the only downloads.
    assert counts.skipped == 3, counts
    assert len(tg.downloads) - downloads_before == 3
    assert tg.downloads[-3:] == [4, 5, 6]


def test_a_download_failure_is_recorded_then_drained_on_the_next_run():
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())
    tg.raise_on = {4}  # the second audio message
    _, counts = run_sync(convex_fake, s3, tg, batch_size=10)
    assert counts.failure == 1 and counts.success == 5
    failure = convex_fake.failures[("ingest", "fake/4")]
    assert failure["attempts"] == 1 and not failure["resolved"]
    assert len(convex_fake.media) == 2

    tg.raise_on = set()
    _, counts = run_sync(convex_fake, s3, tg, batch_size=10)
    assert convex_fake.failures[("ingest", "fake/4")]["resolved"] is True
    assert len(convex_fake.media) == 3 and len(convex_fake.links) == 4


def test_a_declined_download_is_a_failure_not_a_silent_loss():
    """`download_media` returning None once cost a binary forever: message row
    written, checkpoint advanced, nothing in the failure ledger."""
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())
    tg.download_media = lambda msg, file: None
    _, counts = run_sync(convex_fake, s3, tg, batch_size=10)
    assert counts.failure == 4, counts  # every media message, none swallowed
    assert len(convex_fake.failures) == 4
    assert not convex_fake.media and not convex_fake.links


def test_the_same_bytes_under_a_new_extension_reuse_the_first_key():
    """A repost whose filename says .mpga must not mint a second blob."""
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script(n_media=1))
    tg.messages[-1].file = FakeFile(mime_type="audio/mpeg", name="lesson0.mpga")
    real_download = tg.download_media

    def download_with_other_ext(msg, file):
        path = real_download(msg, file)
        if msg.id == tg.messages[-1].id:
            renamed = Path(path).with_suffix(".mpga")
            Path(path).rename(renamed)
            return str(renamed)
        return path

    tg.download_media = download_with_other_ext
    run_sync(convex_fake, s3, tg, batch_size=10)
    blobs = [k for k in s3.objects if k.startswith("blobs/")]
    assert len(blobs) == 1, blobs
    assert len(convex_fake.media) == 1 and len(convex_fake.links) == 2


def test_message_args_carry_forwards_groups_replies_and_edits():
    class Fwd:
        from_id = None
        from_name = "Some Channel"
        channel_post = 77

    msg = FakeMessage(
        id=9,
        message="x",
        grouped_id=13906500114419712,
        reply_to_msg_id=4,
        fwd_from=Fwd(),
        edit_date=datetime(2024, 2, 2, tzinfo=UTC),
    )
    args = ingest.message_args("channels#1", "alkulife", msg)
    assert args["telegramUrl"] == "https://t.me/alkulife/9"
    assert args["replyToMessageId"] == 4
    assert args["isForwarded"] is True
    assert args["forwardedFromChannel"] == "Some Channel"
    assert args["forwardedFromMsgId"] == 77
    # int64 grouped_id must survive as a string, not as a lossy JSON number.
    assert args["groupedId"] == "13906500114419712"
    assert args["editDate"] == 1706832000000
    assert args["date"] == 1704067200000


def test_manifest_rewrites_a_replayed_batch_instead_of_duplicating_it():
    s3 = FakeS3()
    raws = [{"id": 1}, {"id": 2}]
    ingest.write_meta_batch(s3, "bucket", "fake", raws)
    ingest.write_meta_batch(s3, "bucket", "fake", raws)
    manifest = json.loads(s3.objects["meta/fake/manifest.json"])
    assert len(manifest["batches"]) == 1 and manifest["count"] == 2


def test_ffprobe_reads_a_real_file():
    """The one place M1 depends on an external binary. ffmpeg makes the fixture."""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("ffmpeg"):
        print("  (skipped: no ffmpeg)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tone.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "quiet",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                "1.5",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(path),
            ],
            check=True,
        )
        probe = ingest.ffprobe(path)
    assert 1400 <= probe["durationMs"] <= 1600, probe
    assert probe["sampleRate"] == 16000 and probe["channelCount"] == 1
    assert probe["codec"], probe
    assert ingest.ffprobe(Path("/dev/null")) == {}


def test_fake_convex_matches_the_deployed_signatures():
    """The fakes above are only worth something if they mirror the real thing."""
    source = (REPO / "convex" / "mutations.ts").read_text()
    source += (REPO / "convex" / "queries.ts").read_text()
    exported = set(re.findall(r"export const (\w+) = (?:mutation|query)\(", source))
    fake = FakeConvex()
    used = {
        "upsertChannel",
        "checkpointChannel",
        "upsertTelegramMessage",
        "getOrCreateMediaObject",
        "linkMessageMedia",
        "recordFailure",
        "resolveFailure",
        "startPipelineRun",
        "finishPipelineRun",
        "acquirePipelineStage",
        "heartbeatPipelineStage",
        "releasePipelineStage",
        "ingestedMessageIds",
        "unresolvedFailures",
    }
    assert used <= exported, f"fakes reference undeployed functions: {used - exported}"
    for name in used:
        assert hasattr(fake, f"m_{name}") or hasattr(fake, f"q_{name}"), name

    # Every argument the fakes accept must be an argument the deployment declares.
    import inspect

    for name in used:
        method = getattr(fake, f"m_{name}", None) or getattr(fake, f"q_{name}")
        body = re.search(
            rf"export const {name} = (?:mutation|query)\(\{{\s*args: \{{(.*?)\n  \}},",
            source,
            re.S,
        )
        if body is None:
            continue
        declared = set(re.findall(r"(\w+):\s*v\.", body.group(1)))
        params = {
            p
            for p in inspect.signature(method).parameters
            if p not in ("self", "rest", "counts", "kw")
        }
        assert params <= declared, f"{name}: fake takes {params - declared}"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


def test_a_takeout_id_survives_a_session_round_trip():
    """Telethon 1.44.0 cannot read back a session row it wrote with a takeout
    open; `_detach_takeout_id` is what keeps the session file loadable."""
    from telethon.sessions import SQLiteSession

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.session"
        SQLiteSession(str(path)).close()  # a real, empty session file
        with sqlite3.connect(path) as db:
            db.execute("update sessions set takeout_id = 2894902766039227874")

        try:
            SQLiteSession(str(path))
            raise AssertionError("expected stock Telethon to choke on the row")
        except TypeError:
            pass

        with mock.patch.object(ingest.telegram, "session_path", lambda: path):
            assert ingest._detach_takeout_id(path) == 2894902766039227874
            SQLiteSession(str(path)).close()  # loads again
            assert ingest._takeout_file().read_text() == "2894902766039227874"

            client = types.SimpleNamespace(session=ingest._session())
            assert ingest._takeout_id(client) == 2894902766039227874
            client.session.save()
            client.session.close()
            # ...and the id still never reaches the column that cannot hold it.
            with sqlite3.connect(path) as db:
                assert db.execute("select takeout_id from sessions").fetchone()[0] is None
            SQLiteSession(str(path)).close()


def test_an_expired_file_reference_is_renewed_not_failed():
    """A batch of lesson-sized audio takes hours to drain, so the reference a
    message was handed with can go stale before its turn to download."""
    from telethon.errors import FileReferenceExpiredError

    fresh = FakeMessage(id=7, kind="audio")
    calls = []

    class Client:
        def download_media(self, msg, file):
            calls.append(msg)
            if msg is not fresh:
                raise FileReferenceExpiredError(request=None)
            return file + ".mp3"

    got = ingest._download(
        Client(), FakeMessage(id=7, kind="audio"), lambda i: fresh, lambda m: None
    )
    assert got.endswith("7.mp3"), got
    assert len(calls) == 2 and calls[1] is fresh

    # Without a way to re-fetch there is nothing to do but fail, so §4.6 retries.
    try:
        ingest._download(Client(), FakeMessage(id=7, kind="audio"), None, lambda m: None)
        raise AssertionError("expected the expiry to propagate")
    except FileReferenceExpiredError:
        pass


def test_an_interrupt_stops_before_the_checkpoint_moves():
    """Ctrl-C mid-download is swallowed by asyncio, so the flag is checked
    between messages — and it has to fire before the batch is published."""
    convex_fake, s3, tg = FakeConvex(), FakeS3(), FakeTelegram(script())
    undo = patch({"convex": convex_fake, "ffprobe": lambda p: {"durationMs": 1000}})
    ingest._stop = True
    try:
        channel = convex_fake.m_upsertChannel(username="fake", title="fake")
        ingest._flush(
            tg,
            s3,
            "bucket",
            channel["id"],
            "fake",
            tg.messages,
            FakeCounts(),
            lambda *a: None,
        )
        raise AssertionError("expected the interrupt to stop the batch")
    except KeyboardInterrupt:
        pass
    finally:
        ingest._stop = False
        undo()

    assert tg.downloads == [], tg.downloads
    assert not [k for k in s3.objects if k.startswith("meta/")], list(s3.objects)
    assert convex_fake.channels["fake"]["lastMessageId"] == 0


if __name__ == "__main__":
    sys.exit(main())
