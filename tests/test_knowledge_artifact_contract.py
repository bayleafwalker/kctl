import hashlib
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs/protocols/knowledge-artifact-v1.schema.json"
FIXTURE_PATH = ROOT / "verification/examples/knowledge-artifact-v1.ndjson"
CONTEXT_PATH = ROOT / "verification/contexts/knowledge-artifact.json"


def _canonical_content(record: dict) -> bytes:
    return json.dumps(
        {"body": record["body"], "title": record["title"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_knowledge_artifact_schema_declares_strict_published_record():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "knowledge-artifact/v1"
    assert schema["properties"]["status"]["const"] == "published"
    assert schema["properties"]["stream"]["enum"] == ["durable", "coordination"]
    assert schema["properties"]["source"]["additionalProperties"] is False


def test_knowledge_artifact_fixture_has_stable_identity_digest_and_source_ref():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert set(record) == set(schema["required"])
    assert set(record["source"]) == set(schema["properties"]["source"]["required"])
    assert record["schema_version"] == "knowledge-artifact/v1"
    assert record["status"] == "published"
    assert record["stream"] == "durable"
    assert record["source"]["event_ref"] == f"sprintctl:event:{record['source']['event_id']}"
    assert record["content_digest"] == "sha256:" + hashlib.sha256(
        _canonical_content(record)
    ).hexdigest()
    assert record["superseded_by"] is None
    for field in ("published_at", "rendered_at"):
        assert datetime.fromisoformat(record[field].replace("Z", "+00:00")).tzinfo


def test_knowledge_artifact_context_is_a_depth_zero_projection_contract():
    context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))

    assert context["schema_version"] == "test-context/v1"
    assert context["id"] == "kctl.knowledge.artifact"
    assert context["depth"] == 0
    assert "interrupted-before-atomic-replace" in context["faults"]
    assert context["contract_ref"]["revision"] == "sha256:" + hashlib.sha256(
        (ROOT / "docs/protocols/knowledge-artifact-v1.md").read_bytes()
    ).hexdigest()
