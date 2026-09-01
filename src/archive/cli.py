"""M0 command line: `archive <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gates, legacy
from .config import CONFIG_HASH, PINNED_CONFIG, canonical_json

STATUS_MARK = {"pass": "PASS", "fail": "FAIL", "pending": "PEND"}


def cmd_config_hash(args) -> int:
    print(canonical_json(PINNED_CONFIG))
    print(f"configHash={CONFIG_HASH}")
    return 0


def cmd_gates(args) -> int:
    rows = gates.run_all()
    width = max(len(name) for name, _, _ in rows)
    for name, status, detail in rows:
        print(f"{STATUS_MARK[status]}  {name:<{width}}  {detail}")
    counts = {key: sum(1 for _, s, _ in rows if s == key) for key in STATUS_MARK}
    print(f"\n{counts['pass']} pass, {counts['pending']} pending, {counts['fail']} fail")
    return 0 if counts["fail"] == 0 and counts["pending"] == 0 else 1


def cmd_legacy_export(args) -> int:
    manifest = legacy.export(
        Path(args.source), prefix=args.prefix, exclude=tuple(args.exclude)
    )
    print(
        f"{manifest['count']} files ({manifest['totalBytes']} bytes) under "
        f"{args.prefix}/ — {manifest['uploaded']} uploaded, "
        f"{manifest['skipped']} already present, {manifest['excluded']} excluded"
    )
    return 0


def _peak_memory(stats) -> tuple[float, str]:
    """Peak device memory. cohere-transcribe only tracks it on CUDA (0.0 elsewhere),
    so on MPS fall back to torch's driver allocation and say so in the record."""
    if stats.peak_cuda_allocated_gib > 0:
        return round(stats.peak_cuda_allocated_gib, 2), "torch.cuda.max_memory_allocated"
    try:
        import torch

        if torch.backends.mps.is_available():
            allocated = torch.mps.driver_allocated_memory() / 1024**3
            return round(
                allocated, 2
            ), "torch.mps.driver_allocated_memory (end of run, not peak)"
    except Exception:
        # A missing or unusual torch must not lose an otherwise good benchmark.
        pass
    return 0.0, "unavailable on this device"


def cmd_bench(args) -> int:
    """Benchmark the real GPU under the pinned config and record the numbers."""
    import time

    from cohere_transcribe import Transcriber
    from cohere_transcribe.api.types import TranscriptionOptions

    from .config import transcriber_kwargs

    options = TranscriptionOptions(**transcriber_kwargs())
    transcriber = Transcriber(options)
    try:
        run = transcriber.transcribe([str(p) for p in args.audio])
    finally:
        transcriber.close()
    stats = run.statistics
    rtf_x = stats.real_time_factor_x
    peak_gb, peak_source = _peak_memory(stats)
    record = (
        json.loads(gates.RECORD_PATH.read_text()) if gates.RECORD_PATH.is_file() else {}
    )
    record["configHash"] = CONFIG_HASH
    record["gpuBenchmark"] = {
        "device": run.resolved_options.device,
        "files": len(run.results),
        "audioSeconds": round(stats.successful_audio_seconds, 2),
        "elapsedSeconds": round(stats.elapsed_seconds, 2),
        "rtfX": round(rtf_x, 3),
        "filesPerBatch": (
            round(len(run.results) / stats.asr_batches, 2) if stats.asr_batches else None
        ),
        "peakMemoryGb": peak_gb,
        "peakMemorySource": peak_source,
        "archiveHours": args.archive_hours,
        "projectedRuntimeHours": round(args.archive_hours / rtf_x, 1) if rtf_x else None,
        "measuredAt": int(time.time() * 1000),
    }
    gates.RECORD_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record["gpuBenchmark"], indent=2))
    print(f"recorded in {gates.RECORD_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "config-hash", help="print the pinned config and its configHash"
    ).set_defaults(func=cmd_config_hash)
    sub.add_parser("gates", help="check every M0 exit gate").set_defaults(func=cmd_gates)

    export = sub.add_parser("legacy-export", help="upload an assoli-v1 export dir to R2")
    export.add_argument("source", help="local directory holding the v1 export")
    export.add_argument(
        "--prefix", default=legacy.PREFIX, help=f"R2 key prefix (default {legacy.PREFIX})"
    )
    export.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching this glob, relative to source; repeatable",
    )
    export.set_defaults(func=cmd_legacy_export)

    bench = sub.add_parser("bench", help="benchmark the GPU under the pinned config")
    bench.add_argument("audio", nargs="+", type=Path, help="sample audio files")
    bench.add_argument(
        "--archive-hours",
        type=float,
        required=True,
        help="total archive audio hours, for the runtime projection",
    )
    bench.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
