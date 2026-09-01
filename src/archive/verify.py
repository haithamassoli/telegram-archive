"""M1 exit criteria, as a command (plan Phase 1, "Validation").

Two questions, both answered against live Telegram rather than against our own
bookkeeping: does the archive hold as many messages as the channel says it has,
and does a random `telegramUrl` still point at the message we recorded?
"""

from __future__ import annotations

import itertools
import random

from . import convex, ingest, r2

SPOT_CHECKS = 20
# ponytail: Convex reads whole documents, and a 4096-char Arabic post is ~8 KB,
# so 500 rows a page stays well under the 8 MiB query read limit. Lower it if a
# channel ever carries bigger message bodies.
SCAN_PAGE = 500


def archived_ids(channel_id: str) -> tuple[list[int], int]:
    """Every archived message id for the channel, and how many carry a binary."""
    ids: list[int] = []
    with_media = 0
    cursor = 0
    while True:
        page = convex.query(
            "queries:channelScan",
            channelId=channel_id,
            fromId=cursor,
            limit=SCAN_PAGE,
        )
        ids.extend(page["ids"])
        with_media += page["withMedia"]
        if page["nextFromId"] is None or page["count"] < SCAN_PAGE:
            return ids, with_media
        cursor = page["nextFromId"]


def spot_check(client, entity, username: str, rows: list[dict]) -> list[dict]:
    """Re-fetch each recorded message from Telegram and compare what we stored."""
    results = []
    fetched = client.get_messages(entity, ids=[r["telegramMessageId"] for r in rows])
    for row, msg in zip(rows, fetched, strict=True):
        problems = []
        if msg is None:
            problems.append("gone from Telegram")
        else:
            expected = f"https://t.me/{username}/{msg.id}"
            if row["telegramUrl"] != expected:
                problems.append(f"url {row['telegramUrl']} != {expected}")
            if row["date"] != ingest._ms(msg.date):
                problems.append("date mismatch")
            live_type = ingest.media_type(msg)
            if row["mediaType"] != live_type:
                problems.append(f"mediaType {row['mediaType']} != {live_type}")
            if (msg.message or None) != (row.get("text") or None):
                problems.append("text mismatch")
        results.append(
            {
                "id": row["telegramMessageId"],
                "url": row["telegramUrl"],
                "problems": problems,
            }
        )
    return results


def verify_channel(
    client, s3, bucket: str, username: str, checks: int = SPOT_CHECKS
) -> dict:
    channel = convex.query("queries:channelByUsername", username=username)
    if channel is None:
        raise RuntimeError(f"{username} has never been synced")
    entity = client.get_entity(username)
    live_total = client.get_messages(entity, limit=0).total

    ids, with_media = archived_ids(channel["_id"])
    # Telegram counts service messages and skips deleted ids, so the archived
    # count is compared to the channel's own total rather than to max(id).
    gaps = [(a, b) for a, b in itertools.pairwise(ids) if b - a > 1]

    sample_ids = random.sample(ids, min(checks, len(ids))) if ids else []
    rows = convex.query(
        "queries:messagesByIds", channelId=channel["_id"], ids=sorted(sample_ids)
    )
    spots = spot_check(client, entity, username, rows) if rows else []

    manifest = None
    if r2.head(s3, bucket, ingest.manifest_key(username)):
        import json

        manifest = json.loads(
            s3.get_object(Bucket=bucket, Key=ingest.manifest_key(username))["Body"].read()
        )
    meta_keys = r2.list_keys(s3, bucket, f"meta/{username}/")
    missing_batches = (
        [b["key"] for b in manifest["batches"] if b["key"] not in meta_keys]
        if manifest
        else []
    )

    return {
        "channel": username,
        "liveTotal": live_total,
        "archived": len(ids),
        "withMedia": with_media,
        "checkpoint": channel["lastMessageId"],
        "idGaps": len(gaps),
        "metaBatches": len(manifest["batches"]) if manifest else 0,
        "metaMessages": manifest["count"] if manifest else 0,
        "missingBatches": missing_batches,
        "spotChecks": spots,
        "spotFailures": [s for s in spots if s["problems"]],
    }


def verify(channels: list[str], checks: int = SPOT_CHECKS) -> list[dict]:
    client, _ = ingest.open_client(takeout=False)
    s3, bucket = r2.client(), r2.bucket()
    try:
        return [verify_channel(client, s3, bucket, name, checks) for name in channels]
    finally:
        client.disconnect()
