"""Compare BF16 and FP16 against an FP32 reference on MPS for the ops the model uses.

Plan step 4a (`docs/apple-silicon-plan/plan.md`), tasks T4.1-T4.4. Standalone:
stdlib plus torch, and deliberately **never** imports `cohere_transcribe`, whose
`_environment.py` would `setdefault` `PYTORCH_ENABLE_MPS_FALLBACK` and blur the
paired runs (T4.3).

Run it paired, setting the variable outside the process -- the script reads and
reports the value it was launched with, and never sets it (T4.2):

    PYTORCH_ENABLE_MPS_FALLBACK=0 uv run --no-sync python scripts/mps_kernel_probe.py
    PYTORCH_ENABLE_MPS_FALLBACK=1 uv run --no-sync python scripts/mps_kernel_probe.py

A difference between the two runs is itself the finding. At `0` a missing MPS
kernel raises; that is caught per operation and recorded as a `raised:` row so
one absent kernel cannot hide the other operations' numbers.

**This probe can only veto a dtype, never approve one** (T4.4). It exits non-zero
only on an outright failure: a non-finite output, or an operation that raised
while the fallback was enabled. Large-but-finite error is reported, never vetoed
-- a human reads the table and judges. It is not promoted into the `auto`
decision, and it does not replace the single-allocation check at `runtime/engine.py:100`.

Exits 0 with a message when MPS is unavailable, so CI can run it anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import torch
import torch.nn.functional as F

OPS = {
    "matmul": lambda t: t["a"] @ t["b"],
    "sdpa": lambda t: F.scaled_dot_product_attention(t["q"], t["k"], t["v"]),
    "softmax": lambda t: torch.softmax(t["x"], dim=-1),
    "layernorm": lambda t: F.layer_norm(t["x"], (t["x"].shape[-1],), t["w"], t["b1"]),
}


def tensors(device: str) -> dict[str, torch.Tensor]:
    """Fixed-seed FP32 inputs, generated on CPU so both devices see the same values."""
    generator = torch.Generator(device="cpu").manual_seed(0)

    def make(*shape: int) -> torch.Tensor:
        return torch.randn(*shape, generator=generator).to(device)

    return {
        "a": make(2048, 2048),
        "b": make(2048, 2048),
        "q": make(2, 8, 512, 64),
        "k": make(2, 8, 512, 64),
        "v": make(2, 8, 512, 64),
        "x": make(64, 2048),
        "w": make(2048),
        "b1": make(2048),
    }


def compare(op: str, base: dict[str, torch.Tensor], dtype: torch.dtype) -> dict:
    """One (op, dtype) result row against the FP32 result of the same inputs."""
    name = str(dtype).removeprefix("torch.")
    reference = OPS[op](base).float()
    try:
        actual = OPS[op]({k: v.to(dtype) for k, v in base.items()}).float()
    except Exception as error:  # ponytail: a raised kernel is a result row, not a crash
        return {
            "op": op,
            "dtype": name,
            "max_abs": None,
            "max_rel": None,
            "status": f"raised: {error}",
        }
    error_abs = (actual - reference).abs()
    # ponytail: clamp the denominator instead of masking near-zero reference values.
    relative = error_abs / reference.abs().clamp_min(1e-6)
    finite = bool(torch.isfinite(actual).all())
    return {
        "op": op,
        "dtype": name,
        "max_abs": error_abs.max().item(),
        "max_rel": relative.max().item(),
        "status": "ok" if finite else "NON-FINITE",
    }


def matmul_seconds(base: dict[str, torch.Tensor], dtype: torch.dtype) -> float:
    """Median-free mean of 20 large matmuls; only the between-dtype ratio matters."""
    cast = {k: v.to(dtype) for k, v in base.items()}
    OPS["matmul"](cast)
    torch.mps.synchronize()
    start = time.perf_counter()
    for _ in range(20):
        OPS["matmul"](cast)
    torch.mps.synchronize()
    return (time.perf_counter() - start) / 20


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit results as JSON")
    arguments = parser.parse_args()

    if not torch.backends.mps.is_available():
        print("MPS unavailable on this machine; skipping the kernel probe.")
        return 0

    base = tensors("mps")
    rows = [
        compare(op, base, dtype)
        for dtype in (torch.float16, torch.bfloat16)
        for op in OPS
    ]
    timings = {
        str(d).removeprefix("torch."): matmul_seconds(base, d)
        for d in (torch.float32, torch.float16, torch.bfloat16)
    }
    # BF16 emulated through FP32 shows up as bf16/fp16 well above 1 (plan step 7).
    ratios = {
        "bf16_over_fp16": timings["bfloat16"] / timings["float16"],
        "bf16_over_fp32": timings["bfloat16"] / timings["float32"],
    }
    fallback = os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", "<unset>")
    vetoed = [
        r
        for r in rows
        if r["status"] == "NON-FINITE"
        or (fallback == "1" and r["status"].startswith("raised"))
    ]
    report = {
        "torch": torch.__version__,
        "PYTORCH_ENABLE_MPS_FALLBACK": fallback,
        "results": rows,
        "matmul_seconds": timings,
        "matmul_ratios": ratios,
        "veto": [f"{r['op']}/{r['dtype']}" for r in vetoed],
    }

    if arguments.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"torch {torch.__version__}  PYTORCH_ENABLE_MPS_FALLBACK={fallback}")
        print(f"{'op':<10} {'dtype':<10} {'max_abs':>11} {'max_rel':>11}  status")
        for row in rows:
            cells = [
                "-" if row[k] is None else f"{row[k]:.3e}"
                for k in ("max_abs", "max_rel")
            ]
            print(
                f"{row['op']:<10} {row['dtype']:<10} {cells[0]:>11} {cells[1]:>11}  {row['status']}"
            )
        print("\nmatmul seconds:", " ".join(f"{k}={v:.5f}" for k, v in timings.items()))
        print(
            "bf16 emulation ratios (>~1 suggests emulation):",
            " ".join(f"{k}={v:.2f}" for k, v in ratios.items()),
        )
        raised = [r for r in rows if r["status"].startswith("raised")]
        if vetoed:
            print("\nVETO:", ", ".join(report["veto"]))
        else:
            print(
                "\nNo veto. Large-but-finite error is reported, not graded -- you judge."
            )
            if raised:
                print(
                    f"But {len(raised)} operation(s) raised at fallback {fallback}; "
                    "read the table before concluding anything."
                )
    return 1 if vetoed else 0


if __name__ == "__main__":
    raise SystemExit(main())
