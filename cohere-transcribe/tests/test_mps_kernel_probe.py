from __future__ import annotations

import importlib.util
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mps_kernel_probe", ROOT / "scripts/mps_kernel_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_fp32_against_fp32_is_exact_on_cpu() -> None:
    base = probe.tensors("cpu")
    for op in probe.OPS:
        row = probe.compare(op, base, torch.float32)
        assert row["status"] == "ok", row
        assert row["max_abs"] == 0.0
        assert row["max_rel"] == 0.0


def test_fp16_softmax_error_is_small_on_cpu() -> None:
    row = probe.compare("softmax", probe.tensors("cpu"), torch.float16)
    assert row["status"] == "ok", row
    assert 0.0 <= row["max_abs"] < 1e-2
