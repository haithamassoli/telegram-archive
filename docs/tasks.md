# Telegram Knowledge Archive — Milestones & Tasks

Derived from `docs/telegram_archive_plan.md` (v2.2, FROZEN). Milestone numbers mirror plan phases for traceability; § references point into the plan.

## Sequencing

M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7, with two allowed overlaps:

- Start M1 the moment its M0 blockers are done (Telegram account, legacy export, Convex/R2 provisioning). The raw archive is irreplaceable and Telegram-rate-clocked; everything downstream is reproducible (§7).
- M2 batches may start on partial archive data once the config is pinned and the GPU benchmark is done; M2 exit still requires M1 complete.

Every batch command in every milestone obeys §4: R2-artifact-first write order, skip on `done`, crash-window recovery, stage locks + run history, failures drained first, `approved` lessons never recomposed.

---

## M0 — Foundations, gates, legacy export (Phase 0, ~½–1 day)

**Goal:** every irreversible decision pinned, every external dependency proven, v1 data safe.
**Exit:** `configHash` computed and logged; codec decision recorded; GPU benchmark numbers recorded and M2 schedule derived; legacy export verified in R2; transcribe doctor passes; Convex/R2/Meilisearch reachable with scoped keys.

- [x] Telegram access — **decided: archiving runs on the personal account `@haithamassoli`, not a dedicated one.** The §9 mitigation for "Telegram limits/ban" is therefore not in place: a ban during M1 would cost the owner's own account rather than a throwaway. The rest of that mitigation still applies (takeout, pacing, resumable checkpoints), and the archive stays resumable either way. Session at `secrets/archive.session` (mode 600, gitignored), created by `archive telegram-login`; the gate asks Telegram whether it is really signed in, because Telethon writes a complete-looking session file during the key exchange, before it prompts for a phone.
- [x] Accept HF model terms; set `HF_TOKEN`/`HF_HOME` — verified by the `model-access` gate (pinned model+revision readable with the token)
- [x] Pin transcription config → `src/archive/config.py`; `configHash=d27d1fb0a633fb8273f793655bd8ef82e6100da90f63340d1f9bb16c609bc4d5`, recorded in `m0.gates.json`; drift from the pin or from the package default fails the `config-pin` gate (§0.5)
- [x] Benchmark the actual GPU: **Apple MPS (M-series), RTF 23.5x, 4.14 GB resident, 0.29 files per ASR batch** on 25.9 min of real archive audio (a 4.5-min voice note + a 21-min lesson) — `archive bench` wrote it to `m0.gates.json`. Projected M2 runtime for the archive's 2,926.6 audio hours: **124.3 h ≈ 5.2 days of continuous GPU**, so M2 is a background campaign, not a sitting. Two caveats recorded with the numbers: `filesPerBatch` is below 1 because a long lesson spans several ASR batches (it is files ÷ batches, not a batch size), and MPS has no peak-memory counter, so the 4.14 GB is a live driver reading taken while the model is resident — the fix that made it meaningful was sampling it before `close()` instead of after.
- [ ] Codec gate: Opus vs AAC seek/Range behavior on iOS Safari, Android, desktop — record `decision` + `testedOn` in `m0.gates.json`
- [x] Legacy export first (§8.1) — 3387 objects / 39.8 MB under `legacy/assoli-v1/`: all 3379 `articles/` (posts, fatwas, books, tg) plus `videos.json`, `playlists.json`, `eval-questions*.json` (the §8.4 relevance set), `eval-articles.json`, `domain-synonyms.json`, wayback logs. Verified against the manifest. **YouTube transcripts deliberately excluded** — `segments/` (177 MB) and `tafrigh/output/` (1.7 GB) stay on local disk only; `meili/` (10 GB) is a rebuildable index. v1 untouched.
- [x] Provision Convex — schema (§2) + the four atomic mutations (plus lock heartbeat/release) deployed to `haitham-assoli:alkulify` dev (`friendly-cheetah-400`); uniqueness, lock hand-off, attempt counting and merge invalidation exercised against the live deployment
- [x] Provision R2 — both buckets reachable with a real account token. Note: the token is scoped to *both* buckets rather than one key pair per bucket; `lessons-media` gets its first write in M5, split the keys before then. Custom domain still to do (M5).
- [ ] Provision Meilisearch: instance, admin key, search-only key
- [x] Repo skeleton: Python project (`src/archive/`), config module holding the pinned config, `.gitignore` covering `.env`, `*.session`, `secrets/`, temp dirs; `tests/test_m0.py`

`archive gates` is the live tracker for this milestone — it exits non-zero until every gate passes.

**Decisions to close (§10 — must not block M1):**
- [ ] GPU location (desktop vs server) — shapes M6 timers only
- [x] M1 download scope — resolved on the live account, counts as of 2026-09-01:
  | channel | msgs | range | title |
  |---|---|---|---|
  | `@doros_alkulify` | 11,899 | 2017-09-08 → 2026-09-01 | المواد الصوتية / عبد الله الخليفي |
  | `@alkulife` | 14,787 | 2017-05-10 → 2026-08-31 | قناة \| أبي جعفر عبدالله الخليفي |
  | `@T_alkulife` | 904 | 2022-09-10 → 2026-03-31 | تدبرات قرآنية |
  | `@alkulifyfgh` | 16 | 2024-10-14 → 2026-03-16 | الفقه سؤال وجواب |
  | `@KulifyAntiCapitalism` | 9 | 2022-12-25 → 2025-10-02 | نقد الرأسمالية / الخليفي |

  **Decided: M1 downloads `@alkulife` and `@doros_alkulify` only** (26,686 messages). The other three are deliberately out of scope for now; adding them later costs a second Telegram-clocked pass over ~929 messages, which is cheap.
- [ ] assoli-v1 salvage audit: any manually corrected transcripts? accounts/bookmarks/analytics worth exporting?

---

## M1 — Archive everything (Phase 1, Telegram-rate-clocked — start immediately)

**Goal:** the complete raw archive — every message and every unique binary — durable in Convex + R2. Highest-value milestone in the project (§7).
**Exit:** all in-scope channels fully synced; per-channel counts match channel stats; 20 random `telegramUrl` spot-checks pass; kill-and-resume proven clean.

- [x] Takeout session bootstrap: takeout id persisted in the session (`finalize=False`, so a resumed run continues the same export); `TakeoutInitDelay` reported with the wait; flood waits slept through (`flood_sleep_threshold = 24h`); chronological iteration; checkpoint per channel in `channels.lastMessageId`. `--reset-takeout` closes a stored id, `--no-takeout` falls back to the normal session. **Telegram currently answers the takeout request with a 24 h delay — it has to be approved in the Telegram app before the paced run can start.**
- [x] Message ingest: `upsertTelegramMessage` (uniqueness on `(channelId, telegramMessageId)`, `mediaType` set here, `semanticType` left null and never clobbered on re-ingest) + raw dict appended to `meta/{channel}/{from:08d}-{to:08d}.jsonl.zst` + `meta/{channel}/manifest.json` (schemaVersion, range, count, createdAt, per-batch sha256)
- [x] Capture forward info, `grouped_id` (as a string — it is an int64), `replyToMessageId`, edit dates
- [x] Media pipeline per file: download → sha256 → existing-row lookup (a repost reuses that row's `r2Key`, so identical bytes can never land under two keys) → ffprobe (durationMs, codec, sampleRate, channelCount) → upload `blobs/{sha256[:2]}/{sha256}.{ext}` → `getOrCreateMediaObject` + `linkMessageMedia` → delete temp. R2 before Convex (§4.1)
- [x] §4.5 batch harness (built here, reused by every later stage): local `flock` + `acquirePipelineStage`, 60 s heartbeat, `pipelineRuns` row with counts on every exit path. §4.6 failure drain: a failed message goes to `failures`, and the next run retries it before new work, capped at 5 attempts
- [x] Kill-safety test: proven twice. Against fakes — `tests/test_m1.py` kills mid-channel and mid-file and asserts no duplicate rows, no gaps, no orphan temp files. Against the live channel — `SIGKILL` mid-download of message 18 left a 19 MB partial; the second run was correctly refused by the Convex stage lock, and after the 5-minute stale window the recovered run wiped the temp dir, skipped messages 14–16 with zero re-downloads, and re-fetched only 18
- [x] Validation pass: `archive verify-archive` — archived count vs the channel's own total, id-gap count, meta-batch presence in R2, and N random `telegramUrl` spot-checks re-fetched from live Telegram (url, date, mediaType, text). 17/17 clean on the synced slice
- [x] **Run it to completion.** Both channels archived in full: `alkulife` 14,801/14,801 and `doros_alkulify` 11,922/11,922 — 26,723 messages, 11,460 unique binaries, 117.3 GB in R2, 62 meta batches. `archive verify-archive` exits 0: counts match each channel's own total, every manifest batch is present in R2, 40/40 live `telegramUrl` spot-checks clean, no message claiming a binary that was never stored, no unresolved ingest failure. Three defects were found and fixed during the run — a Telethon 1.44.0 session-row column swap that corrupted the session on the first takeout, file references expiring mid-batch, and Ctrl-C being swallowed by asyncio — plus `verify-archive` itself, which counted a message as archived on its declared `mediaType` rather than on the binary actually being linked.

## M2 — Transcribe all parts (Phase 2, GPU-clocked per M0 benchmark)

**Goal:** a `done` part transcript for effectively every unique audio binary under the pinned config.
**Exit:** ≥99% of unique audio sha256s have `partTranscripts.status = done` for the active `configHash`.

- [x] Batch-command harness (reused by all later stages): built in M1 (`src/archive/pipeline.py`) and reused unchanged — local `flock` + `acquirePipelineStage`, 60 s heartbeat, `pipelineRuns` row on every exit path (§4.5). Proven again here: a Ctrl-C 10 minutes into a live run released the lock, wrote a `pipelineRuns` row with `status=interrupted` and 9,790 processed, and left no scratch behind
- [x] Failures-first drain: `archive transcribe` reads `queries:unresolvedFailures` for the stage before selecting, sorts those sha256s to the front, and skips any past the 5-attempt cap (§4.6). A repaired sha256 also has its failure resolved — in the worker and in `reconcile-artifacts`, so the ops page never keeps a stale entry
- [x] Selection: `queries:mediaObjectsPage` walks `mediaObjects` in sha256 order and joins each row's part transcript for the active `configHash`, so the §4.2 fast path costs no extra round trip. `done` is skipped outright; pending/failed/missing and `processing` older than 5 min are candidates. Recovery (§4.3) computes the deterministic key, checks it against one `LIST` of `transcripts/` (not 9,793 HEADs), validates the artifact and promotes it to `done` without inference — verified live in 7 s. `existing="skip"` stays on as the same-machine belt
- [x] Materialize needed blobs locally as `{sha256}.{ext}` (plain copy from R2 into `.tmp/asr/`, deleted per file after upload; the whole scratch dir is wiped at the start and end of every run)
- [x] Persistent `Transcriber` (`language="ar"`, `vad_merge=True`, JSON out) built once per run and reused across batches, loaded lazily — a run with nothing to do never loads the 2B model. Batches of ~500 under the stage lock
- [x] Per file: upload `transcripts/{sha256}/{configHash}.json` → `upsertPartTranscript` done (§4.1). `validate_artifact` checks all six pinned fields against the artifact's own provenance (model, revision, language, vad, merge, timing); a mismatch is a failure, never a new identity
- [x] `reconcile-artifacts` command, part-transcript mode: A) Convex done + R2 missing → marked `failed` + a `failures` row, B) R2 exists + not done → validated and repaired to `done`, C) orphan and superseded-`configHash` objects reported, never deleted (§4.4). All three verified against the live deployment by deleting and restoring a real artifact
- [ ] **Run it to completion.** The implementation is done, tested and verified end to end on real blobs (3 binaries: R2 artifact → Convex `done`, plus a live crash-window recovery and a live reconcile in both directions). The campaign itself has not been run: 9,793 unique audio binaries / 2,926.6 hours is ~124 h of GPU at the measured 23.5x, and it needs to be launched deliberately on the GPU host (`uv sync --extra gpu && archive transcribe`). Current coverage: 3/9,793. 500 rows are `processing` from the interrupted trial run and are claimable again after their 5-minute stale window
- [ ] Scope note surfaced by the first full scan: 9,793 of the 11,460 unique binaries are audio; the other 1,667 are 1,212 images, 338 PDFs, 94 videos (6.2 h — `video/mp4` and one `3gpp`), 18 PNGs and 4 DOCX. The plan scopes M2 to "unique audio sha256s", so the videos are excluded and reported rather than silently dropped. **Decide before M3 whether those 6.2 h of video get their audio transcribed too.**

---

## M3 — Organize + lesson transcripts (Phases 3 + 3.5)

**Goal:** messages classified, articles extracted, lessons composed with stable identity, lesson-transcript artifacts built. Organizer is pure (reads Convex only) and fully re-runnable.
**Exit:** a full Organizer re-run on unchanged data is a no-op; every lesson has a lesson-transcript artifact matching its current `assemblyHash` + active `configHash`; §8.2 coverage diff resolved.

- [ ] Classifier v1 → `semanticType` + `classifierVersion`; forwarded audio flagged and excluded from sheikh-voice lessons by default
- [ ] Article extraction → `articles` rows with `normalizedTitle`, `titleSource`
- [ ] Series/title parser: all fields incl. `normalizedSeriesName`, parser version, confidence; test corpus of ≥50 real titles per channel per era
- [ ] Grouping v1: grouped_id → title-then-consecutive-audio → replies → YouTube-link confirmation → time proximity as support; outputs grouping version, confidence, `reviewStatus` auto/needs_review
- [ ] Compose: `upsertLessonByKey`; `assemblyHash` = canonical JSON of the ordered part sha256s only (§0.3); `lessonParts` with offsets accumulated from `mediaObjects.durationMs`
- [ ] `lessonSources`: telegram primary row + youtube mirror row from trailing YouTube-link messages / v1 YT↔TG map
- [ ] Enforce §0 amendment 1 / §4.7: reruns never touch `approved` lessons; a source-message edit/delete demotes `approved` → `needs_review`, never silent recompose
- [ ] §8.2 coverage diff vs v1: YouTube-only items → `legacy`/`youtube` lessons via `lessonSources`; empty diff → delete v1 YouTube transcripts with a clear conscience
- [ ] Lesson Transcript Builder (Phase 3.5): concat part JSONs + offsets → `lesson-transcripts/{lessonId}/{assemblyHash}-{configHash}.json` (organizerVersion recorded inside) → set pointer; regenerates automatically whenever `assemblyHash` changes

---

## M4 — Search v0 beta (Phase 4, runs alongside live v1)

**Goal:** searchable articles + audio chunks behind a usable RTL UI.
**Exit:** v1 query-log replay against both v1 and v2 produces a recorded relevance comparison (the regression set for M7).

- [ ] Chunk builder: 45–90 s chunks from lesson-transcript artifacts; deterministic IDs; part-N tails always joined with part-N+1 heads (§5); part-level fallback for ungrouped audio
- [ ] Meilisearch `articles` + `audio_chunks`: searchable `normalizedTitle`/`normalizedText`; displayed `title`/`text`; filters `channel`, `seriesName`, `lessonId`; sort `date`
- [ ] Full `reindex` from scratch proven; `indexedAt`/`indexVersion` stamped
- [ ] Import v1 manual corrections as transcript overrides (§8.3) — the only data no GPU can reproduce
- [ ] UI: RTL, articles/audio tabs, excerpts, series facets, Telegram links
- [ ] Raw-part playback via short-lived signed R2 URLs (Range OK; refresh on 403); raw bucket stays private forever
- [ ] Relevance baseline: replay v1 query logs against v1 and v2; record results (§8.4)

---

## M5 — Merged playback (Phase 5)

**Goal:** one continuous public audio file per lesson, seekable and deep-linkable.
**Exit:** merged audio exists for all transcribed multi-part lessons; playback and seek verified on the gated codec across the M0 device matrix.

- [ ] Merge worker: concat-demuxer fast path, re-encode fallback to the M0-gated codec; `mergeStatus` lifecycle; write-order law
- [ ] Upload `lessons/{lessonId}/{mergedSha256}.{ext}` to the public bucket; warn when Σ part durations vs merged duration differs by >500 ms
- [ ] Extend `reconcile-artifacts` to merged audio
- [ ] Player: merged playback, seek `max(0, startMs − 2000)`, `?t=` links, autoplay handling
- [ ] Chunk re-attribution to the merged timeline = pure reindex

**Cutover (gated by §8.5 — only when coverage ≥ v1 AND log-replay equal-or-better):**
- [ ] Run the parity check (coverage diff + query-log replay); record go/no-go
- [ ] On go: switch domain, add redirects, sheikh announcement; v1 stays live until then

---

## M6 — Review UI & continuous sync (Phase 6)

**Goal:** the archive stays current without manual runs; humans can fix grouping without fighting the pipeline.
**Exit:** timers running unattended for a week; one approve → regen → reindex cycle verified end-to-end; an edited source message demotes its approved lesson.

- [ ] Review UI: `needs_review` queue ordered by confidence; preview, reorder, add/remove parts, split, merge, rename
- [ ] Approve flow: approve → new `assemblyHash` → lesson-scoped regen (lesson transcript + remerge + reindex)
- [ ] Incremental sync on a normal session: messages > `lastMessageId` + ~300-message recheck for edits/`deletedAt`; affected `approved` lessons demote to `needs_review` (§4.7), never recompose
- [ ] systemd timers under §4.5 locks: sync every 15 min → transcribe-pending → index-pending; merge nightly

---

## M7 — Hardening & relevance (Phase 7)

**Goal:** boring operations — failures visible, recovery drilled, search tuned on real queries.
**Exit:** recovery drills pass (§4.3 crash window, reindex-from-scratch); ops page live; relevance changes measured against the §8.4 regression set.

- [ ] Ops page: stage counts, unresolved `failures`, `pipelineLocks` state, `pipelineRuns` history (last run, duration, counts), per-channel sync status
- [ ] Schedule `reconcile-artifacts` weekly
- [ ] Retry wrappers on network/API calls; temp cleanup
- [ ] Security: rate-limit the signed-URL endpoint; least-privilege key audit; search-only Meilisearch key client-side
- [ ] Relevance tuning from v1 logs: hamza/diacritics variants, Arabic typo tolerance, chunk duration, title weight; then decide whether raw `text` joins the searchable fields
- [ ] Recovery drills: kill inside the §4.3 crash window and recover; reindex from scratch; restore-from-R2 walkthrough
