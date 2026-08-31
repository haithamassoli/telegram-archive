# Apple Silicon validation corpus (T1.5)

Generated 2026-08-31 by `scripts/build_validation_corpus.py`.
8 clips (4 Arabic `ar_eg`, 4 English `en_us`), 78.16 s total, all from the
FLEURS `dev` split at revision `70bb2e84b976b7e960aa89f1c648e09c59f894dd`.
Every clip is already 16 kHz mono WAV (32-bit float) in the source tarball and is
copied out verbatim — no resampling, no requantization, so the hashes are stable.
Each clip has a one-line UTF-8 reference transcript (`<stem>.txt`, the FLEURS
`raw_transcription` column) for later WER/CER.

**Audio lives outside the repository**, under the output directory you pass to the
script. Only this manifest is versioned.

## Evidence limitation

Local FLEURS subset assembled for T1.5. It is the substitute for the balanced 500-file baseline of docs/performance.md:48, whose file selection is not stored in this repository (T1.6). It supports self-consistency only — MPS output compared against CPU FP32 output on the same files — and does NOT demonstrate parity with the released CUDA baseline. No such parity may be claimed.

## Files

| id | lang | sec | config/split | tsv row | original filename | sha256 wav | sha256 txt |
|---|---|---|---|---|---|---|---|
| `ar_01` | ar | 12.66 | ar_eg/dev | 200 | `10013199422658976283.wav` | `7cfcdce2750b` | `e1d6cefae02a` |
| `ar_02` | ar | 10.92 | ar_eg/dev | 270 | `10138255891586291976.wav` | `2aa0b4497679` | `b23d996e24fd` |
| `ar_03` | ar | 11.56 | ar_eg/dev | 189 | `10244002567270267663.wav` | `c5a645e25423` | `448b400b79c5` |
| `ar_04` | ar | 8.04 | ar_eg/dev | 207 | `10267575737816936473.wav` | `443c0b84391d` | `224a70800749` |
| `en_01` | en | 6.54 | en_us/dev | 41 | `10010138729160973689.wav` | `048169439c03` | `b7bb91272649` |
| `en_02` | en | 16.38 | en_us/dev | 244 | `10035998729701048400.wav` | `5fefdcd12d4c` | `e0ea52b3bb0d` |
| `en_03` | en | 7.92 | en_us/dev | 320 | `1009709090964908274.wav` | `e5664ae10a62` | `9d6a75562584` |
| `en_04` | en | 4.14 | en_us/dev | 39 | `10098964113747380446.wav` | `7172cc6e8a7b` | `af9bc74c809f` |

Hashes are truncated here for reading; full values are in
`reports/apple-silicon-corpus-manifest.json`.

## How to rebuild this corpus

```bash
cd cohere-transcribe
uv run --no-sync python scripts/build_validation_corpus.py /path/to/corpus \
  > reports/apple-silicon-corpus-manifest.json
```

The script pins the dataset revision and takes the first four clips per language,
in tar order, that are 16 kHz mono and 3–30 s long, so a rerun reproduces the same
eight files byte for byte (only the `generated` date changes — verified by running
it twice and diffing the manifests). It streams the FLEURS `dev` tarballs and stops
early, downloading a few MB instead of the full 330 MB, and needs no HF token:
`google/fleurs` is ungated.

Dependencies are `huggingface_hub` and `soundfile` (both already in the venv) plus
the standard library. Tool versions used: python 3.12.13,
huggingface_hub 1.23.0, soundfile 0.14.0 (libsndfile 1.2.2).
