"""M3 — the Organizer (plan Phase 3) and the Lesson Transcript Builder (3.5).

Pure with respect to Telegram and the GPU: it reads Convex, decides, and writes
Convex. Nothing here re-downloads a message or re-runs inference, which is what
makes a full rerun legal at any time — subject to §4.7, the one rule that
outranks rerunning: an `approved` lesson is never silently recomposed.

Three versioned decisions live here, and each is stamped on the row it produced
so a later run can tell its own work from an older run's: `CLASSIFIER_VERSION`,
`TITLE_PARSER_VERSION`, `GROUPING_VERSION`.

Two rules the plan proposed do not survive contact with the archive, and the
measurements that killed them are recorded next to the code that replaces them:
`groupedId` (§ grouping) and YouTube-link confirmation (§ sources).
"""

import hashlib
import json
import re

from . import convex, r2, transcribe
from .config import CONFIG_HASH, M1_CHANNELS, canonical_json
from .normalize import NORM_VERSION, normalize

STAGE = "organize"
SCAN_PAGE = 400

CLASSIFIER_VERSION = f"classify-v1+{NORM_VERSION}"
TITLE_PARSER_VERSION = f"title-v1+{NORM_VERSION}"
GROUPING_VERSION = "group-v1"
ORGANIZER_VERSION = "organize-v1"

# Measured, not guessed. Text-only length p50 is 1,217 chars on @alkulife (a
# writing channel) against 43 on @doros_alkulify (a scheduling channel); only
# 0.3% of title candidates reach 500 chars. So 500 separates an article from a
# title cleanly, and wording never has to.
ARTICLE_MIN_CHARS = 500
# Telegram truncates at 4,096. 145 of the 200 @alkulife messages at or above
# 4,000 chars are continued by the next text message; treating those as separate
# articles would cut 145 articles in half.
ARTICLE_CONTINUES_AT = 3_900
ARTICLE_CONTINUATION_MIN = 200
# A title message is short. 97.3% of title candidates are under 100 chars; 300
# leaves room for @doros_alkulify's multi-line "series [ N ] / sub-series [ n ]"
# headers without admitting prose.
TITLE_MAX_CHARS = 300

# Grouping thresholds, from the gap histograms. @alkulife posts a lesson's parts
# as it records them: 82.3% of within-run gaps land in 300-600 s, against a
# between-run median of 30 hours. 600 s is where those two distributions part.
PART_GAP_S = 600
# A recording this long is a whole sitting, not a part of one. @alkulife's parts
# have a median duration of 319 s; @doros_alkulify's lessons, 2,626 s. Nothing
# in the archive sits between 20 minutes and a part.
WHOLE_LESSON_MS = 20 * 60 * 1000

URL = re.compile(r"https?://\S+")
YOUTUBE = re.compile(r"https?://(?:www\.)?(?:youtube\.com/\S+|youtu\.be/\S+)")
# Announcements, live-stream state, channel promotion, postponements. Every one
# of these was counted in the corpus before it was written down.
NOTICE = re.compile(
    r"يستكمل الشيخ"          # 145 occurrences, all @doros_alkulify
    r"|^بدأ (البث|الدرس|المجلس|التسجيل)"   # 65, backed by 581 group-call events
    r"|بث مباشر"
    r"|عدد الدروس"           # a series-index post: it introduces N lessons, is not one
    r"|فهرس"
    r"|اشترك|رابط القناة|قناة مخصصة"
    r"|تأجيل|تأجل|اعتذر|لا درس"
)

# `SERIES (N)` and `SERIES [ N ]` — the dominant modern form (1,657 + 361 hits).
SERIES_NUM = re.compile(r"^\s*(.{2,60}?)\s*[(\[]\s*(\d{1,4})\s*[)\]]")
# `SUBTOPIC (n) من SOURCE (N)` — two numbers, and the durable series is the
# source: «الصلاة (22) من المغني (42)» is episode 42 of المغني, not 22 of الصلاة.
FROM_SOURCE = re.compile(r"من\s+(.{2,40}?)\s*[(\[]\s*(\d{1,4})\s*[)\]]")
MAJLIS = re.compile(r"المجلس\s+(\S+)")
# `SERIES N` with nothing around the number — how filenames count: «الجامع 8»,
# «عمدة الفقه 121», «الدارمي-7». Last, because a bracketed number is the surer
# reading whenever both are present.
SERIES_TAIL = re.compile(r"^(.*?[ء-ي].*?)[\s\-_]+(\d{1,4})$")
ORDINALS = {
    "الاول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5,
    "السادس": 6, "السابع": 7, "الثامن": 8, "التاسع": 9, "العاشر": 10,
    "الحادي": 11, "الثاني عشر": 12, "الثالث عشر": 13, "الرابع عشر": 14,
}
_DIGITS = str.maketrans(
    {
        **{chr(0x0660 + n): str(n) for n in range(10)},
        **{chr(0x06F0 + n): str(n) for n in range(10)},
        "ـ": "",
    }
)
# Telegram sanitises filenames and wraps them in RTL isolates.
_FILENAME_JUNK = str.maketrans({"‎": "", "⁨": "", "⁩": "", "_": " "})


def _log(message: str) -> None:
    print(message, flush=True)


def _digits(text: str) -> str:
    """Arabic-Indic digits to Latin, runs of spaces collapsed. Letters untouched.

    The title parser matches numbers, so it needs one digit alphabet — but it
    also lifts `seriesName` out of the string it matched, so the letters must
    survive. Line breaks survive too: a @doros_alkulify header numbers its series
    on the first line and its sub-series on the second, and flattening them makes
    the parser read the wrong number.
    """
    lines = text.translate(_DIGITS).split("\n")
    return "\n".join(" ".join(line.split()) for line in lines).strip()


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def messages_scan(channel_id: str, page: int = SCAN_PAGE):
    """Every message of a channel with its binaries, in `telegramMessageId` order."""
    cursor = 0
    while True:
        result = convex.query(
            "queries:messagesPage",
            channelId=channel_id,
            cursor=cursor,
            limit=page,
        )
        yield from result["messages"]
        cursor = result["nextCursor"]
        if cursor is None:
            return


def audio_media(message: dict) -> list[dict]:
    """The transcribable binaries a message carries, in link order.

    Deliberately not `mediaType in ("audio", "voice")`: 387 audio files in the
    archive arrive without a `DocumentAttributeAudio` attribute and are typed
    `document`. The mime/extension test is the one M2 counted its own corpus
    with, so lessons and transcripts agree on what audio is.
    """
    return [media for media in message["media"] if transcribe.is_audio(media)]


# --------------------------------------------------------------------------- #
# Classifier v1
# --------------------------------------------------------------------------- #


def classify(message: dict, next_message: dict | None) -> str | None:
    """`semanticType` for one message. Order of the tests is the whole design."""
    if message["deletedAt"] is not None:
        return None
    text = (message["text"] or "").strip()
    if not text:
        return "other"
    # 3,977 @doros_alkulify messages are nothing but a URL: a republication
    # stream, not content.
    stripped = URL.sub("", text).strip()
    if not stripped:
        return "link"
    if NOTICE.search(text):
        return "notice"
    # A title is short and has audio right behind it. 98.9% of @alkulife's audio
    # runs are introduced this way, and it is the only title source that channel
    # has — its voice notes carry no caption, no filename and no tags.
    if (
        len(text) <= TITLE_MAX_CHARS
        and next_message is not None
        and audio_media(next_message)
    ):
        return "lesson_title"
    if len(text) >= ARTICLE_MIN_CHARS and message["mediaType"] in ("none", "photo"):
        return "article"
    return "other"


# --------------------------------------------------------------------------- #
# Title parser v1
# --------------------------------------------------------------------------- #


def parse_title(raw: str) -> dict:
    """Series name, episode number and part label out of a lesson title.

    `الحلقة N`, `الجزء الثاني` and `ج2` are not implemented: they appear 0 times
    in title position across 26,723 messages. Digits are how this archive counts,
    in both alphabets, sometimes weeks apart inside one series.
    """
    text = _digits(raw)
    first_line = text.split("\n")[0].strip()

    source = FROM_SOURCE.search(first_line)
    if source is not None:
        name = source.group(1).strip(" .:،-")
        return {
            "seriesName": name or None,
            "seriesEpisode": int(source.group(2)),
            "lessonPartLabel": None,
            "confidence": 0.9,
        }

    numbered = SERIES_NUM.match(first_line)
    if numbered is not None:
        name = numbered.group(1).strip(" .:،-[]()")
        return {
            "seriesName": name or None,
            "seriesEpisode": int(numbered.group(2)),
            "lessonPartLabel": None,
            "confidence": 0.85,
        }

    tail = SERIES_TAIL.match(first_line)
    if tail is not None:
        name = tail.group(1).strip(" .:،-")
        return {
            "seriesName": name or None,
            "seriesEpisode": int(tail.group(2)),
            "lessonPartLabel": None,
            "confidence": 0.7,
        }

    majlis = MAJLIS.search(text)
    if majlis is not None:
        episode = ORDINALS.get(normalize(majlis.group(1)))
        if episode is not None:
            return {
                "seriesName": None,
                "seriesEpisode": episode,
                "lessonPartLabel": majlis.group(0),
                "confidence": 0.6,
            }

    return {
        "seriesName": None,
        "seriesEpisode": None,
        "lessonPartLabel": None,
        "confidence": 0.4,
    }


def filename_title(media: list[dict]) -> str | None:
    """A title recovered from the uploaded filename, or None if it carries none.

    Machine names (`record.ogg`, `4_5825933552872592339.m4a`, `01.mp3`) say
    nothing; 588 of them exist and pretending otherwise would title a lesson with
    a serial number.
    """
    for item in media:
        name = (item.get("originalFileName") or "").translate(_FILENAME_JUNK)
        name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name).strip()
        if not name or re.fullmatch(r"[\d\s\-.]+", name):
            continue
        if re.fullmatch(r"(record|audio|voice)[\s\d]*", name, re.IGNORECASE):
            continue
        return name
    return None


# --------------------------------------------------------------------------- #
# Grouping v1
# --------------------------------------------------------------------------- #


def group(messages: list[dict]) -> list[dict]:
    """Lesson candidates: a title message and the audio that belongs to it.

    `groupedId` is not a primary rule here and the plan expected it to be. It is
    set on 9 of 14,801 @alkulife messages and 200 of 11,922 @doros_alkulify ones,
    and most of those groups are photo albums — under 1% coverage. It is kept
    only as a hard hint where it exists.

    What actually separates lessons is duration. A recording of 20 minutes or
    more is a whole sitting and stands alone; short recordings posted within ten
    minutes of each other are parts of one. That single test covers @alkulife's
    5-minute voice-note parts, @doros_alkulify's 45-minute lessons, and its
    2017 bulk series uploads, which are N separate lessons behind one index post.
    """
    lessons: list[dict] = []
    by_id = {message["telegramMessageId"]: message for message in messages}
    of_message: dict[int, int] = {}  # telegramMessageId -> index in `lessons`

    for position, message in enumerate(messages):
        media = audio_media(message)
        if not media or message["deletedAt"] is not None:
            continue
        # §Phase 3: forwarded audio is not the sheikh's own recording of this
        # lesson. 642 of @alkulife's forwards are its own reposts, which would
        # otherwise become a second lesson over the same binaries.
        if message["isForwarded"]:
            continue

        previous = messages[position - 1] if position else None
        joined = None

        # A `تتمة` continuation says which lesson it continues. 142 of these
        # exist and they are the one reply edge in the archive worth trusting.
        parent = message["replyToMessageId"]
        if parent is not None and parent in of_message:
            joined = of_message[parent]
        elif message["groupedId"] is not None:
            for index, lesson in enumerate(lessons):
                if lesson["groupedId"] == message["groupedId"]:
                    joined = index
                    break

        if joined is None and lessons:
            last = lessons[-1]
            gap = (message["date"] - last["lastDate"]) / 1000
            contiguous = (
                previous is not None
                and previous["telegramMessageId"] == last["lastMessageId"]
            )
            short = all(
                (item["durationMs"] or 0) < WHOLE_LESSON_MS for item in media
            ) and last["shortParts"]
            if contiguous and short and gap <= PART_GAP_S:
                joined = len(lessons) - 1

        if joined is not None:
            lesson = lessons[joined]
            lesson["parts"].append(message)
            lesson["lastDate"] = message["date"]
            lesson["lastMessageId"] = message["telegramMessageId"]
            lesson["shortParts"] = lesson["shortParts"] and all(
                (item["durationMs"] or 0) < WHOLE_LESSON_MS for item in media
            )
            of_message[message["telegramMessageId"]] = joined
            continue

        title = None
        if (
            previous is not None
            and previous["semanticType"] == "lesson_title"
            and previous["telegramMessageId"] == message["telegramMessageId"] - 1
        ):
            title = previous
        lessons.append(
            {
                "title": title,
                "parts": [message],
                "groupedId": message["groupedId"],
                "lastDate": message["date"],
                "lastMessageId": message["telegramMessageId"],
                "shortParts": all(
                    (item["durationMs"] or 0) < WHOLE_LESSON_MS for item in media
                ),
            }
        )
        of_message[message["telegramMessageId"]] = len(lessons) - 1

    for lesson in lessons:
        lesson["byId"] = by_id
    return lessons


def title_of(lesson: dict) -> tuple[str, str, float]:
    """(rawTitle, titleSource, groupingConfidence) for one candidate.

    Confidence is about the *lesson*, not the words: a title message sitting
    immediately before the audio is the archive's strongest signal, a filename is
    a weaker one, and no title at all is a lesson a human should look at.
    """
    title = lesson["title"]
    if title is not None:
        text = (title["text"] or "").strip()
        # 👇 introduces 94% of the audio that follows it. Its presence is the
        # difference between a title and a short remark that happens to sit
        # before a recording.
        return text, "title_message", 0.9 if "👇" in text else 0.7
    for part in lesson["parts"]:
        name = filename_title(audio_media(part))
        if name:
            return name, "filename", 0.5
    for part in lesson["parts"]:
        caption = (part["text"] or "").strip()
        if caption:
            return caption.split("\n")[0][:TITLE_MAX_CHARS], "caption", 0.5
    # No title anywhere. An empty string rather than a stand-in: a URL or a date
    # put here would reach the search index as this lesson's title and read like
    # one. The review queue is where it gets a name.
    return "", "untitled", 0.2


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def assembly_hash(sha256s: list[str]) -> str:
    """§0 amendment 3: the ordered part sha256s, and nothing else.

    Offsets are derivable and live in the artifact body; hashing them would churn
    lesson identity on any ffprobe re-run.
    """
    return hashlib.sha256(canonical_json(sha256s).encode("utf-8")).hexdigest()


def lesson_key(username: str, title_id: int | None, first_id: int) -> str:
    """Deterministic lesson identity (plan §2): channel + title msg + first part.

    Not the assembly hash: repairing or re-ordering a lesson's parts must keep
    the same lesson — and therefore the same R2 prefix and the same search ids —
    while changing its composition.
    """
    return f"tg:{username}:{'0' if title_id is None else title_id}:{first_id}"


# --------------------------------------------------------------------------- #
# Articles
# --------------------------------------------------------------------------- #


def article_title(text: str) -> tuple[str, str]:
    """(title, titleSource). Two thirds of the articles write their own headline."""
    lines = text.split("\n")
    head = lines[0].strip()
    if head and len(head) <= 80:
        return head, "first_line"
    return " ".join(text.split())[:80], "prefix"


def articles_of(messages: list[dict]) -> list[dict]:
    """Article rows, with Telegram's 4,096-char cap stitched back together."""
    out: list[dict] = []
    consumed: set[int] = set()
    for position, message in enumerate(messages):
        if message["semanticType"] != "article":
            continue
        if message["telegramMessageId"] in consumed:
            continue
        if message["isForwarded"]:
            continue  # quoted third-party material, not the sheikh's writing
        text = (message["text"] or "").strip()
        cursor = position
        while len(text) >= ARTICLE_CONTINUES_AT and cursor + 1 < len(messages):
            following = messages[cursor + 1]
            more = (following["text"] or "").strip()
            if (
                following["mediaType"] != "none"
                or following["isForwarded"]
                or following["semanticType"] in ("lesson_title", "notice", "link")
                or len(more) < ARTICLE_CONTINUATION_MIN
            ):
                break
            text = f"{text}\n{more}"
            consumed.add(following["telegramMessageId"])
            cursor += 1
        title, source = article_title(text)
        out.append(
            {
                "messageId": message["id"],
                "title": title,
                "normalizedTitle": normalize(title),
                "titleSource": source,
                "text": text,
                "normalizedText": normalize(text),
                "date": message["date"],
                "telegramUrl": message["telegramUrl"],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# The stage
# --------------------------------------------------------------------------- #


def organize(
    channels: tuple[str, ...] = M1_CHANNELS,
    limit: int | None = None,
    log=_log,
) -> dict:
    """M3's batch command: classify, extract, group, compose.

    Every write reports whether it changed anything, and the run returns the
    total. That total being zero on a second run against unchanged data *is* the
    milestone's exit criterion, so it is measured rather than asserted.
    """
    from . import pipeline

    with pipeline.stage(STAGE) as counts:
        existing = {row["lessonKey"]: row for row in lessons_scan()}
        totals = {
            "messages": 0,
            "classified": 0,
            "articles": 0,
            "lessons": 0,
            "parts": 0,
            "changed": 0,
            "demoted": 0,
            "frozen": 0,
            "needsReview": 0,
        }

        for username in channels:
            channel = convex.query("queries:channelByUsername", username=username)
            if channel is None:
                raise RuntimeError(f"no channel row for {username}")
            messages = list(messages_scan(channel["_id"]))
            if limit is not None:
                messages = messages[:limit]
            log(f"{username}: {len(messages)} message(s)")
            totals["messages"] += len(messages)

            # 1. Classify. Batched: one round trip per page, not per message.
            updates = []
            for position, message in enumerate(messages):
                nxt = messages[position + 1] if position + 1 < len(messages) else None
                kind = classify(message, nxt)
                message["semanticType"] = kind
                updates.append(
                    {
                        "messageId": message["id"],
                        "semanticType": kind,
                        "classifierVersion": CLASSIFIER_VERSION,
                    }
                )
            changed = 0
            for start in range(0, len(updates), SCAN_PAGE):
                changed += convex.mutation(
                    "mutations:classifyMessages",
                    updates=updates[start : start + SCAN_PAGE],
                )["changed"]
            totals["classified"] += changed
            totals["changed"] += changed
            log(f"  classified {len(updates)}, {changed} changed")

            # 2. Articles.
            for article in articles_of(messages):
                counts.processed += 1
                result = convex.mutation(
                    "mutations:upsertArticle",
                    channelId=channel["_id"],
                    **article,
                )
                totals["articles"] += 1
                totals["changed"] += int(result["changed"])
            log(f"  {totals['articles']} article(s) so far")

            # 3+4. Group and compose.
            for candidate in group(messages):
                counts.processed += 1
                title_text, title_source, confidence = title_of(candidate)
                parsed = parse_title(title_text)
                title_message = candidate["title"]
                first = candidate["parts"][0]
                key = lesson_key(
                    username,
                    title_message["telegramMessageId"] if title_message else None,
                    first["telegramMessageId"],
                )

                parts, offset = [], 0
                for part in candidate["parts"]:
                    for media in audio_media(part):
                        duration = media["durationMs"] or 0
                        parts.append(
                            {
                                "messageId": part["id"],
                                "mediaObjectId": media["mediaObjectId"],
                                "order": len(parts),
                                "durationMs": duration,
                                "offsetMs": offset,
                                "sha256": media["sha256"],
                            }
                        )
                        offset += duration
                digest = assembly_hash([part["sha256"] for part in parts])

                # §4.7. An approved lesson is frozen — but a composition that no
                # longer matches, or a source message deleted underneath it, is
                # handed back to a human rather than kept or overwritten.
                previous = existing.get(key)
                if previous is not None and previous["reviewStatus"] == "approved":
                    deleted = any(
                        part["deletedAt"] is not None for part in candidate["parts"]
                    )
                    if previous["assemblyHash"] != digest or deleted:
                        convex.mutation(
                            "mutations:demoteLesson",
                            lessonId=previous["id"],
                            reason="composition changed" if not deleted else "source deleted",
                        )
                        totals["demoted"] += 1
                        totals["changed"] += 1
                        log(f"  demoted {key}")
                    else:
                        totals["frozen"] += 1
                    counts.skipped += 1
                    continue

                # A weaker title source is forgiven when the title numbers
                # itself: «الدارمي (159).m4a» is a filename, but an episode
                # number is not something a machine name produces by accident.
                review = (
                    "auto"
                    if confidence >= 0.6
                    or (confidence >= 0.5 and parsed["seriesEpisode"] is not None)
                    else "needs_review"
                )
                if review == "needs_review":
                    totals["needsReview"] += 1
                lesson = convex.mutation(
                    "mutations:upsertLessonByKey",
                    lessonKey=key,
                    assemblyHash=digest,
                    channelId=channel["_id"],
                    rawTitle=title_text,
                    normalizedTitle=normalize(title_text),
                    seriesName=parsed["seriesName"],
                    normalizedSeriesName=(
                        normalize(parsed["seriesName"]) if parsed["seriesName"] else None
                    ),
                    seriesEpisode=parsed["seriesEpisode"],
                    lessonPartLabel=parsed["lessonPartLabel"],
                    titleParserVersion=TITLE_PARSER_VERSION,
                    titleParseConfidence=parsed["confidence"],
                    groupingVersion=GROUPING_VERSION,
                    groupingConfidence=confidence,
                    reviewStatus=review,
                    titleMessageId=title_message["id"] if title_message else None,
                    firstTelegramMessageId=first["id"],
                    lastTelegramMessageId=candidate["parts"][-1]["id"],
                    partCount=len(parts),
                    durationMs=offset,
                )
                written = convex.mutation(
                    "mutations:replaceLessonParts",
                    lessonId=lesson["id"],
                    parts=[
                        {k: v for k, v in part.items() if k != "sha256"}
                        for part in parts
                    ],
                )
                sources = convex.mutation(
                    "mutations:replaceLessonSources",
                    lessonId=lesson["id"],
                    sources=sources_of(candidate, channel["_id"], title_source),
                )
                totals["lessons"] += 1
                totals["parts"] += len(parts)
                totals["changed"] += int(
                    lesson["created"] or written["changed"] or sources["changed"]
                )
                counts.success += 1
                counts.audio_ms += offset

        counts.notes.append(
            f"lessons={totals['lessons']} articles={totals['articles']} "
            f"changed={totals['changed']}"
        )
        log(
            f"  {totals['lessons']} lesson(s), {totals['parts']} part(s), "
            f"{totals['articles']} article(s), {totals['changed']} write(s), "
            f"{totals['needsReview']} needing review, {totals['demoted']} demoted"
        )
        return totals


def sources_of(candidate: dict, channel_id: str, title_source: str) -> list[dict]:
    """`lessonSources` for one lesson: Telegram primary, YouTube only if named.

    The plan expected trailing YouTube links to confirm a grouping. They cannot:
    of 3,977 bare-link messages in @doros_alkulify, 1,417 sit between two other
    links and only 648 are followed by audio, because the links are a separate
    republication stream posted hours to days later in batches. Position cannot
    bind a link to a lesson, so a link is recorded only when it appears inside
    the lesson's own title or caption — where it is an authored fact, not an
    inference. The rest wait for the v1 YT↔TG map (§8).
    """
    first = candidate["parts"][0]
    sources = [
        {
            "sourceType": "telegram",
            "url": (candidate["title"] or first)["telegramUrl"],
            "channelId": channel_id,
            "messageId": (candidate["title"] or first)["id"],
            "isPrimary": True,
        }
    ]
    owned = [candidate["title"]] if candidate["title"] else []
    owned += candidate["parts"]
    seen = set()
    for message in owned:
        for url in YOUTUBE.findall(message["text"] or ""):
            if url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "sourceType": "youtube",
                    "url": url,
                    "isPrimary": False,
                }
            )
    return sources


# --------------------------------------------------------------------------- #
# Phase 3.5 — Lesson Transcript Builder
# --------------------------------------------------------------------------- #

LESSON_PREFIX = "lesson-transcripts/"
TRANSCRIPT_STAGE = "lesson-transcripts"


def lesson_transcript_key(lesson_id: str, digest: str, config_hash: str) -> str:
    """`lesson-transcripts/{lessonId}/{assemblyHash}-{configHash}.json` (plan §3).

    Identity is in the key, so staleness is a lookup and never a flag: an
    artifact is current iff its key names the lesson's *current* assemblyHash and
    the *active* configHash.
    """
    return f"{LESSON_PREFIX}{lesson_id}/{digest}-{config_hash}.json"


def lessons_scan(page: int = SCAN_PAGE):
    """Every lesson with its ordered parts, in `lessonKey` order."""
    cursor = ""
    while True:
        result = convex.query("queries:lessonsPage", cursor=cursor, limit=page)
        yield from result["lessons"]
        cursor = result["nextCursor"]
        if cursor is None:
            return


def compose_transcript(lesson: dict, parts: list[dict], config_hash: str) -> dict:
    """One lesson transcript from its part artifacts and their offsets (plan §5).

    `lessonStartMs = part.offsetMs + segment.startMs`. Part-local times are not
    copied: they are this arithmetic run backwards, and the part artifact sitting
    next to it in R2 is the authority anyway.
    """
    segments = []
    for part, artifact in zip(lesson["parts"], parts):
        for segment in artifact.get("segments") or []:
            segments.append(
                {
                    "partOrder": part["order"],
                    "sha256": part["sha256"],
                    "segmentIndex": segment.get("segment_index"),
                    "startMs": part["offsetMs"] + int(round(float(segment["start"]) * 1000)),
                    "endMs": part["offsetMs"] + int(round(float(segment["end"]) * 1000)),
                    "text": segment.get("text") or "",
                }
            )
    return {
        "schemaVersion": 1,
        "lessonId": lesson["id"],
        "lessonKey": lesson["lessonKey"],
        "assemblyHash": lesson["assemblyHash"],
        "configHash": config_hash,
        "organizerVersion": ORGANIZER_VERSION,
        "title": lesson["rawTitle"],
        "normalizedTitle": lesson["normalizedTitle"],
        "seriesName": lesson["seriesName"],
        "seriesEpisode": lesson["seriesEpisode"],
        "partCount": len(lesson["parts"]),
        "durationMs": lesson["durationMs"],
        "parts": [
            {
                "sha256": part["sha256"],
                "order": part["order"],
                "offsetMs": part["offsetMs"],
                "durationMs": part["durationMs"],
                "rawR2Key": transcribe.transcript_key(part["sha256"], config_hash),
            }
            for part in lesson["parts"]
        ],
        "segments": segments,
    }


def build_lesson_transcripts(
    config_hash: str = CONFIG_HASH,
    limit: int | None = None,
    log=_log,
) -> dict:
    """Phase 3.5: a current lesson-transcript artifact for every lesson.

    Derived and disposable — deleting the whole prefix costs one rerun. The rule
    it must not break is the write-order law (§4.1): artifact into R2 first, the
    Convex pointer second.
    """
    from . import pipeline

    with pipeline.stage(TRANSCRIPT_STAGE) as counts:
        s3, bucket = r2.client(), r2.bucket()
        published = r2.list_keys(s3, bucket, LESSON_PREFIX)
        failures = {
            row["refKey"]: row
            for row in convex.query(
                "queries:unresolvedFailures", stage=TRANSCRIPT_STAGE
            )
        }
        capped = {
            key
            for key, row in failures.items()
            if row["attempts"] >= transcribe.FAILURE_ATTEMPT_CAP
        }
        retrying = set(failures) - capped

        lessons = [row for row in lessons_scan() if row["parts"]]
        lessons.sort(key=lambda row: (row["lessonKey"] not in retrying, row["lessonKey"]))
        if limit is not None:
            lessons = lessons[:limit]
        log(f"{len(lessons)} lesson(s) with parts")
        if capped:
            log(f"  {len(capped)} lesson(s) past the failure cap")

        built = 0
        for lesson in lessons:
            counts.processed += 1
            key = lesson_transcript_key(
                lesson["id"], lesson["assemblyHash"], config_hash
            )
            # §4.2 fast path, by identity: the pointer already names this exact
            # key and the object is really there.
            if lesson["lessonTranscriptR2Key"] == key and key in published:
                counts.skipped += 1
                continue
            if lesson["lessonKey"] in capped:
                counts.skipped += 1
                continue
            try:
                artifacts = []
                for part in lesson["parts"]:
                    part_key = transcribe.transcript_key(part["sha256"], config_hash)
                    body = s3.get_object(Bucket=bucket, Key=part_key)["Body"].read()
                    artifacts.append(json.loads(body))
                payload = json.dumps(
                    compose_transcript(lesson, artifacts, config_hash),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=payload,
                    ContentType="application/json",
                )
                convex.mutation(
                    "mutations:setLessonTranscript",
                    lessonId=lesson["id"],
                    lessonTranscriptR2Key=key,
                )
                if lesson["lessonKey"] in retrying:
                    convex.mutation(
                        "mutations:resolveFailure",
                        stage=TRANSCRIPT_STAGE,
                        refKey=lesson["lessonKey"],
                    )
                counts.success += 1
                counts.audio_ms += lesson["durationMs"]
                built += 1
            except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                error = f"{type(exc).__name__}: {exc}"[:600]
                counts.failure += 1
                convex.mutation(
                    "mutations:recordFailure",
                    stage=TRANSCRIPT_STAGE,
                    refKey=lesson["lessonKey"],
                    error=error,
                )
                log(f"  !! {lesson['lessonKey']}: {error}")

        counts.notes.append(f"lessons={len(lessons)} built={built}")
        log(
            f"  {built} built, {counts.skipped} already current, "
            f"{counts.failure} failed"
        )
        return {
            "lessons": len(lessons),
            "built": built,
            "current": counts.skipped,
            "failed": counts.failure,
        }
