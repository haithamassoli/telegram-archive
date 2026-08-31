# Apple Silicon (MPS) enablement plan

Target machine: Apple M3, 24 GB unified memory, macOS 26.6.2, ffmpeg 8.1.1 on PATH.

## Finding: this is mostly a validation job, not a port

The MPS code path already exists and is written correctly. `device.pick_device`
returns `"mps"` from `--device auto` (`src/cohere_transcribe/device.py:23`),
`empty_device_cache` handles it (`device.py:40`), `PYTORCH_ENABLE_MPS_FALLBACK=1`
is already set before torch import (`_environment.py:10`), pinned memory and
CUDA events are already gated off (`asr/generation.py:251`, `:334`), and the OOM
classifier already matches the MPS allocator message
(`device.py:10` `"out of memory"` vs. `"MPS backend out of memory (...)"`).

Every wheel needed is published for `macosx_*_arm64` — verified in `uv.lock`:
`torch 2.11.0`, `torchcodec 0.14.0`, `torchaudio 2.11.0`, `onnxruntime`,
`bitsandbytes 0.49.2`. Nothing has to be built from source.

What is actually missing is: a wrong default precision, Linux-only metadata,
and zero hardware validation. Docs already admit this
(`docs/architecture.md:201`, `docs/usage.md:530`).

## Phase 0 — get it running unmodified (do this first, ~20 min)

The system Python is 3.14.6; `requires-python` is `>=3.10,<3.14`. Use uv with
the pinned 3.12 from `.python-version`.

```bash
cd cohere-transcribe
uv venv --python 3.12
uv sync --locked --all-extras --group dev
uv run cohere-transcribe-doctor
uv run pytest
```

`cohere-transcribe-doctor` should print `accelerator: Apple MPS`
(`doctor.py:116-120`). Then one real file:

```bash
uv run cohere-transcribe sample.wav --language ar --vad-merge --device mps --batch-size 4
```

Do not skip this. Half the items below may already pass, and the ones that fail
tell you which of Phases 1-3 is real work versus paperwork.

## Phase 1 — the one change likely to affect output quality

**`--dtype auto` picks FP16 on MPS** (`runtime/engine.py:81`). The Cohere ASR
checkpoint is BF16. FP16 has a ~65k dynamic range and a 2B encoder-decoder can
overflow to NaN in it; BF16 keeps the training exponent range. Torch supports
BF16 on MPS on M-series + macOS 14+, and the codebase already has the probe —
it just only runs when the user explicitly asks for BF16 (`engine.py:99-107`).

Change: in `_resolve_precision`, for `device == "mps"` and
`requested_dtype == "auto"`, run the existing BF16 probe and select `bf16` when
it succeeds, `fp16` otherwise. Keep the explicit `--dtype bf16` error path as is.

Then compare a transcript against a CPU FP32 run of the same file. If FP16 was
producing garbage or empty segments, this is why.

Test: extend `tests/test_cli.py:220`
(`test_cli_resolves_mps_auto_precision_to_fp16`) into a pair — probe-passes →
bf16, probe-fails → fp16.

## Phase 2 — batch sizing and memory reporting on MPS

Not correctness bugs, but they make the run opaque and leave throughput unclaimed.

1. **The adaptive controller is inert on MPS.** `ASRBatchController.create`
   only reads memory for CUDA (`asr/batching.py:52-68`), so `total_vram_bytes`
   stays 0, `max_size` collapses to `initial` (`:82`), and `record_success`
   returns early on `total_vram_bytes <= 0` (`:135`). The batch stays pinned at
   the MPS default of 8 (`batching.py:20`).
   Fix, if measurement shows headroom: populate `total_vram_bytes` from
   `torch.mps.recommended_max_memory()` and `memory_budget_bytes` from
   `total * args.batch_vram_target`, and use
   `torch.mps.driver_allocated_memory()` as the `peak_reserved_bytes`
   equivalent. Skip this entirely if 8 already saturates the GPU.

2. **`record_oom_batch` returns on non-CUDA** (`batching.py:246`), so an MPS OOM
   is recovered but produces no memory telemetry.

3. **Console and profile report CUDA peaks only**
   (`runtime/console.py:79-81`, `pipeline/transcription.py:334`), so an MPS run
   shows `0.00 GiB`. Either fill from `torch.mps.current_allocated_memory()` or
   suppress the line off-CUDA rather than printing a misleading zero.

4. **Start conservative.** 24 GB unified is shared with the OS and the decode
   pipeline; BF16 weights are ~4 GB before activations and KV cache. Sweep
   `--batch-size 2,4,8,12` on a fixed corpus before touching item 1.

## Phase 3 — verify the platform assumptions the code makes

Each of these is a Linux assumption that is probably fine on macOS but is
untested. Confirm by running, not by reading.

- **Output locks.** `state/locking.py:52` hardcodes `/tmp` (macOS symlinks it to
  `/private/tmp`), and `_validate_lock_directory` (`:63-80`) rejects the
  directory unless it is owned by the uid with mode 0700. `fcntl.lockf` byte
  ranges (`:148`) are supported on macOS via `F_SETLK`. Verify a two-process
  concurrent run over the same output directory still serializes. Note macOS
  periodically prunes `/tmp`; the registry file is recreated, which is fine.
- **`PYTORCH_ALLOC_CONF=expandable_segments:True`** (`_environment.py:16`) is a
  CUDA allocator key. Confirm the MPS allocator ignores it silently rather than
  warning or raising; if it complains, only set it when CUDA is present.
- **TorchCodec.** `probe_torchcodec` (`audio/backends.py:32`) needs the system
  FFmpeg dylibs. Homebrew ffmpeg 8.1.1 is installed; verify `--audio-backend
  auto` resolves to `torchcodec` and not the ffmpeg-subprocess fallback.
- **Word alignment.** `--align-dtype fp16` is CUDA-gated (`engine.py:102`), so
  MMS forced alignment runs FP32 on MPS. Confirm `torchaudio.functional.forced_align`
  passes `preflight_forced_align` (`preflight.py:19`) on this build.

## Explicitly out of scope

- **Quantized checkpoints.** bitsandbytes INT8/INT4 is CUDA-gated in two places
  (`preflight.py:58`, `asr/model.py:130`). The arm64 wheel exists but ships no
  MPS kernels. Leave the gate; the error message is already correct.
- **MLX / GGUF.** README already states they are unsupported by this runtime.
  Making the Cohere model fast on Apple Silicon *properly* means an MLX port —
  a different project, not this one.

## Phase 4 — make the support claim true

Only after Phases 1-3 pass on hardware. These are all metadata; changing them
before validation would be a false claim.

- `pyproject.toml:28`: add `Operating System :: MacOS :: MacOS X`.
- `README.md:9`, `docs/usage.md:7`, `docs/development.md:7`: state macOS on
  Apple Silicon alongside Linux.
- `docs/architecture.md:201` and `docs/usage.md:530`: replace "unvalidated" with
  the measured result.
- `docs/performance.md`: add the M3 numbers, clearly separated from the RTX 3060
  baselines. Do not merge the tables.
- `.github/workflows/ci.yml:47`: add a `macos-latest` job. GitHub's macOS
  runners are Apple Silicon but have no GPU passthrough concerns — MPS is
  available. If the runner proves flaky, run CPU-only tests there and keep MPS
  validation manual.
- `CHANGELOG.md`: one entry.

## Order of work

Phase 0 → Phase 1 → Phase 3 → Phase 2 → Phase 4.

Phase 3's platform checks come before Phase 2's tuning because a broken lock or
decoder invalidates any throughput number you measure.
