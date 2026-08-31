# Apple Silicon (MPS) enablement — milestones and tasks

Source of truth: `APPLE_SILICON_PLAN.md`. This file is the execution breakdown;
where the two disagree, the plan wins.

Machine of record: Apple M3, 24 GB unified memory, macOS 26.6.2, ffmpeg 8.1.1.

**Default outcome is zero runtime source changes.** Tasks that edit runtime code
exist only as contingencies, gated on a recorded failure. Do not "fix" anything
in M2–M6 on sight — record it.

## Milestone map

| ID | Milestone | Plan step | Depends on | Exit condition |
|---|---|---|---|---|
| M1 | Scope and acceptance criteria frozen | 1 | — | Criteria written down and agreed; corpus manifest exists |
| M2 | Environment installed, suite green | 2 | M1 | `pytest` passes on 3.12, doctor reports Apple MPS |
| M3 | Admissible build artifact | 3 | M2 | Wheel built, installed in a clean venv, both smoke scripts pass |
| M4 | Kernel probe (fail-fast gate) | 4a | M2 | Paired probe run, results recorded, no veto |
| M5 | Precision validation | 4b, 4c | M3, M4 | MPS FP16 clean vs CPU FP32; `auto` decision recorded as unchanged |
| M6 | Platform assumptions validated | 5 | M3 | All five assumption items confirmed on the installed wheel |
| M7 | Documentation and metadata | 6 | M5, M6 | Docs, metadata, CHANGELOG, `reports/` evidence landed |
| M8 | CI, scoped honestly | 7 | M4 | `macos-latest` job green, explicitly not described as model validation |
| M9 | BF16 characterization (optional) | 4d | M5 | Documented as an M3-validated flag, or recorded as a measured limitation |

Critical path: **M1 → M2 → M3 → M4 → M5 → M6 → M7**. M8 can run in parallel once
M4 exists. M9 is optional and never blocks release.

---

## M1 — Scope and acceptance criteria frozen

No hardware work. Ends with everything downstream measurable against something
written down.

- [ ] **T1.1** Write the supported boundary: Apple Silicon, macOS 14 or newer.
      Record the two reasons (PyTorch 2.11 MPS floor, TorchCodec
      `macosx_14_0_arm64` wheel target). Intel Macs excluded.
- [ ] **T1.2** Adopt the memory qualifier verbatim: *validated on M3 with 24 GB
      unified memory; lower-memory and other M-series systems are compatibility
      targets, not release-tested.*
- [ ] **T1.3** Freeze the binary gate list (doctor, pytest, clean-wheel, both
      audio backends, `--alignment word`, output locks, CLI/API parity, model
      lifecycle). Any failure blocks support.
- [ ] **T1.4** Write the CLI/API parity criterion precisely: identical text and
      segment/word/cue timestamps, matching resolved configuration, matching
      model provenance. Wall-clock timings are measured, never compared for
      equality.
- [ ] **T1.5** Assemble the local Arabic + English reference subset and write its
      manifest to `reports/` — dataset IDs, per-file hashes, decoded durations.
- [ ] **T1.6** Record the evidence limitation in the report template: the balanced
      500-file baseline is not reproducible from this repo
      (`docs/performance.md:48` names datasets, not the selection). The substitute
      is self-consistency against CPU FP32. **No parity with the released CUDA
      baseline may be claimed.**
- [ ] **T1.7** Fix the measurement protocol: medians over repeated alternating
      runs, per `development.md:148`. A single run proves completion only.

**Risk:** T1.5 is the one task that can silently expand. Timebox it — a small
fixed subset that is hashed and reproducible beats a large one that is not.

---

## M2 — Environment installed, suite green

System Python is 3.14.6 and there is no `python` on PATH; `requires-python` is
`>=3.10,<3.14`. Use the pinned 3.12.

- [ ] **T2.1** `uv venv --python 3.12`
- [ ] **T2.2** `uv sync --locked --group dev --extra word --extra onnx --extra auditok`.
      `quantized` deliberately omitted (bitsandbytes is CUDA-gated at
      `preflight.py:58` and `asr/model.py:130`); `adapters` omitted unless an
      adapter is under test.
- [ ] **T2.3** `uv run --no-sync cohere-transcribe-doctor` → must report
      `accelerator: Apple MPS`. **Binary gate.**
- [ ] **T2.4** `uv run --no-sync cohere-transcribe-doctor --mode word`
- [ ] **T2.5** `uv run --no-sync pytest` — full suite on 3.12. **Binary gate.**
- [ ] **T2.6** Record every failure as a platform fact. Do not patch source here.

Every `uv run` in this project uses `--no-sync` (`docs/development.md:28`) so an
implicit sync cannot replace the device-specific Torch build.

---

## M3 — Admissible build artifact

Runs **before** any measurement. Measuring from the source checkout produces
evidence that must be discarded and repeated (`development.md:148`).

- [ ] **T3.1** Follow `docs/development.md:120` with two documented adaptations:
      - create the test venv with the Step 2 interpreter —
        `".venv/bin/python" -m venv "$WHEEL_TEST/venv"` — because there is no
        `python` on PATH and `python3` is the unsupported 3.14.6;
      - **postpone `rm -rf "$WHEEL_TEST"`** until M6 finishes, or it deletes the
        only installed wheel M5 and M6 depend on.
- [ ] **T3.2** Build the wheel and install it into the clean venv.
- [ ] **T3.3** `scripts/smoke_clean_audio.py` passes. **Binary gate.**
- [ ] **T3.4** `scripts/smoke_public_api.py` passes. **Binary gate.**
- [ ] **T3.5** Confirm every M5/M6 command targets this installed wheel, not the
      checkout.

---

## M4 — Kernel probe (fail-fast gate)

Minutes, no model download. Cheap enough to run before anything expensive.

- [ ] **T4.1** Write a standalone probe script comparing BF16 and FP16 against
      FP32 on MPS for the operations the model actually uses: large matmul,
      `scaled_dot_product_attention`, softmax, layernorm. Report max absolute and
      relative error.
- [ ] **T4.2** Run it **paired**: once with `PYTORCH_ENABLE_MPS_FALLBACK=0`, once
      with `1`. At `0` a missing MPS kernel raises instead of silently relocating
      to CPU; at `1` it matches shipped behavior. **A difference between the two
      runs is itself the finding** — record it.
- [ ] **T4.3** Scope the `=0` override to the probe process only.
      `_environment.py:10` uses `setdefault`, so a caller-set `0` propagates into
      the runtime and M5/M6/M9 would measure a configuration we do not ship.
- [ ] **T4.4** Record results. The probe can only **veto** a dtype, never approve
      one. It must not be promoted into the `auto` decision, and the existing
      single-allocation check (`engine.py:100`) stays where it is, guarding
      explicit `--dtype bf16`.

---

## M5 — Precision validation

`auto` cannot change on this evidence, so this milestone validates the shipped
path rather than adjudicating a dtype.

- [ ] **T5.1** Run `--device cpu --dtype fp32` **once per reference file**. It is
      a numerical reference, not a performance arm — the repo already treats FP32
      that way for alignment (`docs/performance.md:320`).
- [ ] **T5.2** Run `--device mps --dtype fp16` fully — this is the shipped `auto`
      behavior — on the fixed Arabic and English subset, installed wheel, shipped
      environment defaults.
- [ ] **T5.3** Record for the MPS arm: transcript divergence vs CPU FP32, WER/CER
      where references exist, wall-time median, process RSS, and every failure,
      NaN, empty segment, OOM retry, and repetition-guard trip.
- [ ] **T5.4** Evaluate the pass condition: MPS FP16 completes cleanly and its
      divergence from CPU FP32 is explainable as precision, not corruption.
      NaNs, empty segments, or systematic divergence **block support** and reopen
      the runtime-change path.
- [ ] **T5.5** Record the `auto` decision explicitly: `engine.py:81` stays FP16.
      BF16 is emulated through FP32 on earlier M-series parts, so an M3 BF16 win
      can be an M1/M2 regression. `engine.py:81` and `tests/test_cli.py:220` are
      untouched. **This is the expected result, not a disappointment.**

---

## M6 — Platform assumptions validated

Each item is a Linux assumption, probably fine on macOS, untested. Confirm by
running the installed wheel at shipped defaults — **not by reading source**.

- [ ] **T6.1 Word alignment on device.** A real `--alignment word` transcription
      on MPS. `preflight_forced_align` (`preflight.py:18`) proves nothing about
      this path — the real aligner is `Wav2Vec2ForCTC` with sdpa moved onto the
      device (`alignment/runtime.py:30-48`). `--align-dtype fp16` is CUDA-gated
      (`engine.py:102`), so MMS runs FP32 here. **Binary gate.**
- [ ] **T6.2 Output locks — fail-fast, not serialization.** The lock is
      `LOCK_EX | LOCK_NB` (`locking.py:147`) and the lease is per output *stem*
      (`locking.py:39`). Four sub-checks, **binary gate**:
      1. While A holds the lock, B on the **same stem** fails immediately with
         `RuntimeError: Another transcription process owns output set ...`.
      2. B fails during `[1/4] Validating inputs and outputs` — the lock is taken
         in `inputs.py:476` under `build_jobs`, before ASR weights load. A
         contender must never pay a 2B model load to discover contention.
      3. After A releases, a fresh B succeeds.
      4. Two processes on **different stems** in one directory both succeed.
- [ ] **T6.3 Lock directory on macOS.** `state/locking.py:52` hardcodes `/tmp`
      (a symlink to `/private/tmp`); `_validate_lock_directory` (`:63-80`)
      requires uid ownership at mode 0700. macOS prunes `/tmp` periodically —
      confirm recreation works. Expected and fine.
- [ ] **T6.4 TorchCodec vs Homebrew ffmpeg 8.1.1.** `--audio-backend torchcodec`
      decodes; `--audio-backend ffmpeg` decodes; `auto` resolves to TorchCodec
      (`audio/backends.py:32`) rather than silently falling through to the
      subprocess (`:56`). Confirm per-file FFmpeg recovery. **Binary gate.**
- [ ] **T6.5 `PYTORCH_ALLOC_CONF=expandable_segments:True`** (`_environment.py:16`)
      is a CUDA allocator key. Confirm the MPS allocator ignores it without
      warning or raising. If it complains, set it only when CUDA is present —
      this is the one contingent runtime edit in the plan.
- [ ] **T6.6 CLI/API parity**, per T1.4. **Binary gate.**
- [ ] **T6.7 Reusable-model lifecycle:** one-shot cleanup, segment/text ASR
      retention, word-mode ASR eviction, post-alignment reload. **Binary gate.**
- [ ] **T6.8** Only after all of the above: `rm -rf "$WHEEL_TEST"`.

---

## M7 — Documentation and metadata

Only failures and measurements authorize edits. On the evidence this plan
produces, expect **no runtime source change**.

- [ ] **T7.1** `pyproject.toml:28` — add `Operating System :: MacOS :: MacOS X`.
- [ ] **T7.2** `README.md:9`, `docs/usage.md:7`, `docs/development.md:7` — Apple
      Silicon, macOS 14+, with the M3/24 GB qualifier from T1.2.
- [ ] **T7.3** `docs/architecture.md:201`, `docs/usage.md:530` — replace
      "unvalidated" with the measured result and its scope.
- [ ] **T7.4** `docs/development.md:120` — record the two M3 adaptations so the
      next person on macOS does not rediscover them.
- [ ] **T7.5** `docs/performance.md` — M3 numbers in a **separate** table from the
      RTX 3060 baselines, carrying the self-consistency caveat from T1.6.
- [ ] **T7.6** `CHANGELOG.md` — one entry.
- [ ] **T7.7** `reports/` — versioned evidence file plus the T1.5 corpus manifest.
- [ ] **T7.8** *(conditional on M9 passing)* `docs/usage.md:541` — document
      `--dtype bf16` as an M3-validated explicit option, with the emulation
      caveat. State that `auto` remains FP16 and why.

---

## M8 — CI, scoped honestly

- [ ] **T8.1** Add a `macos-latest` job to `.github/workflows/ci.yml:47`.
- [ ] **T8.2** Scope it to installation, the unit suite,
      `cohere-transcribe-doctor`, and the M4 kernel probe.
- [ ] **T8.3** State in the workflow **and** the docs that this is not model
      validation — the runner is M1-class with ~7 GB RAM and cannot host the
      gated 2B model. Full-model evidence stays manual on the M3. Without this, a
      green check will eventually be misread as end-to-end validation.
- [ ] **T8.4** Note that the runner is the generation where BF16 is emulated: the
      right place to watch for the emulation signature in the probe, the wrong
      place to conclude anything about M3 throughput.

---

## M9 — BF16 characterization (optional)

Not required for support. Run it only to decide whether `--dtype bf16` gets
documented.

- [ ] **T9.1** Add a `--device mps --dtype bf16` arm; five alternating runs
      against FP16; report medians.
- [ ] **T9.2** Evaluate all three criteria — document the flag only if BF16
      median wall time is ≤ 110% of the FP16 median, **and** BF16 produces no
      NaN, empty segment, or repetition-guard trip that FP16 does not also
      produce, **and** no CER divergence from CPU FP32 exceeding 0.5 percentage
      points above FP16's.
- [ ] **T9.3** If BF16 misses any bar, record it as a **measured limitation of
      BF16 on this platform** — a documentable finding about the flag, not an
      absence of difference — and leave the flag undocumented.
- [ ] **T9.4** If documented, ship the caveat: the guard at `engine.py:99-107`
      allocates one BF16 tensor, so on a machine where BF16 is emulated the
      allocation succeeds, the flag is accepted, and it then runs slowly. The
      guard detects absence of support, not emulation. Documenting without saying
      so ships a footgun.

---

## Out of scope — do not open these

- **MPS adaptive batching / memory telemetry.** `ASRBatchController` reads memory
  only on CUDA (`asr/batching.py:52-68`), so `max_size` collapses to `initial`
  and batch size stays static at 8, with OOM splitting and learned caps still
  active. Cannot be fixed properly: `record_success` needs **peak** allocated and
  reserved bytes and PyTorch's MPS API exposes only current and capacity values.
  Revisit when static batch 8 plus OOM reduction is *measurably* insufficient, or
  when PyTorch ships an MPS peak-memory API. Until then sweep `--batch-size`
  manually.
- **Quantized checkpoints.** bitsandbytes INT8/INT4 is correctly CUDA-gated.
  Leave the gate and its message alone.
- **MLX / GGUF.** A different project.
- **Changing `auto` precision.** Requires multi-generation M-series measurement.
