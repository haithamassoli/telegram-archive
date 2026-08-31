"""Cloudflare R2 access. Thin wrapper over boto3's S3 client."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import require_env

ARCHIVE = "archive"
MEDIA = "media"


def client():
    import boto3

    creds = require_env("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=creds["R2_ENDPOINT"],
        aws_access_key_id=creds["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def bucket(which: str = ARCHIVE) -> str:
    name = "R2_ARCHIVE_BUCKET" if which == ARCHIVE else "R2_MEDIA_BUCKET"
    return require_env(name)[name]


def head(s3, bucket_name: str, key: str) -> dict | None:
    """Return the object's HEAD metadata, or None when it does not exist."""
    from botocore.exceptions import ClientError

    try:
        return s3.head_object(Bucket=bucket_name, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
