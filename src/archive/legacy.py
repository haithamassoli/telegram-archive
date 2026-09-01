"""One-time assoli-v1 export into R2 `legacy/assoli-v1/` (plan §8.1).

v1 stays live and untouched: this only reads a local export directory and
copies it up. Re-runnable — an object already present with the same sha256 is
skipped, so a killed run resumes for free.
"""

from __future__ import annotations

import json
import time
from fnmatch import fnmatch
from pathlib import Path

from . import r2

PREFIX = "legacy/assoli-v1"
SCHEMA_VERSION = 1


def export(
    source: Path,
    s3=None,
    bucket: str | None = None,
    prefix: str = PREFIX,
    exclude: tuple[str, ...] = (),
) -> dict:
    """Upload every file under `source` and write a manifest. Returns the manifest.

    `exclude` holds glob patterns matched against each file's path relative to
    `source`, so `segments/*` drops a whole subtree.
    """
    if not source.is_dir():
        raise NotADirectoryError(f"{source} is not a directory")
    s3 = r2.client() if s3 is None else s3
    bucket = r2.bucket() if bucket is None else bucket

    files, uploaded, skipped, excluded = [], 0, 0, 0
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = path.relative_to(source).as_posix()
        if any(fnmatch(rel, pattern) for pattern in exclude):
            excluded += 1
            continue
        key = f"{prefix}/{rel}"
        digest = r2.sha256_file(path)
        size = path.stat().st_size
        existing = r2.head(s3, bucket, key)
        if existing and existing.get("Metadata", {}).get("sha256") == digest:
            skipped += 1
        else:
            s3.upload_file(
                str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}}
            )
            uploaded += 1
        files.append({"path": rel, "sha256": digest, "sizeBytes": size})

    # Verify every key landed before the manifest claims it did (write-order law §4.1).
    for entry in files:
        if r2.head(s3, bucket, f"{prefix}/{entry['path']}") is None:
            raise RuntimeError(f"upload verification failed for {entry['path']}")

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": int(time.time() * 1000),
        "count": len(files),
        "totalBytes": sum(f["sizeBytes"] for f in files),
        "uploaded": uploaded,
        "skipped": skipped,
        "excluded": excluded,
        "excludePatterns": list(exclude),
        "files": files,
    }
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/manifest.json",
        Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return manifest


def verify(s3=None, bucket: str | None = None, prefix: str = PREFIX) -> dict:
    """Re-read the manifest from R2 and confirm every listed object exists."""
    s3 = r2.client() if s3 is None else s3
    bucket = r2.bucket() if bucket is None else bucket
    body = s3.get_object(Bucket=bucket, Key=f"{prefix}/manifest.json")["Body"].read()
    manifest = json.loads(body)
    missing = [
        entry["path"]
        for entry in manifest["files"]
        if r2.head(s3, bucket, f"{prefix}/{entry['path']}") is None
    ]
    return {"count": manifest["count"], "missing": missing}
