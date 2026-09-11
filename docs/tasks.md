# Telegram Knowledge Archive — Milestones & Tasks

Derived from `docs/telegram_archive_plan.md` (v2.4, FROZEN). Milestone numbers mirror plan phases for traceability; § references point into the plan.

## Sequencing

M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7, with two allowed overlaps:

- Start M1 the moment its M0 blockers are done (Telegram account, legacy export, Convex/R2 provisioning). The raw archive is irreplaceable and Telegram-rate-clocked; everything downstream is reproducible (§7).
- M2 batches may start on partial archive data once the config is pinned and the GPU benchmark is done; M2 exit still requires M1 complete.

Every batch command in every milestone obeys §4: R2-artifact-first write order, skip on `done`, crash-window recovery, stage locks + run history, failures drained first, `approved` lessons never recomposed.

---

## M0 — Foundations, gates, legacy export (Phase 0, ~½–1 day)

**Goal:** every irreversible decision pinned, every external dependency proven, v1 data safe.
**Exit:** `configHash` computed and logged; playback gate recorded; GPU benchmark numbers recorded and M2 schedule derived; legacy export verified in R2; transcribe doctor passes; Convex/R2/Meilisearch reachable with scoped keys.

- [x] Telegram access — **decided: archiving runs on the personal account `@haithamassoli`, not a dedicated one.** The §9 mitigation for "Telegram limits/ban" is therefore not in place: a ban during M1 would cost the owner's own account rather than a throwaway. The rest of that mitigation still applies (takeout, pacing, resumable checkpoints), and the archive stays resumable either way. Session at `secrets/archive.session` (mode 600, gitignored), created by `archive telegram-login`; the gate asks Telegram whether it is really signed in, because Telethon writes a complete-looking session file during the key exchange, before it prompts for a phone.
- [x] Accept HF model terms; set `HF_TOKEN`/`HF_HOME` — verified by the `model-access` gate (pinned model+revision readable with the token)
- [x] Pin transcription config → `src/archive/config.py`; `configHash=d27d1fb0a633fb8273f793655bd8ef82e6100da90f63340d1f9bb16c609bc4d5`, recorded in `m0.gates.json`; drift from the pin or from the package default fails the `config-pin` gate (§0.5)
- [x] Benchmark the actual GPU: **Apple MPS (M-series), RTF 23.5x, 4.14 GB resident, 0.29 files per ASR batch** on 25.9 min of real archive audio (a 4.5-min voice note + a 21-min lesson) — `archive bench` wrote it to `m0.gates.json`. Projected M2 runtime for the archive's 2,926.6 audio hours: **124.3 h ≈ 5.2 days of continuous GPU**, so M2 is a background campaign, not a sitting. Two caveats recorded with the numbers: `filesPerBatch` is below 1 because a long lesson spans several ASR batches (it is files ÷ batches, not a batch size), and MPS has no peak-memory counter, so the 4.14 GB is a live driver reading taken while the model is resident — the fix that made it meaningful was sampling it before `close()` instead of after.
- [ ] Playback gate (v2.4 — was the Opus/AAC codec gate): nothing is re-encoded any more, so the question is whether the archive's **own** containers seek and honour Range on iOS Safari, Android, desktop. Test the real extensions in `mediaObjects.ext`, not a hypothetical output codec — record `decision` + `testedOn` in `m0.gates.json`
- [x] Legacy export first (§8.1) — 3387 objects / 39.8 MB under `legacy/assoli-v1/`: all 3379 `articles/` (posts, fatwas, books, tg) plus `videos.json`, `playlists.json`, `eval-questions*.json` (the §8.4 relevance set), `eval-articles.json`, `domain-synonyms.json`, wayback logs. Verified against the manifest. **YouTube transcripts deliberately excluded** — `segments/` (177 MB) and `tafrigh/output/` (1.7 GB) stay on local disk only; `meili/` (10 GB) is a rebuildable index. v1 untouched.
- [x] Provision Convex — schema (§2) + the four atomic mutations (plus lock heartbeat/release) deployed to `haitham-assoli:alkulify` dev (`friendly-cheetah-400`); uniqueness, lock hand-off, attempt counting and merge invalidation exercised against the live deployment
- [x] Provision R2 — both buckets reachable with a real account token. **v2.4: `lessons-media` is dropped** along with merging, so the key split and the custom domain it needed are cancelled. One private bucket; audio reaches listeners through signed URLs (plan §5). The over-scoped token should still be narrowed to the archive bucket alone.
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
- [x] **Run it to completion.** 9,793 of 9,793 unique audio binaries `done` — 100.00%, 2,926.6 audio hours, against a ≥99% exit criterion. Run on two Kaggle T4s in one session under `--shard i/n`, at a measured 172x with both cards live (86x per card at fp16). Four defects were found and fixed during the campaign: `dtype="auto"` resolving to *emulated* bf16 on a T4 because `is_bf16_supported(including_emulation=True)` answers yes to a mere allocation check; a poison-file wedge where a binary that killed the process never reached `_fail`, so §4.6's cap never fired and the deterministic selector re-picked the same batch forever (fixed by reading `partTranscripts.attempts`, which is incremented at claim time and therefore survives a hard crash); a supervisor that treated a `--limit`-bounded probe run's clean exit as "shard finished" and silently lost a GPU for two hours; and an unsupervised final sweep whose first crash ended the session. The last 20 binaries crashed every T4 attempt with heap corruption and transcribed on the Mac's MPS path in a single pass, so they were a CUDA-allocator failure, not damaged audio. `reconcile-artifacts` reports zero drift: no missing artifact, none invalid, none orphaned
- [ ] Scope note surfaced by the first full scan: 9,793 of the 11,460 unique binaries are audio; the other 1,667 are 1,212 images, 338 PDFs, 94 videos (6.2 h — `video/mp4` and one `3gpp`), 18 PNGs and 4 DOCX. The plan scopes M2 to "unique audio sha256s", so the videos are excluded and reported rather than silently dropped. **Decide before M3 whether those 6.2 h of video get their audio transcribed too.**

---

## M3 — Organize + lesson transcripts (Phases 3 + 3.5)

**Goal:** messages classified, articles extracted, lessons composed with stable identity, lesson-transcript artifacts built. Organizer is pure (reads Convex only) and fully re-runnable.
**Exit:** a full Organizer re-run on unchanged data is a no-op; every lesson has a lesson-transcript artifact matching its current `assemblyHash` + active `configHash`; §8.2 coverage diff resolved.

- [x] Classifier v1 → `semanticType` + `classifierVersion` (`organize.classify`, version `classify-v1+norm-v1`). Ordered tests, each threshold measured on the corpus first: URL-only → `link` (4,220 of them — @doros_alkulify's republication stream), announcement/live-stream/promo/postponement patterns → `notice` (454), short text with audio right behind it → `lesson_title` (3,504), ≥500 chars with no media or a photo → `article` (5,350), everything else `other`. Forwarded audio is excluded from lessons entirely, not merely flagged: 642 of @alkulife's forwards are its own reposts and would otherwise compose a second lesson over the same binaries
- [x] Article extraction → 4,455 `articles` rows with `normalizedTitle`, `titleSource`. Photo+caption articles are included (666 of them; recent-era articles increasingly carry a screenshot), forwards are excluded as quoted third-party material, and Telegram's 4,096-char cap is stitched back together — 145 of the 200 @alkulife messages at or above 4,000 chars are continued by the next text message, so treating those as separate articles would cut 145 articles in half. Title = first line when it is ≤80 chars (65% of them), else an 80-char prefix
- [x] Series/title parser (`title-v1+norm-v1`): `SERIES (N)`, `SERIES [ ٦٥١ ]`, `SUBTOPIC (n) من SOURCE (N)` — where the durable series is the source book, so «الصلاة (22) من المغني (42)» is episode 42 of المغني — bare trailing numbers as filenames write them («الجامع 8»), and `المجلس` + ordinal. 1,486 lessons parse to a numbered episode; top series الدارمي×198, المغني×157, سيرة ابن هشام×72, الجامع×64. **`الحلقة N`, `الجزء الثاني` and `ج2` are deliberately not implemented: 0 occurrences in title position across all 26,723 messages.** Both digit alphabets are read, since one series uses each in the same month
- [x] Grouping v1 (`group-v1`): 5,630 lessons over 9,914 parts. **Two rules the plan named do not survive the data and the code says so where it replaces them.** `groupedId` is set on 9 of 14,801 @alkulife messages and 200 of 11,922 @doros_alkulify ones, most of them photo albums — under 1% coverage, so it is a hint, not the primary rule. What actually separates lessons is duration: a recording of ≥20 minutes is a whole sitting (@doros_alkulify's median lesson is 2,626 s), a shorter one posted within 600 s of the last is a part (@alkulife's median part is 319 s, and 82.3% of its within-run gaps land in 300-600 s against a between-run median of 30 hours). `تتمة` continuation replies join their parent (142 of them, the one reply edge worth trusting: audio→text replies number 3 in the whole archive). Confidence follows the title source — 0.9 with a `👇` title message, 0.7 without, 0.5 from a filename, 0.2 untitled — and a weaker source is forgiven when the title numbers itself. 1,625 lessons land in `needs_review`
- [x] Grouping v2 (`group-v2`): 5,461 lessons over the same 9,914 parts, and 169 rows retired — the v1 gap was measured on the *median* pause and cut the tail off 162 @alkulife runs, leaving half of each run behind as an untitled lesson (186 untitled on that channel, 172 of them with their title one message further back). A pause is not a new lesson, so a **bare** part — no caption, no filename, nothing that could name it alone — now joins its run up to `RUN_GAP_S` (6 h: the measured tail of those gaps ends at 3.4 h, the between-run median is 30 h). A part that names itself still splits at 600 s, which is what keeps @doros_alkulify's re-upload batches apart: 33 of its lessons would otherwise have fused into 7. Result: untitled lessons 186 → 24 on @alkulife, 526 → 519 on @doros_alkulify (the rest carry no title anywhere in the channel), `needs_review` 1,625 → 1,456. A regrouping never moves a `lessonKey`, it merges one lesson into its neighbour, so the row left behind lists audio the neighbour now plays — `retire_superseded` + `mutations:retireSupersededLesson` tombstone it, and §4.7 outranks that: an approved or hand-edited row is reported and left for a human (0 of the 169 were)
- [x] Compose: `upsertLessonByKey`; `assemblyHash` = sha256 of the canonical JSON of the ordered part sha256s only (§0.3); `lessonParts` written as a replaced set, not per-row upserts, so a rerun that drops a part cannot orphan it; offsets accumulated from `mediaObjects.durationMs`
- [x] `lessonSources`: the Telegram message a lesson came from, and nothing else. The plan expected trailing YouTube links to confirm a grouping and the corpus said they cannot — of @doros_alkulify's 3,977 bare-link messages 1,417 sit between two other links and only 648 are followed by audio, because the links are a separate stream posted in batches hours to days later. The archive's owner then settled it outright: those uploads are the same recordings republished late, so they are not a second source of anything. The messages keep `semanticType = "link"`; nothing binds them to a lesson
- [x] §0 amendment 1 / §4.7 enforced on both sides: `upsertLessonByKey` refuses to recompose an `approved` lesson (`skipped: true`), and the Organizer demotes one to `needs_review` when its recomputed `assemblyHash` differs or a source message was deleted. ponytail: an edit that leaves the composition intact is not detected, because no row records when approval happened — record `approvedAt` in M6's review UI and this becomes a date comparison
- [ ] §8.2 coverage diff vs v1: YouTube-only items → `legacy`/`youtube` lessons via `lessonSources`; empty diff → delete v1 YouTube transcripts with a clear conscience
- [x] Lesson Transcript Builder (Phase 3.5): `lesson-transcripts/{lessonId}/{assemblyHash}-{configHash}.json` with `organizerVersion` inside, `lessonStartMs = part.offsetMs + segment.startMs` (§5), write-order law obeyed — artifact into R2, pointer into Convex second — and staleness decided by identity alone, so a regrouped lesson gets a new artifact without a flag anywhere
- [x] **Exit criterion proven.** A full Organizer re-run over unchanged data reported `0 write(s)`: 26,723 messages reclassified to the same values, 4,455 articles and 5,630 lessons compared field by field, nothing patched. 29 tests in `tests/test_m3.py`, including the rerun-is-a-no-op check, the approved-lesson freeze, the demote-on-recomposition path, and `test_fake_convex_matches_the_deployed_signatures`

---

## M4 — Search v0 beta (Phase 4, runs alongside live v1)

**Goal:** searchable articles + audio chunks behind a usable RTL UI.
**Exit:** v1 query-log replay against both v1 and v2 produces a recorded relevance comparison (the regression set for M7).

- [ ] Chunk builder: 45–90 s chunks from lesson-transcript artifacts; deterministic IDs `{lessonId}:{assemblyHash[:8]}:{seq}`; part-N tails always joined with part-N+1 heads (§5); part-level fallback for ungrouped audio
- [ ] Normalization contract `normVersion` v1 (plan Phase 4): one versioned function for index fields, query preprocessing, and the §8.4 replay — strip tashkeel/tatweel, unify alef/ta-marbuta/alef-maqsura, unify digits; raw text always kept for display
- [ ] Meilisearch `articles` + `audio_chunks`: searchable `normalizedTitle`/`normalizedText`; displayed `title`/`text`; filters `channel`, `seriesName`, `lessonId`, `date`; sort `date`; document shapes per plan Phase 4
- [ ] Initial index settings: synonyms seeded from `legacy/assoli-v1/domain-synonyms.json`; attribute weight title > text; `distinctAttribute = lessonId` on `audio_chunks`; typo tolerance on (tuning belongs to M7)
- [ ] Meilisearch deployment: self-hosted, version pinned in `.env`, on the dev machine for the beta; master key server-side only. No snapshots — recovery is reindex-from-scratch (drilled in M7)
- [ ] Full `reindex` from scratch proven; `indexedAt`/`indexVersion` stamped
- [ ] Import v1 manual corrections as transcript overrides (§8.3) — the only data no GPU can reproduce
- [ ] UI: RTL, articles/audio tabs, excerpts, series facets, Telegram links
- [ ] Part playback via short-lived signed R2 URLs (Range OK; refresh on 403); the bucket stays private forever. **v2.4: this is the permanent playback path, not an interim** — hardening moves to M5
- [ ] Relevance baseline: replay v1 query logs against v1 and v2; record results (§8.4)

## M4.5 — Hybrid semantic search (bge-m3 — gated, optional; plan Phase 4.5)

**Gate:** enter only if the M4 keyword replay shows recall failures that `normVersion` + synonyms cannot close; keep only if hybrid beats keyword on the same replay.

- [ ] Pin embed config → `embedHash` (model `BAAI/bge-m3`, resolved revision, dim 1024, normalize) — same drift-fails law as `configHash`
- [ ] Nightly embed batch on the GPU host: new/changed chunks → dense vectors on the same documents (`userProvided` embedder, `_vectors.default`)
- [ ] Query-side embed service (ONNX int8, CPU) beside Meilisearch; hybrid search with `semanticRatio` 0.3–0.5
- [ ] A/B on the §8.4 replay: hybrid vs keyword; record go/no-go. Sizing: ~4 GB RAM on the VPS or binary quantization

---

## M5 — Continuous multi-part playback (Phase 5)

**Goal:** a lesson plays as one continuous thing without ever becoming one file. A reader must not be able to tell how many parts it has.
**Exit:** on the M0 device matrix — seek across a part boundary, resume mid-part, and a lock screen showing the **lesson's** duration, not a part's.

*(v2.4: merging is removed. See plan §12 for what was deleted and what it cost.)*

- [ ] Player over the plan-§5 virtual timeline: locate the part where `offsetMs ≤ t < offsetMs + durationMs`, play at `(t − offsetMs)/1000`; seek `max(0, startMs − 2000)`; `?t=` deep links
- [ ] Boundary crossing: one `<audio>`, `src` swapped on `ended`. **Measure the gap before building more** — only if it is audible, add a second element unlocked inside the first user gesture (`play()` then immediate `pause()`), which iOS Safari requires before it will honour a programmatic `play()`
- [ ] Prefetch the next part at ~30 s remaining. Never prefetch the whole lesson — a ten-part lesson would burn a mobile reader's data on open
- [ ] `navigator.mediaSession.setPositionState()` fed the lesson duration/position. Skipping this leaks the part boundaries onto the lock screen, which is the one thing this milestone exists to hide
- [ ] Part boundaries visible in the review UI, hidden in the public player — same component, one prop
- [ ] Harden the signed-URL endpoint (moved up from M7): rate limit, 403 refresh mid-listen, Range verified. It is now the only path audio takes to a listener
- [ ] Confirm nothing in the search index changed: chunks were stamped against this timeline in M4

**Cutover (gated by §8.5 — only when coverage ≥ v1 AND log-replay equal-or-better):**
- [ ] Run the parity check (coverage diff + query-log replay); record go/no-go
- [ ] On go: switch domain, add redirects, sheikh announcement; v1 stays live until then

---

## M6 — Review UI & continuous sync (Phase 6)

**Goal:** the archive stays current without manual runs; humans can fix grouping without fighting the pipeline.
**Exit:** timers running unattended for a week; one approve → regen → reindex cycle verified end-to-end; an edited source message demotes its approved lesson.

- [ ] Review UI: `needs_review` queue ordered by confidence; preview, reorder, add/remove parts, split, merge, rename
- [ ] Approve flow: approve → recompute `lessonParts.order` then `offsetMs` cumulatively then `assemblyHash` → lesson-scoped regen (lesson transcript + reindex)
- [ ] Excluding a part must survive the next Organizer run. Today the only thing protecting an edit is §4.7 freezing `approved` lessons, so an exclusion on an `auto` lesson comes back on the next pass — either approve at exclusion time, or add a flag. Decide when the review UI is built, not before
- [ ] Incremental sync on a normal session (no takeout): messages > `lastMessageId` + ~300-message recheck for edits/`deletedAt`; affected `approved` lessons demote to `needs_review` (§4.7), never recompose
- [ ] Timers under §4.5 locks — one dumb wrapper script per cadence chaining the existing self-locking commands: every 15 min sync → transcribe-pending → organize → index-pending; nightly M4.5 embed batch if adopted (nothing otherwise — the nightly merge is gone); weekly reconcile + Convex export
- [ ] Placement per plan Phase 6: launchd LaunchAgents on the Mac (the GPU host) now; move to systemd on the VPS at cutover. Stage split across VPS + Mac is legal under the existing locks; CPU transcription on the VPS is legal because transcript identity is the pinned config, not the device
- [ ] Failure push: end of each wrapper run, non-ok `pipelineRuns` or new unresolved `failures` → one Telegram message via a send-only bot token (~10 lines; the bot never reads channels)
- [ ] Human-decision backup: weekly `npx convex export` → `backups/convex/{date}.zip` in the private bucket — approvals/corrections are the one thing no GPU can regenerate

---

## M7 — Hardening & relevance (Phase 7)

**Goal:** boring operations — failures visible, recovery drilled, search tuned on real queries.
**Exit:** recovery drills pass (§4.3 crash window, reindex-from-scratch); ops page live; relevance changes measured against the §8.4 regression set.

- [ ] Ops page: stage counts, unresolved `failures`, `pipelineLocks` state, `pipelineRuns` history (last run, duration, counts), per-channel sync status
- [ ] Schedule `reconcile-artifacts` weekly
- [ ] Retry wrappers on network/API calls; temp cleanup
- [ ] Security: least-privilege key audit; search-only Meilisearch key client-side. (Signed-URL rate limiting moved to M5 — v2.4 made that endpoint load-bearing)
- [ ] Relevance tuning from v1 logs: hamza/diacritics variants (via `normVersion`), Arabic typo tolerance (`minWordSizeForTypos`), chunk duration, title weight, `distinctAttribute` revisit, hybrid `semanticRatio` if M4.5 was adopted; then decide whether raw `text` joins the searchable fields
- [ ] Recovery drills: kill inside the §4.3 crash window and recover; reindex from scratch; restore-from-R2 walkthrough; Convex snapshot restore walked through once
