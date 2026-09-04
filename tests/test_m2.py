"""M2 checks. Run: `python tests/test_m2.py` (also collectable by pytest).

The interesting M2 logic is not "does the model transcribe" — it is whether GPU
time is ever spent twice, whether a crash can leave a `done` row without its
artifact, and whether an artifact the pinned config did not produce can be filed
under that config's identity. All three are exercised against fakes: an
in-memory Convex that behaves like the deployed mutations, an in-memory R2, and
a transcriber that writes the same JSON shape cohere-transcribe publishes.
`test_fake_convex_matches_the_deployed_signatures` keeps the fakes honest.
"""

from __future__ import annotations

import io
import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive import pipeline, transcribe
from archive.config import CONFIG_HASH, PINNED_CONFIG

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeConvex:
    """The deployed mutations and queries, re-implemented over dicts."""

    def __init__(self):
        self.media: list[dict] = []
        self.transcripts: dict[tuple[str, str], dict] = {}
        self.failures: dict[tuple[str, str], dict] = {}
        self.runs: list[dict] = []
        self.calls: list[str] = []
        self._ids = 0

    def _new_id(self, table: str) -> str:
        self._ids += 1
        return f"{table}#{self._ids}"

    def mutation(self, path: str, **args):
        self.calls.append(path)
        return getattr(self, f"m_{path.split(':', 1)[1]}")(**args)

    def query(self, path: str, **args):
        self.calls.append(path)
        return getattr(self, f"q_{path.split(':', 1)[1]}")(**args)

    # -- mutations ----------------------------------------------------------
    def m_upsertPartTranscript(self, sha256, configHash, status, **rest):
        key = (sha256, configHash)
        existing = self.transcripts.get(key)
        patch = {"status": status, **rest}
        if status == "processing":
            patch["processingStartedAt"] = 1_000_000
        if existing is None:
            row = {
                "_id": self._new_id("partTranscripts"),
                "sha256": sha256,
                "configHash": configHash,
                "attempts": 1 if status == "processing" else 0,
                **patch,
            }
            self.transcripts[key] = row
            return {"id": row["_id"], "created": True}
        existing.update(patch)
        # Mirrors the deployed mutation: a `done` row keeps no stale error, and a
        # `failed` row keeps no pointer to an artifact that is not there.
        if status == "done":
            existing.pop("error", None)
        if status == "failed" and "rawR2Key" not in rest:
            existing.pop("rawR2Key", None)
        existing["attempts"] += 1 if status == "processing" else 0
        return {"id": existing["_id"], "created": False}

    def m_recordFailure(self, stage, refKey, error):
        row = self.failures.setdefault((stage, refKey), {"attempts": 0})
        row.update(
            stage=stage,
            refKey=refKey,
            error=error,
            attempts=row["attempts"] + 1,
            resolved=False,
        )
        return {"attempts": row["attempts"]}

    def m_resolveFailure(self, stage, refKey):
        row = self.failures.get((stage, refKey))
        if row is None:
            return {"resolved": False}
        row["resolved"] = True
        return {"resolved": True}

    def m_startPipelineRun(self, runId, stage):
        self.runs.append({"runId": runId, "stage": stage, "status": "running"})
        return {"id": runId}

    def m_finishPipelineRun(self, runId, **counts):
        run = next(r for r in self.runs if r["runId"] == runId)
        run.update(counts)
        return {"id": runId}

    def m_acquirePipelineStage(self, stage, runId, owner):
        return {"acquired": True, "holder": runId}

    def m_heartbeatPipelineStage(self, stage, runId):
        return {"held": True}

    def m_releasePipelineStage(self, stage, runId):
        return {"released": True}

    # -- queries ------------------------------------------------------------
    def q_mediaObjectsPage(self, configHash, cursor, limit):
        rows = sorted(
            (row for row in self.media if row["sha256"] > cursor),
            key=lambda row: row["sha256"],
        )[:limit]
        objects = []
        for row in rows:
            found = self.transcripts.get((row["sha256"], configHash))
            objects.append(
                {
                    "sha256": row["sha256"],
                    "r2Key": row["r2Key"],
                    "ext": row["ext"],
                    "mimeType": row.get("mimeType"),
                    "durationMs": row.get("durationMs"),
                    "sizeBytes": row.get("sizeBytes", 1),
                    "status": found["status"] if found else None,
                    "processingStartedAt": (found or {}).get("processingStartedAt"),
                    "attempts": (found or {}).get("attempts", 0),
                    "rawR2Key": (found or {}).get("rawR2Key"),
                }
            )
        return {
            "objects": objects,
            "nextCursor": rows[-1]["sha256"] if rows else None,
        }

    def q_unresolvedFailures(self, stage):
        return [
            row
            for (s, _), row in self.failures.items()
            if s == stage and not row["resolved"]
        ]


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_put_for: set[str] = set()

    def put_object(self, Bucket, Key, Body, **kw):
        if any(token in Key for token in self.fail_put_for):
            raise OSError(f"scripted upload failure for {Key}")
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        Path(Filename).write_bytes(self.objects[Key])

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


def artifact(duration=60.0, segments=3, **overrides) -> dict:
    """The JSON cohere-transcribe publishes, in the shape M2 reads it."""
    data = {
        "schema_version": 8,
        "source": {"path": "/tmp/x.mp3", "duration_seconds": duration},
        "language": PINNED_CONFIG["language"],
        "segmentation": PINNED_CONFIG["vad"],
        "segmentation_details": {"merge": PINNED_CONFIG["vadMerge"]},
        "timing": PINNED_CONFIG["alignment"],
        "models": {
            "asr": {
                "id": PINNED_CONFIG["model"],
                "revision": PINNED_CONFIG["modelRevision"],
            }
        },
        "transcript": "نص",
        "segments": [
            {"segment_index": i, "start": i, "end": i + 1, "text": "نص"}
            for i in range(segments)
        ],
    }
    data.update(overrides)
    return data


@dataclass
class FakeResult:
    path: str
    status: str = "completed"
    error: str | None = None


@dataclass
class FakeRun:
    results: list


class FakeTranscriber:
    """Publishes an artifact per input, and counts how often it was asked."""

    def __init__(self, fail: set[str] = frozenset(), body=None):
        self.batches: list[list[str]] = []
        self.fail = set(fail)
        self.body = body or (lambda sha: artifact())
        self.closed = False

    def transcribe(self, paths):
        self.batches.append(list(paths))
        results = []
        for path in paths:
            sha = Path(path).stem
            if sha in self.fail:
                results.append(FakeResult(path, "failed", "scripted ASR failure"))
                continue
            out = transcribe.OUT_DIR / f"{sha}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(self.body(sha), ensure_ascii=False))
            (transcribe.OUT_DIR / f".{sha}.manifest.json").write_text("{}")
            results.append(FakeResult(path))
        return FakeRun(results)

    def close(self):
        self.closed = True


@dataclass
class World:
    convex: FakeConvex = field(default_factory=FakeConvex)
    s3: FakeS3 = field(default_factory=FakeS3)
    transcriber: FakeTranscriber | None = None
    loads: int = 0

    def blob(self, sha, ext="mp3", mime="audio/mpeg", size=10):
        key = f"blobs/{sha[:2]}/{sha}.{ext}"
        self.s3.objects[key] = b"BINARY" * size
        self.convex.media.append(
            {"sha256": sha, "r2Key": key, "ext": ext, "mimeType": mime, "sizeBytes": 60}
        )
        return sha

    def publish(self, sha, data=None):
        """Put a transcript artifact in R2 behind Convex's back (a crash window)."""
        self.s3.objects[transcribe.transcript_key(sha)] = json.dumps(
            data or artifact()
        ).encode()

    def run(self, **kwargs):
        transcriber = self.transcriber or FakeTranscriber()
        self.transcriber = transcriber

        def open_transcriber():
            self.loads += 1
            return transcriber

        fake_r2 = types.SimpleNamespace(
            client=lambda: self.s3,
            bucket=lambda which="archive": "bucket",
            list_keys=lambda s3, bucket, prefix: {
                k for k in self.s3.objects if k.startswith(prefix)
            },
            head=lambda s3, bucket, key: (
                {"ContentLength": len(self.s3.objects[key])}
                if key in self.s3.objects
                else None
            ),
        )
        saved = {
            "convex": transcribe.convex,
            "r2": transcribe.r2,
            "open_transcriber": transcribe.open_transcriber,
        }
        transcribe.convex = self.convex
        transcribe.r2 = fake_r2
        transcribe.open_transcriber = open_transcriber
        pipeline_convex = pipeline.convex
        pipeline.convex = self.convex
        try:
            kwargs.setdefault("log", lambda *a: None)
            return transcribe.transcribe(**kwargs)
        finally:
            for name, value in saved.items():
                setattr(transcribe, name, value)
            pipeline.convex = pipeline_convex

    def reconcile(self, **kwargs):
        saved = (transcribe.convex, transcribe.r2)
        transcribe.convex = self.convex
        transcribe.r2 = types.SimpleNamespace(
            client=lambda: self.s3,
            bucket=lambda which="archive": "bucket",
            list_keys=lambda s3, bucket, prefix: {
                k for k in self.s3.objects if k.startswith(prefix)
            },
        )
        try:
            kwargs.setdefault("log", lambda *a: None)
            return transcribe.reconcile(**kwargs)
        finally:
            transcribe.convex, transcribe.r2 = saved


SHA = {name: name[0] * 64 for name in ("a", "b", "c", "d")}


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_is_audio_uses_the_mime_type_then_the_extension():
    assert transcribe.is_audio({"mimeType": "audio/mpeg", "ext": "bin"})
    assert transcribe.is_audio({"mimeType": "audio/ogg", "ext": "oga"})
    assert not transcribe.is_audio({"mimeType": "video/mp4", "ext": "mp4"})
    assert not transcribe.is_audio({"mimeType": "image/jpeg", "ext": "jpg"})
    # Telegram sometimes hands over a binary with no mime type at all.
    assert transcribe.is_audio({"mimeType": None, "ext": "m4a"})
    assert not transcribe.is_audio({"mimeType": None, "ext": "pdf"})


def test_transcript_key_is_sha_then_config():
    key = transcribe.transcript_key(SHA["a"], "cfg")
    assert key == f"transcripts/{SHA['a']}/cfg.json"
    assert transcribe.key_sha256(key) == SHA["a"]
    assert transcribe.key_sha256("blobs/aa/x.mp3") is None


def test_validate_artifact_reads_the_facts_and_rejects_config_drift():
    facts = transcribe.validate_artifact(artifact(duration=90.5, segments=4))
    assert facts["durationMs"] == 90500 and facts["segmentCount"] == 4
    assert facts["model"] == PINNED_CONFIG["model"]

    for broken in (
        artifact(language="en"),
        artifact(timing="word"),
        artifact(segmentation_details={"merge": False}),
        artifact(models={"asr": {"id": "someone/else", "revision": "x"}}),
    ):
        try:
            transcribe.validate_artifact(broken)
            raise AssertionError("expected the drift to be rejected")
        except transcribe.ProvenanceMismatch:
            pass


def test_needs_work_skips_done_and_claims_a_stale_processing_row():
    now = 10 * transcribe.STALE_PROCESSING_MS
    assert not transcribe.needs_work({"status": "done"}, now)
    assert transcribe.needs_work({"status": None}, now)
    assert transcribe.needs_work({"status": "failed"}, now)
    fresh = {"status": "processing", "processingStartedAt": now - 1000}
    stale = {"status": "processing", "processingStartedAt": now - 10 * 60 * 1000}
    assert not transcribe.needs_work(fresh, now)
    assert transcribe.needs_work(stale, now)


def test_a_full_run_transcribes_every_audio_binary_exactly_once():
    world = World()
    world.blob(SHA["a"])
    world.blob(SHA["b"], ext="oga", mime="audio/ogg")
    world.blob(SHA["c"], ext="mp4", mime="video/mp4")  # out of scope

    result = world.run()
    assert result["audio"] == 2 and result["notAudio"] == 1
    assert result["transcribed"] == 2 and result["failed"] == 0
    assert result["coverageNow"] == 1.0

    for sha in (SHA["a"], SHA["b"]):
        row = world.convex.transcripts[(sha, CONFIG_HASH)]
        assert row["status"] == "done"
        assert row["rawR2Key"] == transcribe.transcript_key(sha)
        assert row["segmentCount"] == 3 and row["durationMs"] == 60000
        assert row["modelRevision"] == PINNED_CONFIG["modelRevision"]
        assert transcribe.transcript_key(sha) in world.s3.objects
    assert transcribe.transcript_key(SHA["c"]) not in world.s3.objects
    # One transcriber call for the batch, not one per file.
    assert len(world.transcriber.batches) == 1
    assert world.transcriber.closed
    assert not list(transcribe.TMP_DIR.glob("*")), "scratch outlived the run"


def test_a_rerun_is_a_no_op_and_never_loads_the_model():
    world = World()
    world.blob(SHA["a"])
    world.run()
    assert world.loads == 1

    result = world.run()
    assert result["candidates"] == 0 and result["transcribed"] == 0
    assert result["doneNow"] == 1
    assert world.loads == 1, "a rerun loaded the 2B model for nothing"
    assert len(world.transcriber.batches) == 1


def test_the_crash_window_recovers_an_r2_artifact_without_inference():
    """§4.3: the run died between the upload and the Convex write. The artifact
    is deterministic, so the next run finds it, validates it and promotes it."""
    world = World()
    world.blob(SHA["a"])
    world.publish(SHA["a"], artifact(duration=120.0, segments=7))

    result = world.run()
    assert result["recovered"] == 1 and result["transcribed"] == 0
    assert world.loads == 0, "the model was loaded for an artifact we already had"
    row = world.convex.transcripts[(SHA["a"], CONFIG_HASH)]
    assert row["status"] == "done" and row["segmentCount"] == 7
    assert row["durationMs"] == 120000


def test_a_recovered_artifact_from_another_config_is_redone_not_trusted():
    world = World()
    world.blob(SHA["a"])
    world.publish(SHA["a"], artifact(language="en"))
    result = world.run()
    assert result["recovered"] == 0 and result["transcribed"] == 1
    assert world.convex.transcripts[(SHA["a"], CONFIG_HASH)]["status"] == "done"
    # The bad artifact was overwritten by one the pinned config produced.
    stored = json.loads(world.s3.objects[transcribe.transcript_key(SHA["a"])])
    assert stored["language"] == "ar"


def test_a_failed_upload_never_leaves_a_done_row():
    """Write-order law §4.1: R2 first, Convex second."""
    world = World()
    world.blob(SHA["a"])
    world.s3.fail_put_for = {"transcripts/"}
    result = world.run()
    assert result["transcribed"] == 0 and result["failed"] == 1
    assert world.convex.transcripts[(SHA["a"], CONFIG_HASH)]["status"] == "failed"
    assert transcribe.transcript_key(SHA["a"]) not in world.s3.objects
    assert world.convex.failures[("transcribe", SHA["a"])]["attempts"] == 1


def test_a_provenance_mismatch_is_a_failure_not_a_new_identity():
    """A transcript the pinned config did not produce must never be filed under
    that config's key — the identity is the config, not the file name."""
    world = World()
    world.blob(SHA["a"])
    world.transcriber = FakeTranscriber(
        body=lambda sha: artifact(models={"asr": {"id": "other/model", "revision": "z"}})
    )
    result = world.run()
    assert result["failed"] == 1 and result["transcribed"] == 0
    assert transcribe.transcript_key(SHA["a"]) not in world.s3.objects
    row = world.convex.transcripts[(SHA["a"], CONFIG_HASH)]
    assert row["status"] == "failed" and "other/model" in row["error"]
    assert len(world.convex.transcripts) == 1, "a mismatch minted a second identity"


def test_a_failure_is_drained_first_on_the_next_run_and_capped():
    world = World()
    world.blob(SHA["a"])
    world.blob(SHA["b"])
    world.transcriber = FakeTranscriber(fail={SHA["a"]})
    result = world.run()
    assert result["failed"] == 1 and result["transcribed"] == 1
    assert not world.convex.failures[("transcribe", SHA["a"])]["resolved"]

    # Next run: the failure is retried, and it is retried first.
    world.transcriber = FakeTranscriber()
    result = world.run()
    assert result["transcribed"] == 1 and result["candidates"] == 1
    assert world.transcriber.batches[0][0].endswith(f"{SHA['a']}.mp3")
    assert world.convex.failures[("transcribe", SHA["a"])]["resolved"] is True
    # The row that succeeded on retry keeps no trace of the failure.
    assert "error" not in world.convex.transcripts[(SHA["a"], CONFIG_HASH)]

    # And a failure past the cap is left alone for a human.
    world.convex.failures[("transcribe", SHA["b"])] = {
        "stage": "transcribe",
        "refKey": SHA["b"],
        "attempts": transcribe.FAILURE_ATTEMPT_CAP,
        "resolved": False,
        "error": "x",
    }
    world.convex.transcripts.pop((SHA["b"], CONFIG_HASH))
    world.transcriber = FakeTranscriber()
    result = world.run()
    assert result["candidates"] == 0, "a capped failure was retried anyway"


def test_a_batch_that_blows_up_is_charged_to_its_files_not_swallowed():
    """A run-level error blames no single file — but recording nothing would
    re-select this exact batch forever, at zero progress."""

    class Exploding(FakeTranscriber):
        def transcribe(self, paths):
            raise RuntimeError("MPS backend out of memory")

    world = World()
    world.blob(SHA["a"])
    world.blob(SHA["b"])
    world.transcriber = Exploding()
    result = world.run()
    assert result["failed"] == 2 and result["transcribed"] == 0
    for sha in (SHA["a"], SHA["b"]):
        assert world.convex.failures[("transcribe", sha)]["attempts"] == 1
        assert world.convex.transcripts[(sha, CONFIG_HASH)]["status"] == "failed"
    assert not list(transcribe.TMP_DIR.glob("*.mp3")), "scratch outlived the crash"


def test_limit_and_sha256_narrow_the_run():
    world = World()
    for name in ("a", "b", "c"):
        world.blob(SHA[name])
    assert world.run(limit=1)["transcribed"] == 1
    assert world.run(sha256s=(SHA["c"],))["transcribed"] == 1
    assert world.convex.transcripts.keys() == {
        (SHA["a"], CONFIG_HASH),
        (SHA["c"], CONFIG_HASH),
    }


def test_batches_are_capped_at_the_batch_size():
    world = World()
    for name in ("a", "b", "c", "d"):
        world.blob(SHA[name])
    world.run(batch_size=3)
    assert [len(batch) for batch in world.transcriber.batches] == [3, 1]


def test_reconcile_flags_missing_repairs_present_and_only_reports_orphans():
    world = World()
    world.blob(SHA["a"])  # done in Convex, artifact deleted from R2
    world.blob(SHA["b"])  # artifact in R2, Convex never heard about it
    world.convex.m_upsertPartTranscript(
        sha256=SHA["a"],
        configHash=CONFIG_HASH,
        status="done",
        rawR2Key=transcribe.transcript_key(SHA["a"]),
    )
    world.publish(SHA["b"])
    world.convex.m_recordFailure("transcribe", SHA["b"], "an older run gave up")
    orphan = transcribe.transcript_key(SHA["d"])  # no mediaObject references it
    world.s3.objects[orphan] = json.dumps(artifact()).encode()
    superseded = f"transcripts/{SHA['a']}/older-config.json"
    world.s3.objects[superseded] = json.dumps(artifact()).encode()

    dry = world.reconcile(dry_run=True)
    assert dry["missing"] == [SHA["a"]] and dry["repaired"] == [SHA["b"]]
    assert dry["orphans"] == [orphan] and dry["superseded"] == [superseded]
    assert world.convex.transcripts[(SHA["a"], CONFIG_HASH)]["status"] == "done"
    assert (SHA["b"], CONFIG_HASH) not in world.convex.transcripts

    result = world.reconcile()
    assert result["missing"] == [SHA["a"]] and result["repaired"] == [SHA["b"]]
    assert world.convex.transcripts[(SHA["a"], CONFIG_HASH)]["status"] == "failed"
    assert world.convex.failures[("transcribe", SHA["a"])]["attempts"] == 1
    assert world.convex.transcripts[(SHA["b"], CONFIG_HASH)]["status"] == "done"
    # A repaired sha256 must not keep haunting the ops page.
    assert world.convex.failures[("transcribe", SHA["b"])]["resolved"] is True
    # Archival objects are never deleted, only reported.
    assert orphan in world.s3.objects and superseded in world.s3.objects

    # The repair is what a follow-up run sees: nothing left to do.
    assert world.run()["candidates"] == 1  # only the one marked for redo


def test_reconcile_reports_an_artifact_it_cannot_validate():
    world = World()
    world.blob(SHA["a"])
    world.publish(SHA["a"], artifact(timing="word"))
    result = world.reconcile()
    assert [sha for sha, _ in result["invalid"]] == [SHA["a"]]
    assert (SHA["a"], CONFIG_HASH) not in world.convex.transcripts


def test_coverage_is_the_m2_exit_criterion():
    world = World()
    world.blob(SHA["a"])
    world.blob(SHA["b"])
    world.blob(SHA["c"], ext="jpg", mime="image/jpeg")
    world.run(sha256s=(SHA["a"],))
    stats = transcribe.coverage(
        world.convex.q_mediaObjectsPage(CONFIG_HASH, "", 100)["objects"]
    )
    assert stats == {
        "objects": 3,
        "audio": 2,
        "notAudio": 1,
        "done": 1,
        "coverage": 0.5,
        "configHash": CONFIG_HASH,
    }


def test_fake_convex_matches_the_deployed_signatures():
    """The fakes above are only worth something if they mirror the real thing."""
    import inspect

    source = (REPO / "convex" / "mutations.ts").read_text()
    source += (REPO / "convex" / "queries.ts").read_text()
    exported = set(re.findall(r"export const (\w+) = (?:mutation|query)\(", source))
    fake = FakeConvex()
    used = {
        "upsertPartTranscript",
        "recordFailure",
        "resolveFailure",
        "startPipelineRun",
        "finishPipelineRun",
        "acquirePipelineStage",
        "heartbeatPipelineStage",
        "releasePipelineStage",
        "mediaObjectsPage",
        "unresolvedFailures",
    }
    assert used <= exported, f"fakes reference undeployed functions: {used - exported}"
    for name in used:
        method = getattr(fake, f"m_{name}", None) or getattr(fake, f"q_{name}")
        body = re.search(
            rf"export const {name} = (?:mutation|query)\(\{{\s*args: \{{(.*?)\n  \}},",
            source,
            re.S,
        )
        if body is None:
            continue
        declared = set(re.findall(r"(\w+):\s*v\.", body.group(1)))
        params = {
            p
            for p in inspect.signature(method).parameters
            if p not in ("self", "rest", "counts", "kw")
        }
        assert params <= declared, f"{name}: fake takes {params - declared}"

    # Every field M2 writes has to exist in the schema, or the mutation throws.
    schema = (REPO / "convex" / "schema.ts").read_text()
    table = re.search(r"partTranscripts: defineTable\(\{(.*?)\n  \}\)", schema, re.S)
    fields = set(re.findall(r"(\w+): v\.", table.group(1)))
    assert {"rawR2Key", "durationMs", "segmentCount", "model", "modelRevision"} <= fields


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
