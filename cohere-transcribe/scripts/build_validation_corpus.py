"""Build the T1.5 local ASR validation corpus (4 Arabic + 4 English FLEURS clips).

Streams the FLEURS dev tarballs and stops after the first 4 usable clips per
language, so it downloads a few MB instead of the full 330 MB.

    uv run --no-sync python scripts/build_validation_corpus.py [OUTDIR]
"""

import datetime as dt
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

import huggingface_hub as hfh
import soundfile as sf

REPO, REV = "google/fleurs", "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
LANGS = {"ar": ("ar_eg", "dev"), "en": ("en_us", "dev")}
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "corpus")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


OUT.mkdir(parents=True, exist_ok=True)
entries = []
for lang, (cfg, split) in LANGS.items():
    tsv = Path(
        hfh.hf_hub_download(
            REPO, f"data/{cfg}/{split}.tsv", repo_type="dataset", revision=REV
        )
    )
    rows = [line.split("\t") for line in tsv.read_text(encoding="utf-8").splitlines()]
    by_name = {r[1]: (i, r) for i, r in enumerate(rows)}
    url = hfh.hf_hub_url(
        REPO, f"data/{cfg}/audio/{split}.tar.gz", repo_type="dataset", revision=REV
    )
    with (
        urllib.request.urlopen(url, timeout=60) as resp,
        tarfile.open(fileobj=resp, mode="r|gz") as tar,
    ):
        n = 0
        for m in tar:
            if n == 4:
                break
            if not m.name.endswith(".wav") or Path(m.name).name not in by_name:
                continue
            idx, row = by_name[Path(m.name).name]
            raw = tar.extractfile(m).read()
            info = sf.info(io.BytesIO(raw))
            if (
                info.samplerate != 16000
                or info.channels != 1
                or not 3.0 <= info.duration <= 30.0
            ):
                continue
            n += 1
            stem = f"{lang}_{n:02d}"
            (OUT / f"{stem}.wav").write_bytes(
                raw
            )  # already 16 kHz mono WAV; copied verbatim
            (OUT / f"{stem}.txt").write_text(row[2] + "\n", encoding="utf-8")
            entries.append(
                dict(
                    id=stem,
                    language=lang,
                    source_dataset=REPO,
                    revision=REV,
                    config=cfg,
                    split=split,
                    tsv_row_index=idx,
                    fleurs_id=int(row[0]),
                    original_filename=Path(m.name).name,
                    tar_member=m.name,
                    sha256_wav=sha((OUT / f"{stem}.wav").read_bytes()),
                    sha256_transcript=sha((OUT / f"{stem}.txt").read_bytes()),
                    duration_seconds=round(info.duration, 3),
                    sample_rate=info.samplerate,
                    channels=info.channels,
                    sample_format=info.subtype,
                )
            )
    if n != 4:
        raise SystemExit(f"{lang}: found {n} usable clips, expected 4")

versions = {p: __import__(p).__version__ for p in ("huggingface_hub", "soundfile")}
versions["python"] = sys.version.split()[0]
versions["libsndfile"] = sf.__libsndfile_version__
manifest = {
    "generated": dt.date.today().isoformat(),
    "generator": "scripts/build_validation_corpus.py",
    "tool_versions": versions,
    "note": (
        "Local FLEURS subset assembled for T1.5. It is the substitute for the "
        "balanced 500-file baseline of docs/performance.md:48, whose file selection is "
        "not stored in this repository (T1.6). It supports self-consistency only — MPS "
        "output compared against CPU FP32 output on the same files — and does NOT "
        "demonstrate parity with the released CUDA baseline. No such parity may be claimed."
    ),
    "files": entries,
}
print(json.dumps(manifest, ensure_ascii=False, indent=2))
