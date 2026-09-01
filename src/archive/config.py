"""Pinned transcription config, configHash, and environment access (§0.5)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The pinned transcription config. Exactly the fields listed in plan §0.5 /
# Phase 0 — these six define transcript identity. modelRevision was resolved
# branch -> commit once, here, and is never resolved again at runtime.
# Changing any value here mints a new configHash and re-transcribes the archive.
PINNED_CONFIG: dict[str, object] = {
    "model": "CohereLabs/cohere-transcribe-arabic-07-2026",
    "modelRevision": "0a8193caa4f3f92131471ab08824e488141cb392",
    "language": "ar",
    "vad": "silero",
    "vadMerge": True,
    "alignment": "segment",
}


def canonical_json(value: object) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(config: dict[str, object] | None = None) -> str:
    """sha256 of the canonical JSON of the pinned config."""
    payload = canonical_json(PINNED_CONFIG if config is None else config)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


CONFIG_HASH = config_hash()


def transcriber_kwargs() -> dict[str, object]:
    """The pinned config as cohere-transcribe keyword arguments."""
    return {
        "model": PINNED_CONFIG["model"],
        "model_revision": PINNED_CONFIG["modelRevision"],
        "language": PINNED_CONFIG["language"],
        "vad": PINNED_CONFIG["vad"],
        "vad_merge": PINNED_CONFIG["vadMerge"],
        "alignment": PINNED_CONFIG["alignment"],
    }


def check_revision_drift() -> tuple[bool, str]:
    """Fail fast if the package default model revision left the pin (§0.5 am. 5)."""
    try:
        from cohere_transcribe.model_identity import (  # type: ignore[import-not-found]
            DEFAULT_ASR_MODEL_ID,
            DEFAULT_ASR_MODEL_REVISION,
        )
    except ImportError:
        return True, "cohere-transcribe not installed; drift check skipped"
    if PINNED_CONFIG["model"] != DEFAULT_ASR_MODEL_ID:
        return True, f"pinned model is not the package default ({DEFAULT_ASR_MODEL_ID})"
    if PINNED_CONFIG["modelRevision"] != DEFAULT_ASR_MODEL_REVISION:
        return False, (
            f"package default revision {DEFAULT_ASR_MODEL_REVISION} != pinned "
            f"{PINNED_CONFIG['modelRevision']} — pass model_revision explicitly"
        )
    return True, "package default matches the pin"


# .env.local is written by `npx convex dev` and holds CONVEX_URL; it is read
# first so the deployment the CLI selected is the one this code talks to.
ENV_FILES = (".env.local", ".env")


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env.local/.env without overriding the shell.

    ponytail: 12-line parser instead of python-dotenv; these files are ours and
    hold plain secrets. Swap in python-dotenv if it ever needs multiline values.
    """
    if path is None:
        for name in ENV_FILES:
            load_env(REPO_ROOT / name)
        return
    env_path = path
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        # `npx convex dev` writes trailing "# team: ..." comments on its values.
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(name: str, default: str | None = None) -> str | None:
    load_env()
    return os.environ.get(name, default)


def require_env(*names: str) -> dict[str, str]:
    load_env()
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
