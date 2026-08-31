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
```

`.env` holds every secret; it is gitignored, as is the Telegram `.session` file.

## Commands

```sh
archive config-hash                          # pinned config + its configHash
archive gates                                # every M0 exit gate; exit 1 until all pass
archive legacy-export <dir>                  # assoli-v1 export dir -> R2 legacy/assoli-v1/
archive bench <audio...> --archive-hours 900 # GPU benchmark -> m0.gates.json
```

## The pinned config (§0.5)

`src/archive/config.py` holds the six fields that define transcript identity —
model, resolved `modelRevision` commit, language, vad, vadMerge, alignment.
Their canonical JSON hashes to the `configHash` that keys every part transcript
in R2 and Convex. **Changing any of them re-transcribes the whole archive.**
`archive gates` fails if the pin drifts from the value recorded in
`m0.gates.json`, or from cohere-transcribe's own default revision.

## Convex

```sh
npx convex dev      # creates the deployment, generates convex/_generated, deploys schema
npm run typecheck   # tsc over convex/
```

Convex indexes are not `UNIQUE` constraints. Uniqueness on `sha256`,
`(sha256, configHash)`, `lessonKey` and `stage` is enforced only by the atomic
mutations in `convex/mutations.ts` — always write through those.
