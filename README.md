# Telegram Knowledge Archive

Implementation of `docs/telegram_archive_plan.md` (v2.2, FROZEN).
Milestones and their exit criteria live in `docs/tasks.md`.

## Layout

```
src/archive/        pipeline code (M0: pinned config, gates, legacy export)
convex/             schema (§2) + the four atomic uniqueness mutations
m0.gates.json       recorded M0 decisions: configHash, codec, GPU benchmark
tests/test_m0.py    `python tests/test_m0.py` — no framework needed
cohere-transcribe/  vendored ASR package (M2)
```

## Setup

```sh
uv sync                       # python deps
npm install                   # convex + typescript
cp .env.example .env          # then fill in the real values
npx convex dev                # writes .env.local, deploys the schema
```

`.env` holds every secret and `.env.local` holds the Convex deployment the CLI
selected; both are gitignored, as is the Telegram `.session` file. The Python
code reads `.env.local` first, so it always talks to the deployment `npx convex`
is pointed at.

## Commands

```sh
archive config-hash                          # pinned config + its configHash
archive gates                                # every M0 exit gate; exit 1 until all pass
archive legacy-export <dir>                  # assoli-v1 export dir -> R2 legacy/assoli-v1/
archive telegram-login                       # interactive first login (needs a real terminal)
archive bench <audio...> --archive-hours 900 # GPU benchmark -> m0.gates.json
archive sync                                 # M1: archive the in-scope channels
archive verify-archive                       # M1 exit criteria; exit 1 until met
archive transcribe                           # M2: every audio binary -> a done part transcript
archive reconcile-artifacts                  # §4.4: make Convex and R2 agree
```

## M1 — the archive (`archive sync`)

Resumable by construction. `channels.lastMessageId` is a high-water mark advanced
only after that batch's `.jsonl.zst` is in R2, so a kill costs at most one batch
of replay — and replayed messages that are already complete cost one Convex read
instead of a re-download. Binaries are keyed by sha256, so a repost adds a
`messageMedia` row and nothing else.

```sh
archive sync                          # every channel in config.M1_CHANNELS
archive sync alkulife --limit 500     # one channel, bounded
archive sync --no-takeout             # normal session instead of a takeout export
archive sync --reset-takeout          # close the takeout id stored in the session
```

Telegram grants a takeout export higher limits, but only after the request is
approved in the Telegram app — until then it answers with a 24-hour delay and
`archive sync` says so. One run per stage at a time: a local `flock` plus the
`pipelineLocks` row, whose heartbeat has to go stale (5 min) before another
machine can take over.

## M2 — the transcripts (`archive transcribe`)

The GPU is the clock, so the whole design is about never spending it twice. Each
run scans every unique binary once, and a file reaches the model only after two
cheaper gates say it must: a `done` Convex row for the active `configHash` is
skipped outright (§4.2), and a row that is not done but whose deterministic
artifact key already exists in R2 is fetched, validated against the pinned
config, and promoted to `done` without inference (§4.3).

```sh
uv sync --extra gpu                   # torch + cohere-transcribe; the GPU host only
archive transcribe                    # everything still pending, in batches of 500
archive transcribe --limit 20         # bounded run
archive transcribe --batch-size 100   # smaller batches = less scratch disk
archive transcribe --sha256 <sha>     # one binary
archive reconcile-artifacts --dry-run # what Convex and R2 disagree about
```

A transcript is written to `transcripts/{sha256}/{configHash}.json` **before**
its Convex row flips to `done` (§4.1), so a `done` row always has its artifact
behind it. The artifact records its own model, revision, language, vad, merge
and timing; `validate_artifact` compares all six against the pin, and a mismatch
is a failure rather than a new identity — nothing another config produced ever
gets filed under this one's key. Video containers (94 objects, 6.2 h) are out of
scope by the plan's wording and are reported, not silently dropped.

## The pinned config (§0.5)

`src/archive/config.py` holds the six fields that define transcript identity —
model, resolved `modelRevision` commit, language, vad, vadMerge, alignment.
Their canonical JSON hashes to the `configHash` that keys every part transcript
in R2 and Convex. **Changing any of them re-transcribes the whole archive.**
`archive gates` fails if the pin drifts from the value recorded in
`m0.gates.json`, or from cohere-transcribe's own default revision.

## Convex

Deployed to the `alkulify` project's dev deployment.

```sh
npx convex dev      # generates convex/_generated, deploys schema + mutations
npm run typecheck   # tsc over convex/
npx convex dashboard
```

Convex indexes are not `UNIQUE` constraints. Uniqueness on `sha256`,
`(sha256, configHash)`, `lessonKey` and `stage` is enforced only by the atomic
mutations in `convex/mutations.ts` — always write through those.
