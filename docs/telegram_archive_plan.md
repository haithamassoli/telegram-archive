# Telegram Knowledge Archive — Implementation Plan v2.4 (FROZEN)

**Status: FROZEN implementation baseline.** Supersedes v2 / v2.1 / v2.3 / both review docs; standalone. The next architecture change requires a concrete implementation blocker discovered in real data — not another review round. v2.3 (2026-09-05) filled declared-but-thin sections and closed gaps found while operating M1/M2. v2.4 (2026-09-11) **removes** audio merging: a lesson stays a list of parts and the player concatenates them on a virtual timeline. Scope removal, not addition; log in §11.
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
search chunk identity         = lessonId + assemblyHash + seq
embedding identity            = embedHash        (pinned embed config; Phase 4.5)
```

Staleness is checked by identity, not flags: a lesson-transcript artifact is current iff its key embeds the lesson's **current** `assemblyHash` and the **active** `configHash`. Same pattern for chunks.

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
  lessonTranscriptR2Key?
  indexedAt?, indexVersion?
  titleMessageId?, firstTelegramMessageId?, lastTelegramMessageId?
  partCount, durationMs

lessonParts                       -- the playback timeline; see §5
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
  backups/convex/{yyyy-mm-dd}.zip         (weekly `npx convex export` — §6 Phase 6;
                                           the only non-reproducible data is human
                                           decisions living in Convex)
```

**One bucket.** There is no public media bucket: parts are served from the private archive
bucket through short-lived signed URLs (§5). `lessons-media` and its custom domain were
dropped in v2.4 along with merging.

---

## 4. Coordination, Idempotency, Recovery

1. **Write-order law:** artifact to R2 first, Convex `done` second — every stage.
2. **Skip rule (fast path):** skip iff the authoritative Convex record is `done`.
3. **Crash-window recovery (slow path):** for `pending` / stale-`processing` / `failed` / missing records only — compute the deterministic expected key, `HEAD` it; exists + basic validation → promote Convex to `done` and skip; absent → process. (`existing="skip"` stays on as a same-machine belt.) The worker knows `inputSha256`, `configHash`, and `expectedR2Key` **before** inference starts.
4. **`reconcile-artifacts` command:** A) Convex `done` + R2 missing → flag + mark for reprocessing; B) R2 exists + Convex not `done` → validate + repair Convex; C) R2 orphan with no Convex reference → report only; never auto-delete archival objects. Reused for part transcripts and lesson transcripts.
5. **Stage locks:** every batch command takes a local `flock` **and** `acquirePipelineStage` (free, or heartbeat stale > 5 min); heartbeat 60 s; on exit release lock and append a `pipelineRuns` row with counts. Per-record claims remain out of scope until ≥2 concurrent workers exist for one stage.
6. **Failures first:** each run drains unresolved `failures` for its stage before new work; attempts capped, then ops page.
7. **Override precedence:** per §0 amendment 1 — reruns never touch `approved` compositions; source-message changes demote to `needs_review`.
8. **Full re-runs stay legal** for Organizer (subject to rule 7), Lesson Transcript Builder, Chunk Builder, reindex.

**Reprocessing map:** grouping change → new `assemblyHash` → recompute `lessonParts.order`/`offsetMs` → regen lesson transcript + chunks; transcription config change → new `configHash` → new part transcripts → downstream; normalization/chunk/index change (`normVersion`) → rebuild search documents only; embed config change → new `embedHash` → re-embed + vector update only; nothing downstream ever requires re-downloading Telegram.

---

## 5. Timestamps, Chunks, Playback

`lessonStartMs = part.offsetMs + segment.startMs`; offsets = cumulative ffprobe durations captured at ingest (Telegram's own durations are whole-second and unusable for ms offsets). Chunks 45–90 s, deterministic IDs, always joining part-N tails with part-N+1 heads. Player seeks `max(0, startMs − 2000)`.

**The lesson timeline is virtual (v2.4).** There is no merged file. `lessons.durationMs` is Σ part durations and `lessonParts.offsetMs` is the running total, so the timeline every chunk is already stamped against *is* the concatenation — nothing has to be re-attributed, and the "Σ parts vs merged file" drift class does not exist.

Playback contract:
- Locate `t` → the part where `offsetMs ≤ t < offsetMs + durationMs`; play it at `(t − offsetMs) / 1000`.
- One `<audio>` element, `src` swapped on `ended`. If the boundary gap is audible, add a second element and unlock it inside the first user gesture (`play()` then immediate `pause()`) — iOS Safari blocks programmatic playback on an element no gesture has touched.
- Prefetch the next part when ~30 s remain, never the whole lesson: a ten-part lesson would otherwise burn a mobile reader's data on open.
- `navigator.mediaSession.setPositionState()` gets the *lesson* duration and position. Without it the lock screen shows part boundaries — exactly what this design hides.
- Parts are served from the private bucket via short-lived signed URLs, refreshed on 403. This is a permanent production path, not an interim (see §9).
- Part boundaries are rendered in the review UI (§6) and hidden in the public player. Same component, one prop.

**Known ceiling:** the virtual timeline trusts stored ffprobe durations. A part whose real decoded length differs (VBR without a Xing header) drifts the *displayed* position by that delta; playback self-corrects at each `ended`. Sub-second, and caught at ingest if it matters.

**No single-file download.** A lesson has no one shareable/downloadable object, and a podcast RSS feed would have nothing to point at. If either is ever wanted, concatenate on demand for that one lesson — the ffmpeg call was never the hard part; the lifecycle and reconciliation were, and they are what v2.4 deletes.

---

## 6. Phases

### Phase 0 — Access, gates, config pinning, legacy export (½–1 day)
- Dedicated Telegram account; SQLite `.session` file treated as a secret.
- HF model terms + `HF_TOKEN`/`HF_HOME`; `cohere-transcribe-doctor --model-access`.
- **Pin transcription config** (model, resolved modelRevision commit, language=ar, vad, vadMerge, alignment) → canonical-JSON → `configHash` computed and logged before any inference.
- **Benchmark the actual GPU** (RTF, files/batch, VRAM) → real archive runtime; schedule from measurement.
- **Playback gate (v2.4):** the source files ship as-is — nothing is re-encoded — so this is no longer "Opus vs AAC" but "do the archive's own containers seek and honour Range on iOS Safari, Android, desktop?" Test the real extensions present in `mediaObjects.ext`; record decision + `testedOn`.
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
- **Deployment:** self-hosted, version pinned in `.env`; on the dev machine for the beta, moves to the VPS at cutover (Phase 6 placement). Master key server-side only; search-only key in the client (Phase 7). The index is derived data: recovery = reindex from Convex + R2 (drilled in Phase 7), so no Meilisearch snapshots.
- **Normalization contract (`normVersion`):** one versioned function used for index-side `normalized*` fields, query preprocessing, and the §8.4 log replay — never two implementations. v1: strip tashkeel + tatweel; أ/إ/آ/ٱ → ا; ة → ه; ى → ي; Arabic-Indic digits → Latin; collapse whitespace. Raw `title`/`text` always stored for display. Bumping it rebuilds search documents only (§4 map). Meilisearch's own Arabic folding (charabia) may overlap; ours is authoritative so replay results never depend on Meilisearch internals.
- **Documents:**
  ```text
  audio_chunks   id = {lessonId}:{assemblyHash[:8]}:{seq:04d}
    lessonId, channel, seriesName?, seriesEpisode?, date, startMs, endMs
    title, normalizedTitle, text, normalizedText, telegramUrl
  articles       id = {channelId}:{telegramMessageId}
    channel, date, title, normalizedTitle, text, normalizedText, telegramUrl
  ```
  Lesson recomposition = delete by `lessonId` filter, re-add; ids embed `assemblyHash` so stale chunks cannot collide with fresh ones.
- **Initial settings (tuned in Phase 7, never invented there):** synonyms seeded from `legacy/assoli-v1/domain-synonyms.json`; ranking = Meilisearch default order with attribute weight title > text; `distinctAttribute = lessonId` on `audio_chunks` (one hit per lesson in mixed results; in-lesson occurrences via a `lessonId`-filtered follow-up query); typo tolerance on, `minWordSizeForTypos` tuned for short Arabic tokens.
- UI: RTL, tabs, excerpts, series facets, Telegram links; raw-part playback via short-lived signed R2 URLs (Range OK; refresh on 403). Raw bucket private forever.
- **Exit:** replay v1 query logs against v1 and v2 as the relevance test set.

### Phase 4.5 — Hybrid semantic search (bge-m3; gated, optional)
**Gate:** build keyword v0 first and run the §8.4 replay. Enter this phase only if the replay shows recall failures that normalization + synonyms do not close (paraphrase queries returning nothing relevant). Keep it only if hybrid beats keyword on the same replay. No baseline → no measurable gain → no vectors.
- **Why bge-m3:** Arabic derivational morphology and paraphrase-heavy فقهي/عقدي phrasing break exact-token recall (query «حكم الاحتفال بالمولد» vs a lesson that says «بدعية إقامة الموالد»). Dense embeddings retrieve by meaning. bge-m3 is multilingual with strong Arabic, 8k context, 1024-d dense output, and runs locally — no per-query API cost, no text leaving the machines.
- **Identity:** `embedHash` = canonical JSON of {model `BAAI/bge-m3`, resolved revision commit, dim 1024, normalize true} — same law as `configHash`: resolved once, pinned, drift = failure; change = re-embed everything (§1, §4 map). Dense vectors only; sparse/ColBERT outputs are skipped — the keyword side already owns lexical matching.
- **Index side:** the nightly job on the GPU host embeds new/changed chunks (from `normalizedText`) and ships vectors on the same documents as `_vectors.default` with a `userProvided` embedder (dimensions 1024).
- **Query side:** a small embed service (ONNX int8, CPU) beside Meilisearch on the VPS embeds the query; client calls hybrid search with `semanticRatio` starting at 0.3–0.5, tuned by replay in Phase 7.
- **Sizing:** ~150–200k chunk docs → ~0.6–0.8 GB raw vectors + ANN overhead; the VPS needs ~4 GB RAM, or enable Meilisearch binary quantization.

### Phase 5 — Continuous multi-part playback
- Player over the §5 virtual timeline: locate-part, seek −2 s, `?t=` deep links, gapless-enough boundary, prefetch-next, Media Session position, autoplay handling.
- Signed-URL endpoint hardened for production: rate limit, 403 refresh, Range verified.
- Verified on the Phase-0 device matrix: seek across a boundary, resume mid-part, lock screen shows lesson duration.
- Chunks need no change at all — they were stamped against this timeline from Phase 4.

### Phase 6 — Review & continuous sync
- Review UI: needs_review queue by confidence; preview/reorder/add/remove/split/merge/rename; approve → recompute `order`/`offsetMs` → new `assemblyHash` → regen transcript + reindex (lesson-scoped).
- Incremental sync on a normal session (no takeout — that privilege is for the historical bulk pull only): > `lastMessageId` + ~300-message recheck (edits/`deletedAt`); affected approved lessons demote to `needs_review` (§4.7), never recompose.
- **Timers under §4.5 locks — the whole automation story.** No bot, no daemon, no framework: each cadence is one dumb wrapper script chaining the existing idempotent commands, fired by the OS scheduler. Every command self-locks and skips `done` work, so blind scheduling is the design, not a compromise.
  ```text
  every 15 min   sync → transcribe-pending → organize → index-pending
  nightly        Phase-4.5 embed batch, if adopted (otherwise nothing)
  weekly         reconcile-artifacts · `npx convex export` → backups/convex/
  ```
- **Placement (closes Open Item 1):** the GPU host is the M-series Mac. Until cutover, all timers run there as launchd LaunchAgents (macOS has no systemd). At cutover they move as systemd timers to the VPS that hosts Meilisearch and the site anyway. Transcription then either stays on the Mac or runs on the VPS CPU — transcript identity is the pinned config, not the device, so both are legal; and the §4.5 stage locks already arbitrate two machines, so splitting stages across VPS + Mac needs no new code. The session file moves to the VPS as a secret with the same care as `.env`.
- **Failure push:** at the end of each wrapper run, any non-ok `pipelineRuns` row or new unresolved `failures` row → one Telegram message via a plain bot token (`sendMessage`, ~10 lines). The bot sends only; it never reads channels — the push complement to the Phase-7 ops page.
- **Human-decision backup:** the weekly Convex export above exists because §7's "everything downstream is reproducible" is true of pipeline output only — approvals and manual corrections live in Convex and no GPU can regenerate them. Restore is drilled in Phase 7.

### Phase 7 — Hardening & relevance
- Ops page: stage counts, `failures`, `pipelineLocks` state, `pipelineRuns` history (last run, duration, counts), per-channel sync.
- `reconcile-artifacts` scheduled weekly; retry wrappers; temp cleanup; least-privilege keys; search-only Meili key client-side. (Signed-URL rate limiting moved up to Phase 5 — v2.4 makes that endpoint the only way audio reaches a listener.)
- Relevance from real v1 query logs: hamza/diacritics variants (via `normVersion`), Arabic typo tolerance, chunk duration, title weight, `distinctAttribute` revisit, hybrid `semanticRatio` (if Phase 4.5 was adopted); then decide if raw `text` joins searchable.
- Recovery drills incl. the §4.3 crash window; reindex-from-scratch proven; Convex snapshot restore walked through once.

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
| Part-boundary truncation | Cross-part chunk joining (§5) |
| Approximate timestamps | Seek −2 s; excerpt shown |
| Title/grouping drift across eras | Versioned parser + grouping; confidence; review queue; legal full reruns |
| iOS codec / Range on source containers | Phase-0 playback gate |
| Audible gap at a part boundary | Prefetch-next; second unlocked `<audio>` element if measured gap is audible (§5) |
| Displayed position drifts from real audio | Stored ffprobe durations; self-corrects at each `ended` (§5) |
| Signed-URL expiry mid-listen | Short parts + refresh on 403; endpoint rate-limited in Phase 5 |
| Losing v1 assets | §8 export + coverage diff gate deletion |
| **Planning loop** | This document is frozen; the next artifact is running code |

---

## 10. Open Items (do not block Phase 0/1)

1. GPU location — **decided (v2.3): the M-series Mac is the GPU host; timer placement per Phase 6** (launchd on the Mac now, systemd on the VPS at cutover, stages splittable across both under the §4.5 locks).
2. Phase-1 download scope — **decided: `@alkulife` (14,787 msgs) and `@doros_alkulify` (11,899) only.** `@T_alkulife` (904), `@alkulifyfgh` (16) and `@KulifyAntiCapitalism` (9) are out of scope for now.
3. assoli-v1 salvage — any manually corrected transcripts? Anything else in the v1 data model (accounts, bookmarks, analytics) worth exporting?
4. Video scope (surfaced by the M2 full scan): 94 video containers carry 6.2 h of audio, excluded from Phase 2 by its "unique audio sha256s" wording. **Decide before Phase 3** whether their audio joins the transcript corpus; recommendation is yes — ~40 min of GPU at the measured campaign rate, and they become searchable lessons.

---

## 11. v2.3 Amendment Log (2026-09-05)

Spec completion, not architecture change — every item below either details a section the plan already declared or closes a gap found while operating M1/M2 for real. The freeze holds.

1. **Phase 6 automation spelled out:** wrapper-script-per-cadence over the existing self-locking commands; launchd-on-Mac now / systemd-on-VPS at cutover; two-machine stage split legalized by the existing locks; CPU transcription legalized by identity-is-config; failure push via a send-only bot token. Closes Open Item 1.
2. **Human-decision backup (gap):** weekly `npx convex export` → `backups/convex/` in the private bucket, restore drilled in Phase 7. §7's reproducibility claim covers pipeline output, not approvals/corrections; this was the one unbacked-up data class.
3. **Phase 4 search spec filled in:** deployment + key handling, the `normVersion` normalization contract (one function for index, query, and replay), concrete document shapes with `assemblyHash`-embedding chunk ids, and initial index settings including synonyms seeded from the v1 `domain-synonyms.json`.
4. **Phase 4.5 added — hybrid semantic search with bge-m3, explicitly gated** on the §8.4 keyword-replay showing recall gaps that normalization + synonyms cannot close. Embeddings get the same identity discipline as transcripts (`embedHash`), added to §1 and the §4 reprocessing map.
5. **Open Item 4 added:** the 94-video / 6.2 h scope decision the M2 scan surfaced, due before Phase 3.

---

## 12. v2.4 Amendment Log (2026-09-11) — merging removed

An owner decision, and a **scope removal**: the plan gets smaller, no new architecture enters. Phase 5 built one continuous file per lesson; it is replaced by a player that concatenates the parts a lesson already has. The freeze holds — nothing below adds a moving part.

**Why it is not merely neutral.** Merging did not only buy a single file, it bought a class of failure modes with it: the Σ-part-durations vs merged-file drift check (the 500 ms warning), a re-encode fallback whenever parts disagreed on codec, and a re-merge after every approval. With a virtual timeline that whole class is gone by construction — the timeline *is* Σ ffprobe durations, which is the number chunks were already stamped against. Heterogeneous part codecs also stop being a problem, since each file decodes on its own.

**Removed:**

1. Merge worker: concat-demuxer fast path, re-encode fallback, `mergeStatus` lifecycle, the >500 ms duration warning.
2. `lessons.mergeStatus` / `mergedR2Key` / `mergedSha256` (§2), and `mergedSha256` from the §1 identity hierarchy.
3. The `lessons-media` public bucket, its custom domain, and the M0 key split that existed to serve it (§3).
4. `reconcile-artifacts` coverage of merged audio (§4.4); the `+ merge` leg of the reprocessing map.
5. The nightly `merge` timer (Phase 6) and the remerge step in the approve flow.
6. "Chunk re-attribution to the merged timeline" — there is no second timeline to re-attribute to.
7. "Optional merged retranscription" as a part-boundary mitigation (§9); cross-part chunk joining already covers it.

**Added, in exchange:**

1. The §5 playback contract: locate-part, single `<audio>` with `src` swap, iOS gesture-unlock for a second element if needed, prefetch-next at ~30 s, Media Session position on the lesson duration, boundaries shown to reviewers and hidden from readers. Roughly 80 lines over the v1 player.
2. Phase 5 re-purposed to that player plus hardening the signed-URL endpoint, which v2.4 promotes from an interim convenience to the only path audio takes to a listener. Rate limiting moves from Phase 7 to Phase 5.
3. Reorder in the review UI now recomputes three things in order: `lessonParts.order`, then `offsetMs` cumulatively, then `assemblyHash` — the last invalidating the lesson transcript and its chunks, exactly as a grouping change always did.

**Accepted costs, named:**

1. **No single downloadable or externally shareable object per lesson**, and no podcast RSS feed without one. Remedy if ever wanted: concatenate on demand for that one lesson.
2. **The signed-URL endpoint is now load-bearing.** R2 egress is free so the bill does not move, but an outage there is now an outage of playback.
3. **A part boundary is a seam.** It is engineered down to inaudible, not to zero.

**Phase numbering is unchanged** — Phase 5 keeps its slot, so every "after Phase 5" reference (notably the §8.5 parity cutover) still reads correctly.
