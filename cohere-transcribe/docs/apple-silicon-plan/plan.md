# Apple Silicon (MPS) enablement plan

Development machine: Apple M3, 24 GB unified memory, macOS 26.6.2, ffmpeg 8.1.1
on PATH.

## Position

This is a validation exercise, not a port. No code change is justified until a
measurement demands it. The plan's default outcome is **zero runtime source
changes** — CI configuration, a kernel-probe script, and tests are source changes
too, but nothing under the runtime path moves without a recorded failure behind
it.

The MPS path already exists and is written correctly. `pick_device` returns
`"mps"` from `--device auto` (`device.py:23`), `empty_device_cache` handles it
(`device.py:40`), `PYTORCH_ENABLE_MPS_FALLBACK=1` is set before torch import
(`_environment.py:10`), pinned memory and CUDA events are gated off
(`asr/generation.py:251`, `:334`), the OOM classifier matches the MPS allocator
message (`device.py:10`), and the console already suppresses CUDA memory
telemetry off-CUDA (`runtime/console.py:78`).

Every required wheel is published for `macosx_*_arm64` — verified in `uv.lock`:
`torch 2.11.0`, `torchcodec 0.14.0`, `torchaudio 2.11.0`, `onnxruntime`.
Nothing builds from source.

## Step 1 — Platform boundary and acceptance criteria

### Supported boundary

**Apple Silicon, macOS 14 or newer** — PyTorch 2.11 requires macOS 14+ for MPS
and the TorchCodec arm64 wheel targets `macosx_14_0_arm64`.

Memory qualifier, stated in exactly these terms: *validated on M3 with 24 GB
unified memory; lower-memory and other M-series systems are compatibility
targets, not release-tested.* "Apple Silicon" alone includes 8 GB machines, and
the 2B model must fit entirely in unified memory alongside the OS and decode
pipeline. Intel Macs are out of scope — no MPS, and no measured CPU-only claim.

### Binary gates (any failure blocks support)

- `cohere-transcribe-doctor` reports `accelerator: Apple MPS`.
- Full `pytest` suite passes on 3.12.
- Clean-wheel sequence (Step 3) passes, including `scripts/smoke_clean_audio.py`
  and `scripts/smoke_public_api.py`.
- `--audio-backend torchcodec` decodes; `--audio-backend ffmpeg` decodes; `auto`
  resolves to TorchCodec (`audio/backends.py:32`).
- `--alignment word` completes a real transcription on MPS.
- Output-lock contention behaves as specified below.
- CLI and Python API produce identical text and segment/word/cue timestamps,
  matching resolved configuration, and matching model provenance for equivalent
  options. Wall-clock timings are measured, not compared for equality.
- Reusable-model lifecycle: one-shot cleanup, segment/text ASR retention,
  word-mode ASR eviction, post-alignment reload.

### Output-lock criterion — fail-fast, not serialization

The lock is non-blocking: `fcntl.LOCK_EX | fcntl.LOCK_NB` (`locking.py:147`).
A contender does not wait; its `OSError` becomes
`RuntimeError: Another transcription process owns output set <identity>
(lock <path>, byte <offset>)` (`locking.py:167-171`). Nothing serializes.

The lease is also **per output stem**, not per directory —
`lock_target_for_outputs` (`locking.py:39`) hashes `parent/stem` into a byte
offset. Two processes writing *different* stems into one directory both succeed
by design.

Test, using the **same stem** in both processes:

1. While process A holds the lock, process B **fails immediately** with that
   `RuntimeError`.
2. B's failure occurs during `[1/4] Validating inputs and outputs` — the lock is
   taken in `inputs.py:476` under `build_jobs`, before ASR weights load. A
   contender must never pay a 2B model load to discover contention.
3. After A releases, a fresh B **succeeds**.
4. Two processes on *different* stems in the same directory both succeed.

### Measured criteria

Wall time and process RSS as medians over repeated alternating runs. A single run
proves completion and must not support a speed claim (`development.md:148`).

### Known evidence limitation — state this in the report

The balanced 500-file baseline is **not reproducible from this repository**.
`docs/performance.md:48` names the five source datasets (Casablanca, Common Voice
18 Arabic, FLEURS `ar_eg`, a Quran Classical Arabic proxy, SADA22) but the
specific file selection is not stored here. Retained-transcript comparison, which
`development.md:148` requires, is unavailable.

The substitute is **self-consistency**: MPS output compared against CPU FP32
output over a locally assembled fixed subset. That demonstrates implementation
stability on this platform. It does not demonstrate parity with the released CUDA
baseline, and no such parity may be claimed.

Record the local subset as a manifest under `reports/` — dataset IDs, per-file
hashes, decoded durations — so the corpus is reproducible even though the
original selection is not. `development.md:148` already designates `reports/` for
release evidence.

## Step 2 — Install and run the existing suite

System Python is 3.14.6 and there is no `python` on PATH; `requires-python` is
`>=3.10,<3.14`. Use the pinned 3.12 from `.python-version`.

```bash
cd cohere-transcribe
uv venv --python 3.12
uv sync --locked --group dev --extra word --extra onnx --extra auditok
uv run --no-sync cohere-transcribe-doctor
uv run --no-sync cohere-transcribe-doctor --mode word
uv run --no-sync pytest
```

`--no-sync` follows `docs/development.md:28`: it keeps an implicit
synchronization from replacing the environment's device-specific Torch build.
Every later `uv run` in this plan uses it too.

`quantized` is deliberately omitted — bitsandbytes is CUDA-gated in two places
(`preflight.py:58`, `asr/model.py:130`) and the arm64 wheel ships no MPS kernels.
`adapters` is omitted unless an adapter is under test.

Failures here are platform facts to record, not things to fix on sight.

## Step 3 — Build and install the wheel

Before any measurement, not after. `development.md:148` requires installed-wheel
execution, so measuring from the source checkout produces evidence that must be
discarded and repeated.

The sequence at `docs/development.md:120` **cannot run verbatim on this machine**.
Two adaptations:

- It calls bare `python -m venv`. There is no `python` here, and `python3` is the
  unsupported 3.14.6. Create the environment with the Step 2 interpreter:
  `".venv/bin/python" -m venv "$WHEEL_TEST/venv"`.
- It ends with `rm -rf "$WHEEL_TEST"`, which would delete the only installed
  wheel before Steps 4 and 5 can use it. **Postpone that deletion until all
  hardware validation finishes.**

Otherwise run it as written, both smoke scripts included. Every number in Steps 4
and 5 comes from this installed wheel.

## Step 4 — Precision: validate the shipped path

### 4a. Kernel probe (fail-fast gate, minutes, no model download)

A standalone script comparing BF16 and FP16 against FP32 on MPS for the
operations the model uses: large matmul, `scaled_dot_product_attention`, softmax,
layernorm. Record max absolute and relative error.

Run it **paired**: once with `PYTORCH_ENABLE_MPS_FALLBACK=0`, once with `1`.
At `0`, an op with no MPS kernel raises instead of silently relocating to CPU,
which turns an invisible performance cliff into a visible failure. At `1` it
matches shipped behavior. A difference between the two runs *is* the finding.

Scope that override to the probe only. `_environment.py:10` uses `setdefault`, so
a caller-set `0` propagates into the real runtime — end-to-end validation in
Steps 4b, 4d, and 5 run at the shipped default, or they measure a configuration
we do not ship.

This probe can only **veto** a dtype, never approve one. The existing
single-allocation check (`engine.py:100`) is not this: it proves BF16 tensors can
be allocated and nothing more, which is why it stays where it is, guarding
explicit `--dtype bf16`, and is never promoted into the `auto` decision.

### 4b. Precision validation (required for support)

`auto` cannot change on this evidence (see below), so this step validates the
shipped path rather than adjudicating a dtype. Two arms, on a fixed Arabic and
English subset, using the installed wheel at shipped environment defaults:

| Arm | Scope |
|---|---|
| `--device cpu --dtype fp32` | **Once per reference file.** Numerical reference. |
| `--device mps --dtype fp16` | The shipped `auto` behavior. Validate fully. |

CPU FP32 runs once per file, not repeatedly — it is a correctness reference, not
a performance arm, and the repository already treats FP32 that way for alignment
(`docs/performance.md:320`).

Record for the MPS FP16 arm: transcript divergence against CPU FP32, WER/CER
where references exist, wall-time median, process RSS, and every failure, NaN,
empty segment, OOM retry, and repetition-guard trip.

**Pass condition:** MPS FP16 completes cleanly and its divergence from CPU FP32
is explainable as precision, not corruption. NaNs, empty segments, or systematic
divergence block support and reopen the runtime-change path.

### 4c. Why `auto` does not change

`auto` (`engine.py:81`) stays FP16 regardless of how well BF16 performs on this
machine. BF16 is emulated through FP32 on earlier M-series parts, so an M3 BF16
win can be a serious regression on M1 and M2. A default that dispatches on
hardware nobody measured is not a measured default. Changing `auto` requires BF16
measurements across M-series generations — out of scope for this release.

`engine.py:81` and `tests/test_cli.py:220` are therefore untouched, and Step 6 has
**no runtime edit available to it** unless Step 4b or Step 5 fails. That is the
expected result, not a disappointment.

### 4d. BF16 characterization — optional follow-up

Not required for support. Run it only to decide whether to document `--dtype
bf16` as an M3-validated explicit option.

Add a `--device mps --dtype bf16` arm. Five alternating runs against FP16, report
medians. Document the flag only if BF16 median wall time is ≤ 110% of the FP16
median **and** BF16 produces no NaN, empty segment, or repetition-guard trip that
FP16 does not also produce, and no CER divergence from CPU FP32 exceeding 0.5
percentage points above FP16's.

If BF16 misses either bar, that is a **measured limitation of BF16 on this
platform** — a documentable finding about the flag, not an absence of difference.
Record it and leave the flag undocumented.

**If `--dtype bf16` is documented, it carries a caveat.** The guard at
`engine.py:99-107` allocates one BF16 tensor; on a machine where BF16 is
emulated, that allocation succeeds and the flag is accepted, then runs slowly.
The guard detects absence of support, not emulation. Documenting the flag without
saying so ships a footgun.

## Step 5 — Validate the platform assumptions

Each item is a Linux assumption, probably fine on macOS, untested. Confirm by
running the installed wheel at shipped environment defaults, not by reading
source.

- **Word alignment on device.** `preflight_forced_align` (`preflight.py:18`) only
  exercises TorchAudio's CPU `forced_align` on hand-built tensors. The actual
  aligner is `Wav2Vec2ForCTC` with `attn_implementation="sdpa"` moved onto the
  selected device (`alignment/runtime.py:30-48`), producing emissions on MPS that
  round-trip to CPU for `forced_align`. A real `--alignment word` run is
  required; the preflight proves nothing about this path. `--align-dtype fp16` is
  CUDA-gated (`engine.py:102`), so MMS runs FP32 here.
- **Output locks.** Per the Step 1 criterion. `state/locking.py:52` hardcodes
  `/tmp` (a symlink to `/private/tmp` on macOS) and `_validate_lock_directory`
  (`:63-80`) rejects it unless owned by the uid at mode 0700. macOS periodically
  prunes `/tmp`; recreation is expected and fine.
- **TorchCodec against Homebrew ffmpeg 8.1.1.** Confirm `auto` resolves to
  TorchCodec rather than silently falling through to the ffmpeg subprocess
  (`audio/backends.py:56`), and that per-file FFmpeg recovery works.
- **`PYTORCH_ALLOC_CONF=expandable_segments:True`** (`_environment.py:16`) is a
  CUDA allocator key. Confirm the MPS allocator ignores it without warning or
  raising. If it complains, set it only when CUDA is present.
- **CLI/API parity and model lifecycle**, per the Step 1 gates.

## Step 6 — Changes justified by Steps 4 and 5

Only failures and measurements authorize edits. On the evidence this plan can
produce, expect:

- **No runtime source change.** Step 4c closes that path; only a Step 4b or
  Step 5 failure could reopen it.
- A `macos-latest` CI job and the Step 4a probe script (Step 7).
- Documentation and metadata:
  - `pyproject.toml:28` — add `Operating System :: MacOS :: MacOS X`.
  - `README.md:9`, `docs/usage.md:7`, `docs/development.md:7` — Apple Silicon,
    macOS 14+, with the M3/24 GB validation qualifier.
  - `docs/architecture.md:201`, `docs/usage.md:530` — replace "unvalidated" with
    the measured result and its scope.
  - `docs/usage.md:541` — if the optional 4d follow-up passes, document
    `--dtype bf16` as an
    M3-validated explicit option, with the emulation caveat. State that `auto`
    remains FP16 and why.
  - `docs/development.md:120` — record the two Step 3 adaptations so the next
    person on macOS does not rediscover them.
  - `docs/performance.md` — M3 numbers in a **separate** table from the RTX 3060
    baselines, carrying the self-consistency caveat.
  - `CHANGELOG.md` — one entry.
  - `reports/` — versioned evidence file plus the Step 1 corpus manifest.

## Step 7 — CI, scoped honestly

Add a `macos-latest` job to `.github/workflows/ci.yml:47`. GitHub's standard
macOS runner is arm64 (M1-class, ~7 GB RAM) — enough for installation, the unit
suite, `cohere-transcribe-doctor`, and the Step 4a kernel probe.

It is **not** enough for the gated 2B model, and the job must not be described as
model validation. Full-model evidence stays manual on the M3. Say so in the
workflow and in the docs, or a green check will eventually be misread as
end-to-end validation.

Note the runner is M1-class — the generation where BF16 is emulated. It is the
right place to run the 4a probe and watch for the emulation signature, and the
wrong place to conclude anything about M3 throughput.

## Deferred — MPS adaptive batching and memory telemetry

Explicitly out of scope for initial support.

`ASRBatchController` reads memory only on CUDA (`asr/batching.py:52-68`), so on
MPS `total_vram_bytes` stays 0, `max_size` collapses to `initial`
(`batching.py:82`), and `record_success` returns early (`batching.py:135`). Batch
size stays static at the MPS default of 8 (`batching.py:20`), with OOM splitting
and learned caps still active (`batching.py:118-124`).

This cannot currently be fixed properly. `record_success` requires **peak**
allocated and reserved bytes (`batching.py:126-143`). PyTorch's MPS API exposes
`current_allocated_memory()`, `driver_allocated_memory()`, and
`recommended_max_memory()` — current and capacity values, no peak counter.
Feeding a current-allocation reading into a peak-shaped budget would grow the
controller against a meaningless number.

`record_oom_batch` likewise returns early off-CUDA (`batching.py:246`), so MPS
OOM is recovered without telemetry.

Revisit only when static batch 8 plus OOM reduction is **measurably**
insufficient on real workloads, or when PyTorch ships an MPS peak-memory API.
Until then, sweep `--batch-size` manually and record what works.

## Also out of scope

- **Quantized checkpoints.** bitsandbytes INT8/INT4 is correctly CUDA-gated
  (`preflight.py:58`, `asr/model.py:130`). The arm64 wheel exists but has no MPS
  kernels. Leave the gate and its error message alone.
- **MLX / GGUF.** Unsupported by this runtime, per README. Making this model
  genuinely fast on Apple Silicon means an MLX port — a different project.
- **Changing `auto` precision.** Requires multi-generation M-series measurement.

## Order

1 → 2 → 3 → 4a → 4b → 5 → 6 → 7. Step 4d is optional and may run any time
after 4b.

Step 3 precedes measurement so the evidence is admissible. Step 4a precedes 4b so a
broken kernel path dies before a gated model download. Step 5 precedes Step 6 so no
edit lands without a failure behind it. `rm -rf "$WHEEL_TEST"` runs after Step 5.
