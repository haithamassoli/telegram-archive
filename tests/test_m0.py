"""M0 checks. Run: `python tests/test_m0.py` (also collectable by pytest)."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive import cli, config, gates, legacy

REPO = Path(__file__).resolve().parents[1]

# The value M0 pins. If this changes, every part transcript in the archive is
# a different identity — that is the whole point of the assertion.
EXPECTED_CONFIG_HASH = "d27d1fb0a633fb8273f793655bd8ef82e6100da90f63340d1f9bb16c609bc4d5"


def test_config_hash_is_pinned():
    assert config.CONFIG_HASH == EXPECTED_CONFIG_HASH
    record = json.loads((REPO / "m0.gates.json").read_text())
    assert record["configHash"] == EXPECTED_CONFIG_HASH


def test_canonical_json_is_key_order_independent():
    a = dict(config.PINNED_CONFIG)
    b = {key: a[key] for key in reversed(list(a))}
    assert config.canonical_json(a) == config.canonical_json(b)
    assert config.config_hash(a) == config.config_hash(b)


def test_config_hash_changes_with_any_pinned_field():
    for key in config.PINNED_CONFIG:
        changed = dict(config.PINNED_CONFIG)
        changed[key] = "different" if not isinstance(changed[key], bool) else False
        assert config.config_hash(changed) != config.CONFIG_HASH, key


def test_pinned_revision_matches_the_vendored_package():
    source = (
        REPO / "cohere-transcribe/src/cohere_transcribe/model_identity.py"
    ).read_text()
    model = re.search(r'DEFAULT_ASR_MODEL_ID = "([^"]+)"', source).group(1)
    revision = re.search(r'DEFAULT_ASR_MODEL_REVISION = "([^"]+)"', source).group(1)
    assert config.PINNED_CONFIG["model"] == model
    assert config.PINNED_CONFIG["modelRevision"] == revision
    assert re.fullmatch(r"[0-9a-f]{40}", revision), "revision must be a resolved commit"


def test_transcriber_kwargs_cover_the_pin():
    kwargs = config.transcriber_kwargs()
    assert set(kwargs) == {
        "model",
        "model_revision",
        "language",
        "vad",
        "vad_merge",
        "alignment",
    }
    assert kwargs["language"] == "ar" and kwargs["vad_merge"] is True


def test_load_env_strips_the_convex_trailing_comment():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env.local"
        path.write_text(
            "# Deployment used by `npx convex dev`\n"
            "CONVEX_DEPLOYMENT=dev:effervescent-mandrill-509 # team: x, project: y\n"
            "CONVEX_URL=https://effervescent-mandrill-509.eu-west-1.convex.cloud\n"
        )
        for key in ("CONVEX_DEPLOYMENT", "CONVEX_URL"):
            os.environ.pop(key, None)
        config.load_env(path)
        assert os.environ["CONVEX_DEPLOYMENT"] == "dev:effervescent-mandrill-509"
        assert os.environ["CONVEX_URL"].endswith(".convex.cloud")


def test_load_env_parses_and_never_overrides_the_shell():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(
            "# comment\n\n"
            "PLAIN=value\n"
            'QUOTED="quoted value"\n'
            "export EXPORTED=exported\n"
            "SHELL_WINS=from_file\n"
            "NO_EQUALS_LINE\n"
        )
        os.environ["SHELL_WINS"] = "from_shell"
        for key in ("PLAIN", "QUOTED", "EXPORTED"):
            os.environ.pop(key, None)
        config.load_env(path)
        assert os.environ["PLAIN"] == "value"
        assert os.environ["QUOTED"] == "quoted value"
        assert os.environ["EXPORTED"] == "exported"
        assert os.environ["SHELL_WINS"] == "from_shell"


class FakeS3:
    """Minimal stand-in for the boto3 S3 client used by the legacy export."""

    def __init__(self):
        self.objects: dict[str, dict] = {}

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[Key]

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        body = Path(filename).read_bytes()
        self.objects[key] = {
            "ContentLength": len(body),
            "Metadata": (ExtraArgs or {}).get("Metadata", {}),
            "Body": body,
        }

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = {"ContentLength": len(Body), "Metadata": {}, "Body": Body}

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.objects[Key]["Body"])}


def _sample_export(root: Path) -> None:
    (root / "transcripts").mkdir(parents=True)
    (root / "transcripts/a.json").write_text('{"text":"درس"}')
    (root / "transcripts/b.json").write_text('{"text":"two"}')
    (root / "query-logs.csv").write_text("q,count\ntest,1\n")


def test_legacy_export_uploads_verifies_and_resumes():
    s3 = FakeS3()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _sample_export(root)
        manifest = legacy.export(root, s3=s3, bucket="archive")

        assert manifest["count"] == 3
        assert manifest["uploaded"] == 3 and manifest["skipped"] == 0
        assert "legacy/assoli-v1/transcripts/a.json" in s3.objects
        assert "legacy/assoli-v1/manifest.json" in s3.objects
        # sha256 travels with the object so a resume can compare content.
        stored = s3.objects["legacy/assoli-v1/query-logs.csv"]["Metadata"]["sha256"]
        assert stored == manifest["files"][-1]["sha256"] or any(
            entry["sha256"] == stored for entry in manifest["files"]
        )

        # Second run is a no-op — everything already present with the same digest.
        again = legacy.export(root, s3=s3, bucket="archive")
        assert again["uploaded"] == 0 and again["skipped"] == 3

        # An edited file re-uploads.
        (root / "query-logs.csv").write_text("q,count\ntest,2\n")
        third = legacy.export(root, s3=s3, bucket="archive")
        assert third["uploaded"] == 1 and third["skipped"] == 2

        assert legacy.verify(s3=s3, bucket="archive") == {"count": 3, "missing": []}
        del s3.objects["legacy/assoli-v1/transcripts/b.json"]
        assert legacy.verify(s3=s3, bucket="archive")["missing"] == ["transcripts/b.json"]


def test_legacy_export_honours_exclude_globs():
    s3 = FakeS3()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _sample_export(root)
        (root / "segments").mkdir()
        (root / "segments/vid1.json").write_text("{}")
        (root / "segments/nested").mkdir()
        (root / "segments/nested/vid2.json").write_text("{}")

        manifest = legacy.export(root, s3=s3, bucket="a", exclude=("segments/*",))
        assert manifest["count"] == 3 and manifest["excluded"] == 2
        assert not any(k.startswith("legacy/assoli-v1/segments/") for k in s3.objects)
        assert manifest["excludePatterns"] == ["segments/*"]


def test_legacy_export_rejects_a_missing_source():
    try:
        legacy.export(Path("/nonexistent/v1"), s3=FakeS3(), bucket="archive")
    except NotADirectoryError:
        return
    raise AssertionError("expected NotADirectoryError")


def _with_record(data: dict):
    tmp = Path(tempfile.mkdtemp()) / "m0.gates.json"
    tmp.write_text(json.dumps(data))
    return tmp


def test_config_gate_fails_when_the_pin_drifts_from_the_record():
    original = gates.RECORD_PATH
    try:
        gates.RECORD_PATH = _with_record({"configHash": "0" * 64})
        status, detail = gates.gate_config()
        assert status == "fail" and "changed" in detail
        gates.RECORD_PATH = _with_record({"configHash": config.CONFIG_HASH})
        assert gates.gate_config()[0] == "pass"
    finally:
        gates.RECORD_PATH = original


def test_codec_and_benchmark_gates_require_real_records():
    original = gates.RECORD_PATH
    try:
        gates.RECORD_PATH = _with_record({})
        assert gates.gate_codec()[0] == "pending"
        assert gates.gate_gpu_benchmark()[0] == "pending"

        gates.RECORD_PATH = _with_record({"codec": {"decision": "opus", "testedOn": []}})
        assert gates.gate_codec()[0] == "fail"  # decision without device evidence

        gates.RECORD_PATH = _with_record(
            {
                "codec": {"decision": "opus", "testedOn": ["iOS Safari", "desktop"]},
                "gpuBenchmark": {
                    "device": "mps",
                    "rtfX": 12.5,
                    "filesPerBatch": 8,
                    "peakMemoryGb": 9.1,
                    "projectedRuntimeHours": 40.0,
                },
            }
        )
        assert gates.gate_codec()[0] == "pass"
        assert gates.gate_gpu_benchmark()[0] == "pass"
    finally:
        gates.RECORD_PATH = original


def test_run_all_reports_every_gate_and_survives_a_broken_one():
    original = dict(gates.GATES)

    def boom():
        raise RuntimeError("gate exploded")

    try:
        gates.GATES.clear()
        gates.GATES.update({"fine": lambda: ("pass", "ok"), "boom": boom})
        rows = gates.run_all()
        assert [(name, status) for name, status, _ in rows] == [
            ("fine", "pass"),
            ("boom", "fail"),
        ]
        assert cli.main(["gates"]) == 1  # a failing gate blocks M0 exit

        gates.GATES.clear()
        gates.GATES.update({"fine": lambda: ("pass", "ok")})
        assert cli.main(["gates"]) == 0
        gates.GATES.update({"waiting": lambda: ("pending", "human")})
        assert cli.main(["gates"]) == 1  # pending is not done either
    finally:
        gates.GATES.clear()
        gates.GATES.update(original)


def test_cli_config_hash():
    assert cli.main(["config-hash"]) == 0


def test_gitignore_covers_the_secrets():
    ignored = (REPO / ".gitignore").read_text()
    for pattern in (".env", "*.session", "secrets/"):
        assert pattern in ignored, pattern


if __name__ == "__main__":
    failures = 0
    for name, test in sorted(vars().copy().items()):
        if not name.startswith("test_") or not callable(test):
            continue
        try:
            test()
            print(f"ok    {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
