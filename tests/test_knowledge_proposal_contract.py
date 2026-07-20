import hashlib
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs/protocols/knowledge-proposal-v1.schema.json"
FIXTURE_PATH = ROOT / "verification/examples/knowledge-proposal-v1.ndjson"
CONTEXT_PATH = ROOT / "verification/contexts/knowledge-proposal.json"


def _canonical_content(record: dict) -> bytes:
    return json.dumps(
        {"detail": record["detail"], "summary": record["summary"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_knowledge_proposal_schema_declares_strict_approved_record():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "knowledge-proposal/v1"
    assert schema["properties"]["status"]["const"] == "approved"
    assert schema["properties"]["stream"]["enum"] == ["durable", "coordination"]
    assert schema["properties"]["provenance"]["additionalProperties"] is False
    # This artifact never claims sprintctl item creation or a mutation surface.
    assert "sprintctl_item_id" not in schema["properties"]
    assert "accepted" not in schema["properties"]


def test_knowledge_proposal_fixture_has_stable_identity_digest_and_source_ref():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert set(record) == set(schema["required"])
    assert set(record["provenance"]) == set(schema["properties"]["provenance"]["required"])
    assert record["schema_version"] == "knowledge-proposal/v1"
    assert record["status"] == "approved"
    assert record["stream"] == "durable"
    assert record["provenance"]["event_ref"] == (
        f"sprintctl:event:{record['provenance']['event_id']}"
    )
    assert record["content_digest"] == "sha256:" + hashlib.sha256(
        _canonical_content(record)
    ).hexdigest()
    assert record["suggested_next_action"] == (
        f"propose sprintctl item add in repo {record['suggested_owner_repo']}"
    )
    for field in ("extracted_at", "rendered_at"):
        assert datetime.fromisoformat(record[field].replace("Z", "+00:00")).tzinfo


def test_knowledge_proposal_context_is_a_depth_zero_projection_contract():
    context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))

    assert context["schema_version"] == "test-context/v1"
    assert context["id"] == "kctl.knowledge.proposal"
    assert context["depth"] == 0
    assert "interrupted-before-atomic-replace" in context["faults"]
    assert "proposal-never-implies-a-sprintctl-item-was-created" in context["invariants"]
    assert context["contract_ref"]["revision"] == "sha256:" + hashlib.sha256(
        (ROOT / "docs/protocols/knowledge-proposal-v1.md").read_bytes()
    ).hexdigest()
