# Telegram Knowledge Archive — Implementation Plan v2.2 (FROZEN)

**Status: FROZEN implementation baseline.** Supersedes v2 / v2.1 / both review docs; standalone. The next architecture change requires a concrete implementation blocker discovered in real data — not another review round.
**Stack:** Python + Telethon · Convex · Cloudflare R2 · cohere-transcribe · Meilisearch · FFmpeg/ffprobe.

---

## 0. Disposition of the v2.2 Change Proposal

**Adopted as written:** #1 non-linear lesson state · #2 `lessonKey` + `assemblyHash` · #3 uniqueness via atomic Convex mutations (Convex indexes are not SQL UNIQUE; mutations are serializable, so lookup+write in one mutation enforces the invariant; >1 row on a "unique" query = integrity error, never silent pick) · #4 crash-window recovery + `reconcile-artifacts` · #5 pre-computed `configHash` from pinned config (provenance = post-hoc verification only) · #7 `pipelineLocks` split from append-only `pipelineRuns` (adopted now, not deferred — it's trivial and the ops page wants the history) · #9 identity hierarchy · #10 reprocessing flows · #12/#14 architectural freeze.

**Amendments:**
1. **Manual-override precedence (new invariant — missing from every prior doc):** an `approved` lesson is frozen against Organizer recomposition. If a source message of an approved lesson is edited or deleted, the lesson is demoted to `needs_review`; it is never silently recomposed. `auto` lessons recompose freely on rerun. Without this rule, "fully re-runnable Organizer" and "manual review" destroy each other.
2. `needsReview` field dropped — redundant with `reviewStatus == needs_review`.
3. `assemblyHash` input = the **ordered list of part sha256s only** (canonical JSON). Offsets are derivable and live in the artifact body; hashing them would churn identity on any ffprobe re-run.
4. `lessonSources` phased: `telegram` + `youtube` mirror rows are populated in Phase 3 (free — from trailing YouTube-link messages and the v1 YT↔TG map, and they give users a "watch on YouTube" link); `legacy` / YouTube-only rows are created only if the §8 coverage diff is non-empty.
5. Setup note for #5: the pinned `modelRevision` is resolved **once** (branch→commit) at configuration time and stored in config; the worker fails fast if the resolved default ever differs from the pinned value.

---

## 1. Identity Hierarchy (conceptual core)

```text
raw media identity            = sha256
part transcript identity      = sha256 + configHash
lesson identity               = lessonKey        (deterministic from source;
                                                  Convex _id stable via upsert-by-key —
                                                  this is what legitimizes lessonId-based
                                                  R2 keys and Meilisearch chunk IDs)
lesson composition identity   = assemblyHash     (ordered part sha256s)
lesson transcript identity    = assemblyHash + configHash
merged audio identity         = mergedSha256     (of the produced file)
```

Staleness is checked by identity, not flags: a lesson-transcript artifact is current iff its key embeds the lesson's **current** `assemblyHash` and the **active** `configHash`. Same pattern for chunks and merged audio.

---

## 2. Convex Schema (consolidated, final)

```text
channels
  username, title, lastMessageId, lastSyncAt

telegramMessages
  channelId, telegramMessageId, date, editDate, deletedAt?
  text, replyToMessageId, groupedId, telegramUrl
  mediaType:    none | audio | voice | video | photo | document
  semanticType: article | lesson_title | link | notice | other | null
  isForwarded, forwardedFromChannel?, forwardedFromMsgId?
  classifierVersion
  idx: by_channel_message, by_channel_date

mediaObjects                      -- one row per unique binary
  sha256 (idx; uniqueness via getOrCreateMediaObject)
  r2Key, ext, sizeBytes, mimeType
  durationMs, codec, sampleRate, channelCount     -- ffprobe at ingest
  firstSeenAt

messageMedia                      -- occurrence -> binary
  messageId, mediaObjectId
  telegramDocId                   -- MTProto document.id, reference only;
                                  -- durable re-download path = (channel, msgId)
  originalFileName
  idx: by_message, by_media_object

partTranscripts
  sha256, configHash (idx; uniqueness via upsertPartTranscript)
  status: pending | processing | done | failed
  processingStartedAt?, processingRunId?, attempts
  model, modelRevision, rawR2Key, durationMs, segmentCount, error?

articles
  messageId, channelId, title, normalizedTitle, titleSource
  text, normalizedText, date, telegramUrl, indexedAt?, indexVersion?

lessons
  lessonKey (idx; uniqueness via upsertLessonByKey)   -- deterministic from
                                                      -- channel + title msgId
                                                      -- + first source msgId
  assemblyHash                                        -- ordered part sha256s
  channelId?                                          -- nullable: non-Telegram
  rawTitle, normalizedTitle
  seriesName?, normalizedSeriesName?, seriesEpisode?, lessonPartLabel?
  titleParserVersion, titleParseConfidence
  groupingVersion, groupingConfidence
  reviewStatus: auto | needs_review | approved        -- §0 amendment 1 governs
  mergeStatus:  pending | processing | done | failed
  mergedR2Key?, mergedSha256?
  lessonTranscriptR2Key?
  indexedAt?, indexVersion?
  titleMessageId?, firstTelegramMessageId?, lastTelegramMessageId?
  partCount, durationMs

lessonParts
  lessonId, messageId, mediaObjectId, order, durationMs, offsetMs
  idx: by_lesson_order, by_message

lessonSources
  lessonId
  sourceType: telegram | youtube | legacy
  url, externalId?, channelId?, messageId?, isPrimary, metadata?

failures
  stage, refKey, error, attempts, lastTriedAt, resolved

pipelineLocks                     -- singleton per stage
  stage (idx; uniqueness via acquirePipelineStage), runId, acquiredAt,
  heartbeatAt, owner

pipelineRuns                      -- append-only history
  runId, stage, startedAt, finishedAt?, status
  processedCount, successCount, failureCount, skippedCount
  wallTimeMs?, audioDurationMs?, summary?
```

No `transcriptSegments` table. No single linear lesson `status`.

Atomic mutations enforcing logical uniqueness: `getOrCreateMediaObject(sha256)`, `upsertPartTranscript(sha256, configHash)`, `upsertLessonByKey(lessonKey)`, `acquirePipelineStage(stage)`.

---

## 3. R2 Layout

```text
telegram-archive/                         (private, permanent)
  blobs/{sha256[:2]}/{sha256}.{ext}
  meta/{channel}/{from:08d}-{to:08d}.jsonl.zst
  meta/{channel}/manifest.json            (schemaVersion, range, count,
                                           createdAt, batch sha256)
  transcripts/{sha256}/{configHash}.json
  lesson-transcripts/{lessonId}/{assemblyHash}-{configHash}.json
                                          (organizerVersion recorded inside)
  legacy/assoli-v1/                       (one-time export, §8)

lessons-media/                            (public via custom domain)
  lessons/{lessonId}/{mergedSha256}.{m4a|opus}    (codec per Phase-0 gate)
```

---

## 4. Coordination, Idempotency, Recovery

1. **Write-order law:** artifact to R2 first, Convex `done` second — every stage.
2. **Skip rule (fast path):** skip iff the authoritative Convex record is `done`.
3. **Crash-window recovery (slow path):** for `pending` / stale-`processing` / `failed` / missing records only — compute the deterministic expected key, `HEAD` it; exists + basic validation → promote Convex to `done` and skip; absent → process. (`existing="skip"` stays on as a same-machine belt.) The worker knows `inputSha256`, `configHash`, and `expectedR2Key` **before** inference starts.
4. **`reconcile-artifacts` command:** A) Convex `done` + R2 missing → flag + mark for reprocessing; B) R2 exists + Convex not `done` → validate + repair Convex; C) R2 orphan with no Convex reference → report only; never auto-delete archival objects. Reused for part transcripts, lesson transcripts, merged audio.
5. **Stage locks:** every batch command takes a local `flock` **and** `acquirePipelineStage` (free, or heartbeat stale > 5 min); heartbeat 60 s; on exit release lock and append a `pipelineRuns` row with counts. Per-record claims remain out of scope until ≥2 concurrent workers exist for one stage.
6. **Failures first:** each run drains unresolved `failures` for its stage before new work; attempts capped, then ops page.
7. **Override precedence:** per §0 amendment 1 — reruns never touch `approved` compositions; source-message changes demote to `needs_review`.
8. **Full re-runs stay legal** for Organizer (subject to rule 7), Lesson Transcript Builder, Chunk Builder, reindex.

**Reprocessing map:** grouping change → new `assemblyHash` → regen lesson transcript + chunks + merge; transcription config change → new `configHash` → new part transcripts → downstream; normalization/chunk/index change → rebuild search documents only; nothing downstream ever requires re-downloading Telegram.

---

## 5. Timestamps, Chunks, Playback (unchanged from v2.1)

`lessonStartMs = part.offsetMs + segment.startMs`; offsets = cumulative ffprobe durations captured at ingest (Telegram's own durations are whole-second and unusable for ms offsets). Chunks 45–90 s, deterministic IDs, always joining part-N tails with part-N+1 heads. Player seeks `max(0, startMs − 2000)`. Merged retranscription per lesson stays a cheap optional exception.

---

## 6. Phases

### Phase 0 — Access, gates, config pinning, legacy export (½–1 day)
- Dedicated Telegram account; SQLite `.session` file treated as a secret.
- HF model terms + `HF_TOKEN`/`HF_HOME`; `cohere-transcribe-doctor --model-access`.
- **Pin transcription config** (model, resolved modelRevision commit, language=ar, vad, vadMerge, alignment) → canonical-JSON → `configHash` computed and logged before any inference.
- **Benchmark the actual GPU** (RTF, files/batch, VRAM) → real archive runtime; schedule from measurement.
- **Codec gate:** Opus vs AAC seek/Range on iOS Safari, Android, desktop; record decision.
- **Legacy export first:** assoli-v1 → `legacy/assoli-v1/` (transcripts, human corrections, query logs, YT↔TG map). v1 stays live and untouched.
- Provision Convex, R2 buckets + scoped keys, Meilisearch keys; repo skeleton; atomic uniqueness mutations from day one.

### Phase 1 — Archive everything (irreplaceable; Telegram-clocked — start immediately)
- Takeout on the persistent session (persist takeout id; handle `TakeoutInitDelay`, flood waits); chronological; checkpoint per channel.
- Per message: structured upsert (mediaType set here; semanticType null) + raw dict to `.jsonl.zst` batches + manifest.
- Media: download → sha256 → `getOrCreateMediaObject` (repost = `messageMedia` row only) → **ffprobe** → upload blob → rows → delete temp.
- Forward info, `grouped_id`, replies, edit dates. Kill-safe at any point.
- Validation: counts vs channel stats; 20 random `telegramUrl` spot-checks.

### Phase 2 — Transcribe all parts (batch campaign)
- Materialize needed blobs locally (`{sha256}.{ext}` — plain copy).
- Selection via §4.2 fast path; §4.3 recovery for the rest; batches of ~500 under the stage lock; persistent `Transcriber` (`language="ar"`, `vad_merge=True`, JSON out).
- Per file: upload JSON → `upsertPartTranscript` done (write-order law). Verify returned provenance matches pinned config; mismatch = failure, not a new identity.
- **Exit:** ≥99% of unique audio sha256s `done`.

### Phase 3 — Organize (pure; reads Convex only)
- Classifier v1 → `semanticType` + version; forwarded audio flagged and excluded from sheikh-voice lessons by default.
- Articles (+`normalizedTitle`, `titleSource`).
- Series parser (all fields incl. `normalizedSeriesName`, version, confidence); tested on ≥50 real titles **per channel per era**.
- Grouping v1 (grouped_id → title-then-consecutive-audio → replies → YouTube-link confirmation → time proximity as support) → confidence, `reviewStatus` auto/needs_review.
- `upsertLessonByKey`; compute `assemblyHash`; offsets from `mediaObjects.durationMs`.
- `lessonSources`: telegram row (primary) + youtube mirror row from link messages / v1 map.
- Rule §4.7 enforced: approved lessons untouched.

### Phase 3.5 — Lesson Transcript Builder (derived, disposable)
- Per lesson: concat part JSONs + offsets → `lesson-transcripts/{lessonId}/{assemblyHash}-{configHash}.json`; set pointer. Regenerated automatically whenever `assemblyHash` changes.

### Phase 4 — Search v0 (beta alongside live v1)
- Chunks from lesson-transcript artifacts (part-level fallback for ungrouped audio); deterministic IDs.
- Meilisearch `articles` + `audio_chunks`: searchable `normalizedTitle`, `normalizedText`; displayed `title`, `text`; filters `channel`, `seriesName`, `lessonId`; sort `date`. Full `reindex` proven; `indexedAt`/`indexVersion` stamped.
- UI: RTL, tabs, excerpts, series facets, Telegram links; raw-part playback via short-lived signed R2 URLs (Range OK; refresh on 403). Raw bucket private forever.
- **Exit:** replay v1 query logs against v1 and v2 as the relevance test set.

### Phase 5 — Merged playback
- concat-demuxer fast path / re-encode fallback to gated codec; merged sha256 → public bucket; Σ durations ≈ merged (warn > 500 ms); `mergeStatus` lifecycle.
- Player: seek −2 s, `?t=` links, autoplay handling. Chunk re-attribution = pure reindex.

### Phase 6 — Review & continuous sync
- Review UI: needs_review queue by confidence; preview/reorder/add/remove/split/merge/rename; approve → new `assemblyHash` → regen transcript + remerge + reindex (lesson-scoped).
- Incremental sync on a normal session: > `lastMessageId` + ~300-message recheck (edits/`deletedAt`); affected approved lessons demote to `needs_review` (§4.7), never recompose.
- systemd timers under §4.5 locks: sync 15 min → transcribe-pending → index-pending; merge nightly.

### Phase 7 — Hardening & relevance
- Ops page: stage counts, `failures`, `pipelineLocks` state, `pipelineRuns` history (last run, duration, counts), per-channel sync.
- `reconcile-artifacts` scheduled weekly; retry wrappers; temp cleanup; signed-URL endpoint rate-limited; least-privilege keys; search-only Meili key client-side.
- Relevance from real v1 query logs: hamza/diacritics variants, Arabic typo tolerance, chunk duration, title weight; then decide if raw `text` joins searchable.
- Recovery drills incl. the §4.3 crash window; reindex-from-scratch proven.

---

## 7. Freeze Criteria (in force)

The remaining uncertainty — title-format drift across eras, grouping accuracy, ASR quality on عقدي/فقهي vocabulary, real GPU RTF, actual type distribution — is resolvable only by real data. The highest-value milestone is the full raw archive in Convex + R2; once durable, everything downstream is reproducible. **No further architecture documents. Changes from here are commits, driven by concrete blockers.**

---

## 8. Migration from assoli-v1 (mandatory, unchanged)

1. Export first (Phase 0): transcripts, corrections, query logs, YT↔TG map. "Delete" = drop from the new index, never destroy data.
2. Coverage diff after Phase 1: YouTube-only items → keep as `lessonSources.sourceType = youtube/legacy` lessons; empty diff → delete YouTube transcripts with a clear conscience.
3. Corrections audit: manually corrected v1 transcripts import as overrides — the only data no GPU can reproduce.
4. Query logs = the Phase-4/7 relevance regression set.
5. Parity cutover after Phase 5: coverage ≥ v1 and log-replay results equal-or-better → switch domain + redirects + sheikh announcement. v1 stays live until then.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Telegram limits/ban | Dedicated account, takeout, pacing, resumable checkpoints |
| GPU slower than reference | Phase-0 benchmark sets schedule; architecture speed-independent |
| Organizer rerun vs manual edits | §0 amendment 1 / §4.7 precedence rule |
| Part-boundary truncation | Cross-part chunk joining; optional merged retranscription |
| Approximate timestamps | Seek −2 s; excerpt shown |
| Title/grouping drift across eras | Versioned parser + grouping; confidence; review queue; legal full reruns |
| iOS codec | Phase-0 gate |
| Signed-URL expiry mid-listen | Short parts + refresh on 403 |
| Losing v1 assets | §8 export + coverage diff gate deletion |
| **Planning loop** | This document is frozen; the next artifact is running code |

---

## 10. Open Items (do not block Phase 0/1)

1. GPU location (desktop vs server) — shapes Phase-6 timers only.
2. Phase-1 download scope — **decided: `@alkulife` (14,787 msgs) and `@doros_alkulify` (11,899) only.** `@T_alkulife` (904), `@alkulifyfgh` (16) and `@KulifyAntiCapitalism` (9) are out of scope for now.
3. assoli-v1 salvage — any manually corrected transcripts? Anything else in the v1 data model (accounts, bookmarks, analytics) worth exporting?
