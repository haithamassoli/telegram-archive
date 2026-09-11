"""M3 checks. Run: `python tests/test_m3.py` (also collectable by pytest).

The interesting M3 logic is not "does it produce lessons" — it is whether a
second run against unchanged data writes anything (the milestone's exit
criterion), whether an `approved` lesson can be silently recomposed (§4.7), and
whether the grouping rules the corpus actually justifies survive both channels'
opposite shapes: five-minute voice-note parts on one, forty-five-minute whole
lessons on the other. `test_fake_convex_matches_the_deployed_signatures` keeps
the fakes honest.
"""

from __future__ import annotations

import io
import json
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive import organize, pipeline

import backend
from archive.config import CONFIG_HASH, PINNED_CONFIG

REPO = Path(__file__).resolve().parents[1]
CHANNEL = "channel1"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeConvex:
    """The deployed M3 functions, re-implemented over dicts."""

    def __init__(self):
        self.messages: list[dict] = []
        self.lessons: list[dict] = []
        self.parts: dict[str, list[dict]] = {}
        self.sources: dict[str, list[dict]] = {}
        self.articles: dict[str, dict] = {}
        self.failures: dict[tuple, dict] = {}
        self.locks: dict[str, dict] = {}
        self.runs: list[dict] = []
        self.writes = 0  # every mutation that reported a change
        self.calls: list[str] = []  # every mutation attempted, changed or not

    # -- transport ---------------------------------------------------------- #

    def query(self, path, **kw):
        return getattr(self, f"q_{path.split(':')[1]}")(**kw)

    def mutation(self, path, **kw):
        name = path.split(":")[1]
        self.calls.append(name)
        return getattr(self, f"m_{name}")(**kw)

    # -- queries ------------------------------------------------------------ #

    def q_channelByUsername(self, username):
        return {"_id": CHANNEL, "username": username, "title": "t", "lastMessageId": 0}

    def q_messagesPage(self, channelId, cursor, limit):
        rows = [m for m in self.messages if m["telegramMessageId"] > cursor][:limit]
        return {
            "messages": [dict(m) for m in rows],
            "nextCursor": rows[-1]["telegramMessageId"] if rows else None,
        }

    def q_lessonsPage(self, cursor, limit):
        rows = sorted(self.lessons, key=lambda r: r["lessonKey"])
        rows = [r for r in rows if r["lessonKey"] > cursor][:limit]
        out = []
        # The deployed query joins `mediaObjects` to reach each part's sha256;
        # `lessonParts` itself only stores the id. Mirror that join here.
        sha = {
            media["mediaObjectId"]: media["sha256"]
            for message in self.messages
            for media in message["media"]
        }
        for row in rows:
            if row.get("deletedAt") is not None:
                continue
            parts = sorted(self.parts.get(row["id"], []), key=lambda p: p["order"])
            out.append(
                {
                    "partsLocked": row.get("partsLocked", False),
                    "titleLocked": row.get("titleLocked", False),
                    **row,
                    "parts": [
                        {**p, "sha256": sha[p["mediaObjectId"]]} for p in parts
                    ],
                }
            )
        return {"lessons": out, "nextCursor": rows[-1]["lessonKey"] if rows else None}

    def q_unresolvedFailures(self, stage):
        return [v for k, v in self.failures.items() if k[0] == stage and not v["resolved"]]

    # -- mutations ---------------------------------------------------------- #

    def m_classifyMessages(self, updates):
        changed = 0
        for update in updates:
            row = next(m for m in self.messages if m["id"] == update["messageId"])
            if (
                row["semanticType"] == update["semanticType"]
                and row.get("classifierVersion") == update["classifierVersion"]
            ):
                continue
            row["semanticType"] = update["semanticType"]
            row["classifierVersion"] = update["classifierVersion"]
            changed += 1
        self.writes += changed
        return {"changed": changed}

    def m_upsertArticle(self, messageId, **rest):
        row = {"messageId": messageId, **rest}
        old = self.articles.get(messageId)
        if old == row:
            return {"id": messageId, "created": False, "changed": False}
        self.articles[messageId] = row
        self.writes += 1
        return {"id": messageId, "created": old is None, "changed": True}

    def m_upsertLessonByKey(self, lessonKey, **rest):
        existing = next((l for l in self.lessons if l["lessonKey"] == lessonKey), None)
        if existing is None:
            row = {"id": f"lesson{len(self.lessons)}", "lessonKey": lessonKey, **rest}
            row.setdefault("lessonTranscriptR2Key", None)
            self.lessons.append(row)
            self.writes += 1
            return {"id": row["id"], "created": True, "skipped": False}
        if (
            existing["reviewStatus"] == "approved"
            or existing.get("deletedAt") is not None
            or existing.get("partsLocked")
        ):
            return {"id": existing["id"], "created": False, "skipped": True}
        if existing.get("titleLocked"):
            rest = {
                k: v
                for k, v in rest.items()
                if k
                not in (
                    "rawTitle",
                    "normalizedTitle",
                    "seriesName",
                    "normalizedSeriesName",
                    "seriesEpisode",
                    "lessonPartLabel",
                    "titleParseConfidence",
                    "titleParserVersion",
                )
            }
        existing.update(rest)
        return {"id": existing["id"], "created": False, "skipped": False}

    def m_replaceLessonParts(self, lessonId, parts):
        row = next(r for r in self.lessons if r["id"] == lessonId)
        if row.get("deletedAt") is not None or row.get("partsLocked"):
            return {"changed": False, "count": 0}
        before = self.parts.get(lessonId, [])
        if before == parts:
            return {"changed": False, "count": len(before)}
        self.parts[lessonId] = [dict(p) for p in parts]
        self.writes += 1
        return {"changed": True, "count": len(parts)}

    def m_replaceLessonSources(self, lessonId, sources):
        row = next(r for r in self.lessons if r["id"] == lessonId)
        if row.get("deletedAt") is not None:
            return {"changed": False, "count": 0}
        before = self.sources.get(lessonId, [])
        if before == sources:
            return {"changed": False, "count": len(before)}
        self.sources[lessonId] = [dict(s) for s in sources]
        self.writes += 1
        return {"changed": True, "count": len(sources)}

    def m_setLessonTranscript(self, lessonId, lessonTranscriptR2Key):
        row = next(l for l in self.lessons if l["id"] == lessonId)
        if row.get("lessonTranscriptR2Key") == lessonTranscriptR2Key:
            return {"changed": False}
        row["lessonTranscriptR2Key"] = lessonTranscriptR2Key
        self.writes += 1
        return {"changed": True}

    def m_retireSupersededLesson(self, lessonId, reason):
        row = next(r for r in self.lessons if r["id"] == lessonId)
        if row.get("deletedAt") is not None:
            return {"retired": False}
        if row["reviewStatus"] == "approved" or row.get("partsLocked"):
            return {"retired": False}
        self.parts.pop(lessonId, None)
        self.sources.pop(lessonId, None)
        row.update(deletedAt=1, durationMs=0, partCount=0)
        self.writes += 1
        return {"retired": True}

    def m_demoteLesson(self, lessonId, reason):
        row = next(l for l in self.lessons if l["id"] == lessonId)
        if row["reviewStatus"] != "approved":
            return {"demoted": False}
        row["reviewStatus"] = "needs_review"
        self.writes += 1
        return {"demoted": True}

    def m_recordFailure(self, stage, refKey, error):
        row = self.failures.setdefault(
            (stage, refKey),
            {"stage": stage, "refKey": refKey, "attempts": 0, "resolved": False},
        )
        row.update(error=error, attempts=row["attempts"] + 1, resolved=False)
        return {"id": refKey, "attempts": row["attempts"]}

    def m_resolveFailure(self, stage, refKey):
        row = self.failures.get((stage, refKey))
        if row is not None:
            row["resolved"] = True
        return {"resolved": row is not None}

    # -- the pipeline harness ----------------------------------------------- #

    def m_acquirePipelineStage(self, stage, runId, owner):
        self.locks[stage] = {"runId": runId, "owner": owner}
        return {"acquired": True, "holder": runId}

    def m_heartbeatPipelineStage(self, stage, runId):
        return {"held": True}

    def m_releasePipelineStage(self, stage, runId):
        self.locks.pop(stage, None)
        return {"released": True}

    def m_startPipelineRun(self, runId, stage):
        self.runs.append({"runId": runId, "stage": stage})
        return {"id": runId}

    def m_finishPipelineRun(self, runId, status, **counts):
        self.runs[-1].update(status=status, **counts)
        return {"id": runId}


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_put_for: set[str] = set()

    def put_object(self, Bucket, Key, Body, **kw):
        if any(token in Key for token in self.fail_put_for):
            raise OSError(f"scripted upload failure for {Key}")
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


class World:
    """A channel of fake messages, plus the fakes the stage writes through."""

    def __init__(self):
        self.convex = FakeConvex()
        self.s3 = FakeS3()
        self.next_id = 1

    def message(self, text=None, audio=(), **kw):
        """One message. `audio` is a list of (sha, durationMs) or (sha, ms, name)."""
        number = self.next_id
        self.next_id += 1
        media = []
        for item in audio:
            sha, duration = item[0], item[1]
            media.append(
                {
                    "mediaObjectId": f"media-{sha}",
                    "originalFileName": item[2] if len(item) > 2 else None,
                    "sha256": sha,
                    "ext": "mp3",
                    "mimeType": "audio/mpeg",
                    "durationMs": duration,
                    # `queries:messagesPage` flags a deleted binary rather than
                    # hiding it, so grouping still sees the message that carried
                    # it and the lesson keeps its key.
                    "deletedAt": None,
                }
            )
        row = {
            "id": f"msg{number}",
            "telegramMessageId": number,
            "date": kw.pop("date", 1_700_000_000_000 + number * 60_000),
            "editDate": None,
            "deletedAt": None,
            "text": text,
            "replyToMessageId": kw.pop("reply", None),
            "groupedId": kw.pop("groupedId", None),
            "telegramUrl": f"https://t.me/alkulife/{number}",
            "mediaType": "audio" if media else kw.pop("mediaType", "none"),
            "semanticType": None,
            "classifierVersion": None,
            "isForwarded": kw.pop("forwarded", False),
            "forwardedFromChannel": None,
            "media": media,
        }
        row.update(kw)
        self.convex.messages.append(row)
        return row

    def transcript(self, sha, segments=((0.0, 10.0, "نص"),)):
        key = f"transcripts/{sha}/{CONFIG_HASH}.json"
        body = {
            "segments": [
                {"segment_index": i, "start": s, "end": e, "text": t}
                for i, (s, e, t) in enumerate(segments)
            ]
        }
        self.s3.objects[key] = json.dumps(body).encode()

    def run(self, command, **kwargs):
        """Run a stage against the fakes — `pipeline` included, or its stage lock
        would be taken on the real deployment."""
        saved = (organize.convex, organize.r2, pipeline.convex)
        organize.convex = self.convex
        organize.r2 = types.SimpleNamespace(
            client=lambda: self.s3,
            bucket=lambda which="archive": "bucket",
            list_keys=lambda s3, bucket, prefix: {
                k for k in s3.objects if k.startswith(prefix)
            },
        )
        pipeline.convex = self.convex
        try:
            kwargs.setdefault("log", lambda *a: None)
            return command(**kwargs)
        finally:
            organize.convex, organize.r2, pipeline.convex = saved


def lesson_world() -> World:
    """One @alkulife-shaped lesson and one @doros_alkulify-shaped one."""
    world = World()
    world.message("منهج إبراهيم في علاج الشرك 👇")
    world.message(audio=[("a" * 64, 320_000)])
    world.message(audio=[("b" * 64, 310_000)])
    world.message(audio=[("c" * 64, 300_000)])
    world.message("الدارمي (169) نهاية كتاب الزكاة 👇")
    world.message(audio=[("d" * 64, 2_600_000, "الدارمي (169).m4a")])
    for name in "abcd":
        world.transcript(name * 64)
    return world


# --------------------------------------------------------------------------- #
# classifier
# --------------------------------------------------------------------------- #


def test_a_message_that_is_only_a_url_is_a_link():
    world = World()
    link = world.message("https://youtu.be/qrf9jHC4pr4")
    assert organize.classify(link, None) == "link", "3,977 of these exist"


def test_an_announcement_is_a_notice_even_with_audio_behind_it():
    world = World()
    notice = world.message("يستكمل الشيخ الليلة درس الجامع إن شاء الله تعالى")
    audio = world.message(audio=[("a" * 64, 2_000_000)])
    assert organize.classify(notice, audio) == "notice"


def test_a_short_message_followed_by_audio_is_a_title():
    world = World()
    title = world.message("مواساة الغرباء 👇")
    audio = world.message(audio=[("a" * 64, 320_000)])
    assert organize.classify(title, audio) == "lesson_title"
    assert organize.classify(title, None) == "other", "no audio behind it, no title"


def test_a_long_text_is_an_article_and_length_is_what_decides():
    world = World()
    body = "باب فيمن أدرك تناقضه فخجل\n\n" + ("قال الذهبي في تاريخ الإسلام. " * 40)
    assert len(body) >= organize.ARTICLE_MIN_CHARS
    article = world.message(body)
    assert organize.classify(article, None) == "article"
    # The same wording, short, in front of audio, is a lesson title. Nothing but
    # length and what follows separates the two.
    title = world.message("باب فيمن أدرك تناقضه فخجل 👇")
    audio = world.message(audio=[("a" * 64, 320_000)])
    assert organize.classify(title, audio) == "lesson_title"


def test_an_article_with_a_photo_still_counts():
    world = World()
    body = "عنوان\n\n" + ("نص طويل جدا. " * 60)
    photo = world.message(body, mediaType="photo")
    assert organize.classify(photo, None) == "article", "666 articles carry a screenshot"


# --------------------------------------------------------------------------- #
# title parser
# --------------------------------------------------------------------------- #


def test_the_series_number_is_read_in_both_digit_alphabets():
    assert organize.parse_title("الدارمي (169) الحج 👇")["seriesEpisode"] == 169
    parsed = organize.parse_title("زاد المسافر [ ١٤ ]\nمن المسألة رقم [ ٦٥١ ]")
    assert parsed["seriesEpisode"] == 14, "Arabic-Indic digits count too"
    assert parsed["seriesName"] == "زاد المسافر"


def test_the_durable_series_is_the_source_book_not_the_chapter():
    parsed = organize.parse_title("الصلاة (22) من المغني (42) 👇")
    assert parsed["seriesName"] == "المغني", "المغني is the course; الصلاة is a chapter"
    assert parsed["seriesEpisode"] == 42


def test_a_majlis_ordinal_is_an_episode_number():
    parsed = organize.parse_title("قراءة تفسير ابن أبي حاتم\nالمجلس الخامس")
    assert parsed["seriesEpisode"] == 5


def test_an_unnumbered_title_parses_with_low_confidence_and_no_series():
    parsed = organize.parse_title("مواساة الغرباء 👇")
    assert parsed["seriesEpisode"] is None and parsed["seriesName"] is None
    assert parsed["confidence"] < 0.6, "@alkulife never numbers anything"


def test_a_machine_filename_is_not_a_title():
    assert organize.filename_title([{"originalFileName": "record.ogg"}]) is None
    assert organize.filename_title([{"originalFileName": "01.mp3"}]) is None
    assert organize.filename_title([{"originalFileName": "4_58259335.m4a"}]) is None
    assert (
        organize.filename_title([{"originalFileName": "زاد_المسافر_(42).m4a"}])
        == "زاد المسافر (42)"
    ), "underscores are a Telegram artefact, not part of the title"


# --------------------------------------------------------------------------- #
# grouping
# --------------------------------------------------------------------------- #


def _grouped(world: World) -> list[dict]:
    messages = world.convex.messages
    for position, message in enumerate(messages):
        nxt = messages[position + 1] if position + 1 < len(messages) else None
        message["semanticType"] = organize.classify(message, nxt)
    return organize.group(messages)


def test_short_consecutive_parts_become_one_lesson():
    world = World()
    world.message("عنوان الدرس 👇")
    for name in "abc":
        world.message(audio=[(name * 64, 320_000)])
    lessons = _grouped(world)
    assert len(lessons) == 1, "three voice notes, one lesson"
    assert len(lessons[0]["parts"]) == 3
    assert lessons[0]["title"] is not None


def test_two_whole_sittings_in_a_row_stay_two_lessons():
    """The 2017 bulk uploads: one index post, then N separate hour-long lessons."""
    world = World()
    world.message("شرح كتاب [ السنة ] ( عدد الدروس : 44 ) 👇")
    world.message(audio=[("a" * 64, 5_682_000, "01.m4a")])
    world.message(audio=[("b" * 64, 3_268_000, "02.m4a")])
    lessons = _grouped(world)
    assert len(lessons) == 2, "45 minutes each is a sitting, not a part"


def test_a_pause_in_the_recording_does_not_rename_the_rest_of_the_run():
    """The bug v1 had: half a run sitting behind a 20-minute pause, untitled."""
    world = World()
    world.message("عنوان 👇")
    first = world.message(audio=[("a" * 64, 320_000)])
    world.message(
        audio=[("b" * 64, 320_000)],
        date=first["date"] + (organize.PART_GAP_S + 60) * 1000,
    )
    lessons = _grouped(world)
    assert len(lessons) == 1, "a bare voice note behind a pause is still a part"
    assert organize.title_of(lessons[0])[1] == "title_message"


def test_a_named_upload_behind_a_pause_is_its_own_lesson():
    """@doros_alkulify's re-upload batches: N named files under one header."""
    world = World()
    world.message("الدروس بصيغة أخرى 👇")
    first = world.message(audio=[("a" * 64, 320_000, "الجامع 7.m4a")])
    world.message(
        audio=[("b" * 64, 320_000, "الجامع 8.m4a")],
        date=first["date"] + (organize.PART_GAP_S + 60) * 1000,
    )
    assert len(_grouped(world)) == 2, "a file that names itself can be a lesson"


def test_even_a_bare_part_splits_once_the_pause_stops_being_a_pause():
    world = World()
    world.message("عنوان 👇")
    first = world.message(audio=[("a" * 64, 320_000)])
    world.message(
        audio=[("b" * 64, 320_000)],
        date=first["date"] + (organize.RUN_GAP_S + 60) * 1000,
    )
    assert len(_grouped(world)) == 2


def test_a_continuation_reply_joins_the_lesson_it_answers():
    world = World()
    world.message("عنوان 👇")
    first = world.message(audio=[("a" * 64, 2_600_000)])
    world.message(
        audio=[("b" * 64, 400_000, "تتمة مهمة.m4a")],
        reply=first["telegramMessageId"],
        date=first["date"] + 3 * 86_400_000,
    )
    lessons = _grouped(world)
    assert len(lessons) == 1, "تتمة says which lesson it continues; believe it"
    assert len(lessons[0]["parts"]) == 2


def test_forwarded_audio_is_not_a_lesson_part():
    world = World()
    world.message("عنوان 👇")
    world.message(audio=[("a" * 64, 320_000)])
    world.message(audio=[("b" * 64, 320_000)], forwarded=True)
    lessons = _grouped(world)
    assert sum(len(l["parts"]) for l in lessons) == 1, "reposts are not new lessons"


def test_audio_sent_as_a_document_is_still_a_lesson_part():
    """387 audio files in the archive arrive typed `document`."""
    world = World()
    world.message("عنوان 👇")
    message = world.message(audio=[("a" * 64, 320_000)])
    message["mediaType"] = "document"
    assert len(_grouped(world)) == 1


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #


def test_assembly_hash_follows_the_order_and_nothing_else():
    one = organize.assembly_hash(["a" * 64, "b" * 64])
    assert one == organize.assembly_hash(["a" * 64, "b" * 64]), "deterministic"
    assert one != organize.assembly_hash(["b" * 64, "a" * 64]), "order is composition"


def test_lesson_key_survives_a_recomposition():
    key = organize.lesson_key("alkulife", 10, 11)
    assert key == organize.lesson_key("alkulife", 10, 11)
    assert key != organize.lesson_key("doros_alkulify", 10, 11)


# --------------------------------------------------------------------------- #
# articles
# --------------------------------------------------------------------------- #


def test_a_truncated_article_is_stitched_back_to_its_continuation():
    world = World()
    head = "عنوان المقال\n\n" + ("كلام. " * 700)
    assert len(head) >= organize.ARTICLE_CONTINUES_AT
    world.message(head)
    world.message("تتمة الكلام. " * 30)
    messages = world.convex.messages
    for message in messages:
        message["semanticType"] = organize.classify(message, None)
    articles = organize.articles_of(messages)
    assert len(articles) == 1, "Telegram's 4,096-char cap is not an article boundary"
    assert "تتمة الكلام" in articles[0]["text"]
    assert articles[0]["title"] == "عنوان المقال"
    assert articles[0]["titleSource"] == "first_line"


# --------------------------------------------------------------------------- #
# the stage
# --------------------------------------------------------------------------- #


def test_a_second_run_over_unchanged_data_writes_nothing():
    """M3's exit criterion, measured rather than asserted.

    The rerun does not merely write nothing — it does not *ask*. A lesson whose
    composition, title and stamped versions all match is skipped before any
    mutation is sent, which is what keeps a channel that gained one message from
    costing one round trip per lesson already in the archive.
    """
    world = lesson_world()
    first = world.run(organize.organize, channels=("alkulife",))
    assert first["lessons"] == 2 and first["changed"] > 0
    before, calls = world.convex.writes, len(world.convex.calls)
    second = world.run(organize.organize, channels=("alkulife",))
    assert second["changed"] == 0, f"rerun wrote {second['changed']} time(s)"
    assert world.convex.writes == before, "and touched nothing underneath either"
    assert second["unchanged"] == 2 and second["lessons"] == 0
    assert "upsertLessonByKey" not in world.convex.calls[calls:], (
        "a no-op rerun still spent a round trip per lesson"
    )


def test_a_row_grouping_no_longer_composes_is_retired():
    """A regrouping merges two lessons into one; the row left behind lists audio
    its neighbour now plays, so it has to stop existing — except when a human
    owns it, and except when it belongs to a channel this run never scanned.
    """
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))

    def orphan(key, **kw):
        row = {
            "id": f"orphan-{key}",
            "lessonKey": key,
            "assemblyHash": "x",
            "rawTitle": "",
            "normalizedTitle": "",
            "reviewStatus": "needs_review",
            "groupingVersion": organize.GROUPING_VERSION,
            "titleParserVersion": organize.TITLE_PARSER_VERSION,
            "durationMs": 0,
            "partCount": 0,
            "lessonTranscriptR2Key": None,
            **kw,
        }
        world.convex.lessons.append(row)
        world.convex.parts[row["id"]] = []
        return row

    superseded = orphan("tg:alkulife:0:900")
    approved = orphan("tg:alkulife:0:901", reviewStatus="approved")
    elsewhere = orphan("tg:doros_alkulify:0:902")

    result = world.run(organize.organize, channels=("alkulife",))
    assert result["retired"] == 1, "exactly the one row nothing composes any more"
    assert superseded["deletedAt"] is not None
    assert superseded["id"] not in world.convex.parts, "its parts went with it"
    assert approved.get("deletedAt") is None, "§4.7: a human's call, not a rerun's"
    assert elsewhere.get("deletedAt") is None, "unvisited is not superseded"


# --------------------------------------------------------------------------- #
# what the dashboard deletes stays deleted
#
# Every one of these runs the Organizer twice: once to build the archive, then
# again after a hand edit, because the whole question is what the *second* run
# does. The dashboard's mutations live in the site repo; what is asserted here is
# the contract this side of the wire depends on.
# --------------------------------------------------------------------------- #


def _delete_media(world, sha):
    """What `admin.deletePartRows` leaves behind: a flagged binary, no part."""
    for message in world.convex.messages:
        for media in message["media"]:
            if media["sha256"] == sha:
                media["deletedAt"] = 1_700_000_000_000
    for lesson_id, parts in world.convex.parts.items():
        kept = [p for p in parts if p["mediaObjectId"] != f"media-{sha}"]
        if len(kept) != len(parts):
            world.convex.parts[lesson_id] = kept
            row = next(r for r in world.convex.lessons if r["id"] == lesson_id)
            row["partsLocked"] = True


def test_a_deleted_audio_is_not_grouped_back_into_its_lesson():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    lesson = world.convex.lessons[0]
    key, parts = lesson["lessonKey"], world.convex.parts[lesson["id"]]
    assert len(parts) == 3

    _delete_media(world, "b" * 64)
    result = world.run(organize.organize, channels=("alkulife",))

    assert result["frozen"] >= 1
    assert [p["mediaObjectId"] for p in world.convex.parts[lesson["id"]]] == [
        "media-" + "a" * 64,
        "media-" + "c" * 64,
    ], "the deleted binary came back"
    assert lesson["lessonKey"] == key, "and the lesson kept its identity"


def test_deleting_the_first_audio_does_not_split_the_lesson_in_two():
    """The regression that makes `live_media` exist.

    `lesson_key` is derived from the first message carrying audio. Hiding a
    deleted binary from the grouper would move that message along by one and
    compose a second lesson over the parts the admin just kept.
    """
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    before = {r["lessonKey"] for r in world.convex.lessons}

    _delete_media(world, "a" * 64)
    world.run(organize.organize, channels=("alkulife",))

    assert {r["lessonKey"] for r in world.convex.lessons} == before


def test_a_deleted_lesson_is_never_composed_again():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    lesson = world.convex.lessons[0]
    # What `admin.deleteLessonRows` leaves: parts gone, the row kept as the
    # tombstone that `lessonKey` is looked up in.
    lesson["deletedAt"] = 1_700_000_000_000
    world.convex.parts[lesson["id"]] = []
    for sha in ("a", "b", "c"):
        _delete_media(world, sha * 64)

    result = world.run(organize.organize, channels=("alkulife",))

    assert result["deleted"] == 1
    assert world.convex.parts[lesson["id"]] == [], "the lesson was recomposed"
    live = [r for r in world.convex.lessons if r.get("deletedAt") is None]
    assert len(live) == 1


def test_a_renamed_lesson_keeps_its_title_through_a_rerun():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    lesson = world.convex.lessons[0]
    lesson.update(
        {"rawTitle": "عنوان كتبه المشرف", "titleLocked": True, "seriesEpisode": 7}
    )

    world.run(organize.organize, channels=("alkulife",))

    assert lesson["rawTitle"] == "عنوان كتبه المشرف"
    assert lesson["seriesEpisode"] == 7
    assert lesson["groupingVersion"] == organize.GROUPING_VERSION, (
        "a locked title must not freeze the rest of the row"
    )


def test_a_lesson_whose_parts_were_moved_by_hand_is_left_alone():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    lesson = world.convex.lessons[0]
    # `admin.movePart` carried the third part over to the other lesson.
    moved = world.convex.parts[lesson["id"]].pop()
    lesson["partsLocked"] = True
    other = world.convex.lessons[1]
    world.convex.parts[other["id"]].append({**moved, "order": 1, "offsetMs": 0})
    other["partsLocked"] = True
    composition = {k: list(v) for k, v in world.convex.parts.items()}

    world.run(organize.organize, channels=("alkulife",))

    assert world.convex.parts == composition, "a rerun undid a hand edit"


def test_an_approved_lesson_is_never_recomposed():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    lesson = world.convex.lessons[0]
    lesson["reviewStatus"] = "approved"
    frozen = dict(lesson)
    result = world.run(organize.organize, channels=("alkulife",))
    assert result["frozen"] == 1
    assert world.convex.lessons[0] == frozen, "§4.7: approved is frozen"


def test_an_approved_lesson_whose_parts_changed_is_handed_back_to_a_human():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    lesson = world.convex.lessons[0]
    lesson["reviewStatus"] = "approved"
    # A fourth part appears under the same title.
    world.convex.messages.insert(
        4,
        {
            **world.convex.messages[3],
            "id": "msg-extra",
            "telegramMessageId": 4,
            "media": [
                {
                    "mediaObjectId": "media-e",
                    "originalFileName": None,
                    "sha256": "e" * 64,
                    "ext": "mp3",
                    "mimeType": "audio/mpeg",
                    "durationMs": 300_000,
                    "deletedAt": None,
                }
            ],
        },
    )
    for index, message in enumerate(world.convex.messages):
        message["telegramMessageId"] = index + 1
    result = world.run(organize.organize, channels=("alkulife",))
    assert result["demoted"] == 1
    assert world.convex.lessons[0]["reviewStatus"] == "needs_review"
    assert world.convex.lessons[0]["assemblyHash"] == lesson["assemblyHash"], (
        "demoted, not recomposed"
    )


def test_a_lesson_has_exactly_one_source_and_it_is_telegram():
    """No YouTube rows: those uploads are the same recordings, republished late."""
    world = World()
    world.message("عنوان 👇\nhttps://youtu.be/abc123")
    world.message(audio=[("a" * 64, 320_000)])
    world.message("https://youtu.be/unrelated")  # the republication stream
    world.run(organize.organize, channels=("alkulife",))
    sources = world.convex.sources["lesson0"]
    assert [s["sourceType"] for s in sources] == ["telegram"]
    assert sources[0]["isPrimary"] and sources[0]["url"].startswith("https://t.me/")


# --------------------------------------------------------------------------- #
# Phase 3.5
# --------------------------------------------------------------------------- #


def test_a_lesson_transcript_offsets_every_part_after_the_first():
    world = lesson_world()
    world.transcript("a" * 64, segments=((0.0, 10.0, "الأول"),))
    world.transcript("b" * 64, segments=((0.0, 12.0, "الثاني"),))
    world.transcript("c" * 64, segments=((0.0, 8.0, "الثالث"),))
    world.run(organize.organize, channels=("alkulife",))
    result = world.run(organize.build_lesson_transcripts)
    assert result["built"] == 2 and result["failed"] == 0
    lesson = next(l for l in world.convex.lessons if l["partCount"] == 3)
    body = json.loads(world.s3.objects[lesson["lessonTranscriptR2Key"]])
    starts = [segment["startMs"] for segment in body["segments"]]
    assert starts == [0, 320_000, 630_000], "lessonStartMs = offsetMs + segment start"
    assert body["configHash"] == CONFIG_HASH
    assert body["organizerVersion"] == organize.ORGANIZER_VERSION


def test_the_pointer_never_moves_before_the_artifact_lands():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    world.s3.fail_put_for.add("lesson-transcripts/")
    result = world.run(organize.build_lesson_transcripts)
    assert result["failed"] == 2 and result["built"] == 0
    assert all(l["lessonTranscriptR2Key"] is None for l in world.convex.lessons), (
        "§4.1: R2 first, Convex second"
    )
    assert len(world.convex.q_unresolvedFailures(organize.TRANSCRIPT_STAGE)) == 2


def test_a_built_lesson_transcript_is_not_built_twice():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    world.run(organize.build_lesson_transcripts)
    again = world.run(organize.build_lesson_transcripts)
    assert again["built"] == 0 and again["current"] == 2


def test_a_recomposed_lesson_gets_a_new_artifact_by_identity_alone():
    world = lesson_world()
    world.run(organize.organize, channels=("alkulife",))
    world.run(organize.build_lesson_transcripts)
    lesson = next(l for l in world.convex.lessons if l["partCount"] == 3)
    stale = lesson["lessonTranscriptR2Key"]
    lesson["assemblyHash"] = "f" * 64  # a regrouping happened
    result = world.run(organize.build_lesson_transcripts)
    assert result["built"] == 1
    assert lesson["lessonTranscriptR2Key"] != stale
    assert stale in world.s3.objects, "the old artifact is orphaned, never deleted"


# --------------------------------------------------------------------------- #
# the fakes, kept honest
# --------------------------------------------------------------------------- #


def test_fake_convex_matches_the_deployed_signatures():
    import inspect

    source = backend.source()
    exported = set(re.findall(r"export const (\w+) = (?:mutation|query)\(", source))
    fake = FakeConvex()
    used = {
        "classifyMessages",
        "upsertArticle",
        "upsertLessonByKey",
        "replaceLessonParts",
        "replaceLessonSources",
        "setLessonTranscript",
        "demoteLesson",
        "retireSupersededLesson",
        "messagesPage",
        "lessonsPage",
        "channelByUsername",
        "unresolvedFailures",
        "recordFailure",
        "resolveFailure",
    }
    assert used <= exported, f"fakes reference undeployed functions: {used - exported}"
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
            if p not in ("self", "rest", "kw")
        }
        assert params <= declared, f"{name}: fake takes {params - declared}"

    schema = backend.schema()
    for table, needed in {
        "lessons": {"seriesName", "seriesEpisode", "groupingConfidence", "reviewStatus"},
        "lessonParts": {"offsetMs", "order", "mediaObjectId"},
        "lessonSources": {"sourceType", "isPrimary"},
        "articles": {"normalizedText", "titleSource"},
    }.items():
        block = re.search(rf"{table}: defineTable\(\{{(.*?)\n  \}}\)", schema, re.S)
        fields = set(re.findall(r"(\w+): v\.", block.group(1)))
        assert needed <= fields, f"{table} is missing {needed - fields}"


def test_the_normalizer_follows_the_documented_contract():
    from archive.normalize import normalize

    assert normalize("الدَّرْسُ الأوَّل") == "الدرس الاول"
    assert normalize("الحلقة ٢٣") == "الحلقه 23", "Arabic-Indic digits become Latin"
    assert normalize("كتـــاب") == "كتاب", "tatweel goes"
    assert PINNED_CONFIG["language"] == "ar"


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


if __name__ == "__main__":
    sys.exit(main())
