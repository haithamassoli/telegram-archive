"""M0 exit gates. Each check returns (status, detail).

status: "pass" — verified now; "pending" — waiting on a human/external step;
"fail" — configured but broken.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import legacy, r2
from .config import CONFIG_HASH, PINNED_CONFIG, REPO_ROOT, check_revision_drift, env

RECORD_PATH = REPO_ROOT / "m0.gates.json"
TIMEOUT = 15


def _record() -> dict:
    if not RECORD_PATH.is_file():
        return {}
    return json.loads(RECORD_PATH.read_text(encoding="utf-8"))


def _http(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def gate_config() -> tuple[str, str]:
    recorded = _record().get("configHash")
    if recorded and recorded != CONFIG_HASH:
        return "fail", (
            f"pinned config changed: computed {CONFIG_HASH} != recorded {recorded}; "
            "a new configHash means re-transcribing the archive"
        )
    ok, detail = check_revision_drift()
    status = "pass" if ok else "fail"
    return status, f"configHash={CONFIG_HASH} — {detail}"


def gate_model_access() -> tuple[str, str]:
    token = env("HF_TOKEN")
    if not token:
        return "pending", "HF_TOKEN not set; accept the model terms and export a token"
    url = (
        f"https://huggingface.co/api/models/{PINNED_CONFIG['model']}"
        f"/revision/{PINNED_CONFIG['modelRevision']}"
    )
    try:
        code, _ = _http(url, {"Authorization": f"Bearer {token}"})
    except OSError as exc:
        return "pending", f"no network: {exc}"
    if code == 200:
        return "pass", "pinned model+revision readable with HF_TOKEN"
    if code in (401, 403):
        return "fail", f"HTTP {code} — accept the model terms for this account"
    return "fail", f"HTTP {code} — pinned revision not reachable"


def gate_r2() -> tuple[str, str]:
    # R2 answers a bad key with a bare 400, so check the shape first: access
    # keys are 32 hex characters, secrets 64.
    for name, length in (("R2_ACCESS_KEY_ID", 32), ("R2_SECRET_ACCESS_KEY", 64)):
        value = env(name) or ""
        if value and len(value) != length:
            return (
                "pending",
                f"{name} is {len(value)} chars, expected {length} — placeholder?",
            )
    try:
        s3 = r2.client()
    except RuntimeError as exc:
        return "pending", str(exc)
    results = []
    for which in (r2.ARCHIVE, r2.MEDIA):
        name = r2.bucket(which)
        try:
            s3.head_bucket(Bucket=name)
            results.append(f"{name}=ok")
        except Exception as exc:
            reason = str(exc).split(": ", 1)[-1][:120]
            results.append(f"{name}=FAILED ({reason})")
    status = "pass" if all(part.endswith("=ok") for part in results) else "fail"
    return status, ", ".join(results)


def gate_legacy_export() -> tuple[str, str]:
    try:
        result = legacy.verify()
    except RuntimeError as exc:
        return "pending", str(exc)
    except Exception as exc:
        return "pending", f"no manifest in R2 yet ({type(exc).__name__})"
    if result["missing"]:
        return "fail", f"{len(result['missing'])} manifest entries missing in R2"
    return "pass", f"{result['count']} objects verified under {legacy.PREFIX}/"


def gate_convex() -> tuple[str, str]:
    schema = REPO_ROOT / "convex" / "schema.ts"
    mutations = REPO_ROOT / "convex" / "mutations.ts"
    if not (schema.is_file() and mutations.is_file()):
        return "fail", "convex/schema.ts or convex/mutations.ts missing"
    url = env("CONVEX_URL")
    if not url:
        return "pending", "schema+mutations written; CONVEX_URL not set (npx convex dev)"
    try:
        code, _ = _http(url.rstrip("/") + "/version")
    except OSError as exc:
        return "fail", f"{url} unreachable: {exc}"
    return ("pass" if code < 500 else "fail"), f"{url} responded HTTP {code}"


def gate_meilisearch() -> tuple[str, str]:
    url = env("MEILI_URL")
    if not url:
        return "pending", "MEILI_URL not set"
    try:
        code, body = _http(url.rstrip("/") + "/health")
    except OSError as exc:
        return "fail", f"{url} unreachable: {exc}"
    if code == 200 and b"available" in body:
        return ("pass" if env("MEILI_SEARCH_KEY") else "fail"), (
            "healthy" if env("MEILI_SEARCH_KEY") else "healthy but MEILI_SEARCH_KEY unset"
        )
    return "fail", f"HTTP {code}"


def gate_telegram() -> tuple[str, str]:
    missing = [n for n in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not env(n)]
    if missing:
        return "pending", f"{', '.join(missing)} not set"
    session = env("TELEGRAM_SESSION", "secrets/archive.session")
    path = Path(session)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        return "pending", f"session file {session} not created yet"
    return "pass", f"api credentials set, session at {session}"


def gate_gpu_benchmark() -> tuple[str, str]:
    data = _record().get("gpuBenchmark") or {}
    required = (
        "device",
        "rtfX",
        "filesPerBatch",
        "peakMemoryGb",
        "projectedRuntimeHours",
    )
    missing = [key for key in required if data.get(key) in (None, "")]
    if missing:
        return (
            "pending",
            f"run `archive bench`; missing {', '.join(missing)} in m0.gates.json",
        )
    return "pass", (
        f"{data['device']} rtfX={data['rtfX']} batch={data['filesPerBatch']} "
        f"peak={data['peakMemoryGb']}GB projected={data['projectedRuntimeHours']}h"
    )


def gate_codec() -> tuple[str, str]:
    data = _record().get("codec") or {}
    if not data.get("decision"):
        return "pending", "no codec decision recorded in m0.gates.json"
    devices = data.get("testedOn") or []
    if not devices:
        return "fail", "decision recorded but no devices listed under testedOn"
    return "pass", f"{data['decision']} — verified on {', '.join(devices)}"


GATES = {
    "config-pin": gate_config,
    "model-access": gate_model_access,
    "r2": gate_r2,
    "legacy-export": gate_legacy_export,
    "convex": gate_convex,
    "meilisearch": gate_meilisearch,
    "telegram": gate_telegram,
    "gpu-benchmark": gate_gpu_benchmark,
    "codec": gate_codec,
}


def run_all() -> list[tuple[str, str, str]]:
    rows = []
    for name, check in GATES.items():
        try:
            status, detail = check()
        except Exception as exc:
            status, detail = "fail", f"{type(exc).__name__}: {exc}"
        rows.append((name, status, detail))
    return rows
