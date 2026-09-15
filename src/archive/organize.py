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
GROUPING_VERSION = "group-v3"
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
# A photo posted on its own this soon before an article's text is its picture.
# ponytail: a fixed window; 46 articles follow a bare photo, the admin unlinks misses.
ARTICLE_PHOTO_LEAD_MS = 10 * 60_000
# A title message is short. 97.3% of title candidates are under 100 chars; 300
# leaves room for @doros_alkulify's multi-line "series [ N ] / sub-series [ n ]"
# headers without admitting prose.
TITLE_MAX_CHARS = 300

# Grouping thresholds, from the gap histograms. @alkulife posts a lesson's parts
# as it records them: 82.3% of within-run gaps land in 300-600 s, against a
# between-run median of 30 hours. 600 s is where those two distributions part.
PART_GAP_S = 600
# …but 600 s cut 172 runs in two and left the tail half of each one nameless: a
# pause in the recording is not a new lesson, and the title is posted once, in
# front of the first part. So a *bare* part — no caption, no filename, nothing
# that could name it on its own — keeps joining across a longer pause. The
# measured tail of those gaps ends at 3.4 hours and the between-run median is
# 30, so 6 hours sits in empty space between the two.
RUN_GAP_S = 6 * 3600
# A recording this long is a whole sitting, not a part of one. @alkulife's parts
# have a median duration of 319 s; @doros_alkulify's lessons, 2,626 s. Nothing
# in the archive sits between 20 minutes and a part.
WHOLE_LESSON_MS = 20 * 60 * 1000
# A series header opens a run: one post names a book, the messages behind it are
# that book, one file per lesson. Two files is what makes it a series rather than
# a title — and 2017's runs match their own declared counts exactly, so a
# declared count of two is accepted where the files themselves were deleted.
SERIES_RUN_MIN = 2

URL = re.compile(r"https?://\S+")
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

# A bulk-series header: «شرح كتاب [ الصمت ] ... ( عدد الدروس : 11 ) 👇», then the
# whole book as eleven consecutive files. @doros_alkulify opened in 2017 by
# uploading its back catalogue this way, and 22 of its 38 declared counts match
# the run behind them file for file. The files are named 01.MP3, 02.MP3, so the
# series name exists nowhere except the header.
SERIES_COUNT = re.compile(r"عدد\s+الدروس\s*:?\s*(\d{1,4})?")
# The book's name, in brackets. Two brackets are not names and both appear here:
# one holding only a number is an episode («تعليق على الجامع [ ١٧٣ ]»), one
# holding the count is the header's own footnote («[ مكتمل - عدد الدروس 4 ]»).
SERIES_BRACKET = re.compile(r"\[\s*([^\]\n]{2,60}?)\s*\]")
# Everything a header carries that is not the book: the count, the pointer down
# at the files, the channel's topic tags.
SERIES_JUNK = re.compile(
    r"[(\[][^)\]]*عدد\s+الدروس[^)\]]*[)\]]"  # ( عدد الدروس : 49 )
    r"|عدد\s+الدروس\s*:?\s*\d*|#\S+|👇|\[[^\]]*\]"
)
# A file named nothing but its own number — the only numbering a bulk run has.
# `31-2` is the second half of episode 31, so it reads as 31 and the run's two
# rows both claim that episode, which is what they are. ponytail: 3 files in the
# archive do this; make them one lesson of two parts if a run ever splits often.
NUMERIC_FILENAME = re.compile(r"^0*(\d{1,4})(?:-\d{1,2})?$")


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

    Binaries an admin deleted are still counted here, on purpose. Grouping and
    `lesson_key` are derived from *which messages carry audio*, so forgetting a
    deleted file would slide a lesson's identity onto the next message and
    compose a duplicate next to the one the admin just edited. `live_media` is
    what a composition is actually built from.
    """
    return [media for media in message["media"] if transcribe.is_audio(media)]


def live_media(message: dict) -> list[dict]:
    """`audio_media` minus what an admin deleted — the parts a lesson gets."""
    return [media for media in audio_media(message) if media["deletedAt"] is None]


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


def episode_number(message: dict) -> int | None:
    """The episode a bulk-run file numbers itself, from its filename. `01.MP3` → 1.

    `filename_title` throws this name away and is right to — «01» is not a title.
    It is a position, though, and inside a series run it is the channel's own
    numbering, which survives a deleted message where counting from one does not.
    """
    for item in message["media"]:
        name = (item.get("originalFileName") or "").translate(_FILENAME_JUNK)
        hit = NUMERIC_FILENAME.match(_digits(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)))
        if hit is not None:
            return int(hit.group(1))
    return None


def series_header(messages: list[dict], position: int) -> str | None:
    """The series this text post opens, or None if it opens nothing.

    Three tests, and every one of them was needed against the corpus. It has to
    be a text post with no media of its own. It has to name a book — a bracketed
    name or a declared lesson count — which is what keeps «الدروس بصيغة أخرى 👇»
    (a re-upload announcement, 41 of them) and «تعليق على الجامع [ 173 ]» (an
    episode number in brackets) from minting series of their own. And a run of at
    least two files has to follow it, or a declared count of at least two, which
    is the difference between a series header and an ordinary lesson title.
    """
    message = messages[position]
    if message["mediaType"] != "none" or not (message["text"] or "").strip():
        return None
    text = _digits(message["text"])
    count = SERIES_COUNT.search(text)
    bracket = next(
        (
            name
            for name in SERIES_BRACKET.findall(text)
            if re.search(r"[ء-ي]", name) and "عدد" not in name
        ),
        None,
    )
    if count is None and bracket is None:
        return None
    declared = int(count.group(1)) if count and count.group(1) else 0
    run = 0
    for follower in messages[position + 1 :]:
        if audio_media(follower):
            run += 1
            continue
        # A group-call event carries neither text nor media; 635 of them sit in
        # this channel and none of them ends a run.
        if follower["mediaType"] == "none" and not (follower["text"] or "").strip():
            continue
        break
    if run == 0 or max(run, declared) < SERIES_RUN_MIN:
        return None
    if bracket is not None:
        return bracket
    name = SERIES_JUNK.sub(" ", text.split("\n")[0])
    return " ".join(name.split()).strip(" .:،-") or None


# The same lesson, encoded twice. The channel uploads the recording and puts the
# other format up minutes later — «الجامع 158 الرازيين 12.m4a», then the same
# thing as `.mp3` — and M1 cannot deduplicate those, because the bytes differ.
# Length to within two seconds is what survives a re-encode; the name is what
# keeps length from lying. Five 2017 pairs are different episodes of one book
# that happen to run the same length, and they are numbered files with no name
# at all, so demanding a name is what excludes them.
REPOST_DRIFT_MS = 2_000
REPOST_NAME_MIN = 6
# The other format goes up the same evening, so the pair is hours apart at most.
# The window is not a nicety: «تتمة مهمة» is a caption this channel reuses 100
# times over nine years, and without it 64 unrelated follow-up clips collide on
# that name the moment two of them happen to run the same length.
REPOST_WINDOW_S = 6 * 3600
_NOT_NAME = re.compile(r"[^\u0621-\u064A0-9]+")


def repost_name(message: dict) -> str:
    """A lesson's name reduced to what survives a re-encode.

    Letters and digits, no spaces. «الجامع 262 البربهاري 1» comes back with its
    numbers bracketed and written in the other digit alphabet, «عمدة الفقه 169
    كتاب القضاء» comes back with the tail cut off, and neither is a difference
    `normalize` plus this stripping cannot erase.
    """
    name = filename_title(audio_media(message)) or (message["text"] or "")
    return _NOT_NAME.sub("", normalize(name))


def reencoded(message: dict, composed: list[tuple[str, int, int]]) -> bool:
    """Is this message a composed lesson uploaded again in another format?"""
    media = live_media(message)
    if len(media) != 1:  # a multi-file message is a composition, not a re-upload
        return False
    name = repost_name(message)
    if len(name) < REPOST_NAME_MIN:
        return False
    duration = media[0]["durationMs"] or 0
    return any(
        message["date"] - date <= REPOST_WINDOW_S * 1000
        and abs(duration - other_ms) <= REPOST_DRIFT_MS
        and (name in other or other in name)
        for other, other_ms, date in composed
    )


def bare_part(message: dict) -> bool:
    """A recording that carries nothing able to name it: caption, filename, both
    absent. Only such a part may join its run across a long pause — a part that
    names itself can be a lesson of its own, and @doros_alkulify's re-upload
    batches are exactly that: N named files under one header post.
    """
    return not (message["text"] or "").strip() and (
        filename_title(audio_media(message)) is None
    )


# --------------------------------------------------------------------------- #
# Grouping v3
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

    v2 widens the pause a *bare* part may sit behind (`RUN_GAP_S`, `bare_part`).
    Nothing else about a lesson's identity moved, so a run that v1 cut in two
    now composes under the key of its first half and the second half's row is
    superseded — see `retire_superseded` for what happens to it.

    v3 adds the two things the 2017 archive needs and nothing else: a series
    header names the run behind it (`series_header`), and a message that re-posts
    a binary an earlier message already carried composes nothing, because the
    same recording is not two lessons.
    """
    lessons: list[dict] = []
    by_id = {message["telegramMessageId"]: message for message in messages}
    of_message: dict[int, int] = {}  # telegramMessageId -> index in `lessons`
    header: str | None = None  # the series a run of files belongs to
    run = 0  # lessons composed under it, for a file that numbers itself nowhere
    posted: set[str] = set()  # every binary already composed, by sha256
    composed: list[tuple[str, int, int]] = []  # (name, length, date), for re-encodes

    for position, message in enumerate(messages):
        media = audio_media(message)
        if not media:
            # A text post closes the run behind it, and may open the next one.
            if (message["text"] or "").strip():
                header, run = series_header(messages, position), 0
            continue
        if message["deletedAt"] is not None:
            continue
        # The same file, posted twice. 186 messages in @doros_alkulify do this —
        # a live stream's recording goes up, and the studio copy follows minutes
        # later under a machine name (`4_5906797496…m4a`). M1 deduplicated the
        # bytes, so the repost is provably the same recording, not a second one.
        live = [item["sha256"] for item in live_media(message)]
        if live and all(sha in posted for sha in live):
            continue
        if reencoded(message, composed):
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
            limit = RUN_GAP_S if bare_part(message) else PART_GAP_S
            if contiguous and short and gap <= limit:
                joined = len(lessons) - 1

        posted.update(live)
        name = repost_name(message)
        if len(live) == 1 and len(name) >= REPOST_NAME_MIN:
            composed = [
                row
                for row in composed
                if message["date"] - row[2] <= REPOST_WINDOW_S * 1000
            ]
            composed.append(
                (name, live_media(message)[0]["durationMs"] or 0, message["date"])
            )

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

        run += 1
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
                "series": header,
                # The file's own number first: a run whose third message was
                # deleted still calls its fourth file 04.MP3, and counting from
                # one would renumber the whole book from there.
                "episode": episode_number(message) or run,
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
    # Nothing on the lesson names it — but the post that opened its run does, and
    # the file numbers itself inside that run. «شرح علل الترمذي (4)» is written
    # in the shape `parse_title` already reads, so the series name and the
    # episode number come back out of it with no new plumbing.
    if lesson["series"] is not None:
        return f"{lesson['series']} ({lesson['episode']})", "series_header", 0.7
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


def article_photos(messages: list[dict], position: int) -> list[str]:
    """mediaObjectIds of an article's photos: its own, its album's, and bare
    photos posted right before it."""
    message = messages[position]
    album = message["groupedId"]

    def belongs(other: dict, before: bool) -> bool:
        if other["deletedAt"] is not None or other["mediaType"] != "photo":
            return False
        if album is not None and other["groupedId"] == album:
            return True
        return (
            before
            and not other["isForwarded"]
            and not (other["text"] or "").strip()
            and message["date"] - other["date"] <= ARTICLE_PHOTO_LEAD_MS
        )

    start = position
    while start > 0 and belongs(messages[start - 1], True):
        start -= 1
    end = position
    while end + 1 < len(messages) and belongs(messages[end + 1], False):
        end += 1
    ids: list[str] = []
    for other in messages[start : end + 1]:
        if other["mediaType"] != "photo":
            continue
        for media in other["media"]:
            if media["deletedAt"] is None and media["mediaObjectId"] not in ids:
                ids.append(media["mediaObjectId"])
    return ids


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
                "photoIds": article_photos(messages, position),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Superseded rows
# --------------------------------------------------------------------------- #


def retire_superseded(
    channels: tuple[str, ...],
    existing: dict[str, dict],
    produced: set[str],
    log=_log,
) -> tuple[int, int]:
    """Tombstone the lesson rows this grouping no longer composes. (retired, kept)

    A regrouping does not move a lesson's key, it merges one lesson into another
    — and the row left behind still lists the audio its neighbour now plays. So
    it has to go, or the site shows the same recording twice. Only rows belonging
    to the channels this run scanned in full are considered: a key from a channel
    nobody looked at is unvisited, not superseded.
    """
    prefixes = tuple(f"tg:{username}:" for username in channels)
    retired = kept = 0
    for key in sorted(set(existing) - produced):
        if not key.startswith(prefixes):
            continue
        result = convex.mutation(
            "mutations:retireSupersededLesson",
            lessonId=existing[key]["id"],
            reason=f"superseded by {GROUPING_VERSION}",
        )
        if result["retired"]:
            retired += 1
        else:
            kept += 1
            log(f"  superseded but frozen, left for a human: {key}")
    return retired, kept


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
            "deleted": 0,
            "frozen": 0,
            "needsReview": 0,
            "unchanged": 0,
            "retired": 0,
        }
        produced: set[str] = set()

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
                if title_source == "series_header":
                    # The header and the filename said both outright. Reading our
                    # own sentence back would let `FROM_SOURCE` cut «الأصول من علم
                    # الأصول» in half and file thirteen lessons under «علم الأصول».
                    parsed |= {
                        "seriesName": candidate["series"],
                        "seriesEpisode": candidate["episode"],
                    }
                title_message = candidate["title"]
                first = candidate["parts"][0]
                key = lesson_key(
                    username,
                    title_message["telegramMessageId"] if title_message else None,
                    first["telegramMessageId"],
                )
                produced.add(key)

                parts, offset = [], 0
                for part in candidate["parts"]:
                    for media in live_media(part):
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

                previous = existing.get(key)

                # Every binary under this lesson was deleted by an admin. There
                # is no lesson left to write, and the deleted row it may still
                # have is its own tombstone.
                if not parts:
                    totals["deleted"] += 1
                    counts.skipped += 1
                    continue

                # A hand-edited composition outranks a rerun (`partsLocked`).
                # `upsertLessonByKey` enforces it either way; skipping here just
                # spares three round trips per edited lesson.
                if previous is not None and previous["partsLocked"]:
                    totals["frozen"] += 1
                    counts.skipped += 1
                    continue

                # §4.7. An approved lesson is frozen — but a composition that no
                # longer matches, or a source message deleted underneath it, is
                # handed back to a human rather than kept or overwritten.
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

                # The no-op fast path. A rerun over a channel whose old messages
                # have not moved should cost reads and nothing else — this is
                # what keeps the Organizer from rewriting 5.6k unchanged lessons
                # every time the channel posts one new one. Sources are not
                # compared: they are derived from the same title/first-part
                # messages the assembly hash and title already pin down.
                if (
                    previous is not None
                    and previous["assemblyHash"] == digest
                    and previous["groupingVersion"] == GROUPING_VERSION
                    and previous["titleParserVersion"] == TITLE_PARSER_VERSION
                    and (previous["titleLocked"] or previous["rawTitle"] == title_text)
                ):
                    totals["unchanged"] += 1
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
                # A deleted lesson comes back `skipped`: its row survives only
                # as a tombstone, and writing parts under it would resurrect it.
                if lesson["skipped"]:
                    totals["frozen"] += 1
                    counts.skipped += 1
                    continue
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
                    sources=sources_of(candidate, channel["_id"]),
                )
                totals["lessons"] += 1
                totals["parts"] += len(parts)
                totals["changed"] += int(
                    lesson["created"] or written["changed"] or sources["changed"]
                )
                counts.success += 1
                counts.audio_ms += offset

        # A bounded run saw part of a channel, so most of its keys are missing
        # for a reason that has nothing to do with grouping.
        if limit is None:
            retired, kept = retire_superseded(channels, existing, produced, log)
            totals["retired"] = retired
            totals["changed"] += retired
            log(f"  {retired} superseded row(s) retired, {kept} left frozen")

        counts.notes.append(
            f"lessons={totals['lessons']} articles={totals['articles']} "
            f"changed={totals['changed']}"
        )
        log(
            f"  {totals['lessons']} lesson(s), {totals['parts']} part(s), "
            f"{totals['articles']} article(s), {totals['changed']} write(s), "
            f"{totals['needsReview']} needing review, {totals['demoted']} demoted, "
            f"{totals['retired']} retired, "
            f"{totals['unchanged']} unchanged, {totals['frozen']} frozen, "
            f"{totals['deleted']} deleted"
        )
        return totals


def sources_of(candidate: dict, channel_id: str) -> list[dict]:
    """`lessonSources` for one lesson: the Telegram message it came from.

    No YouTube rows. The plan expected trailing YouTube links to confirm a
    grouping, and the corpus already said they cannot — of 3,977 bare-link
    messages 1,417 sit between two other links and only 648 are followed by
    audio, because the links are posted in batches hours to days later. The
    archive's owner then settled the question outright: those uploads are the
    same recordings republished late, so they are not a second source of
    anything. `semanticType = "link"` still marks the messages; nothing binds
    them to a lesson.
    """
    first = candidate["parts"][0]
    origin = candidate["title"] or first
    return [
        {
            "sourceType": "telegram",
            "url": origin["telegramUrl"],
            "channelId": channel_id,
            "messageId": origin["id"],
            "isPrimary": True,
        }
    ]


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
