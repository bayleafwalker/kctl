import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_knowledge_context_records_restart_and_publication_faults():
    packet = json.loads(
        (ROOT / "verification/contexts/knowledge-lifecycle.json").read_text(encoding="utf-8")
    )

    assert packet["schema_version"] == "test-context/v1"
    assert packet["depth"] == 1
    assert "crash-after-candidate-commit-before-watermark" in packet["faults"]
    assert "crash-after-entry-insert-before-candidate-transition" in packet["faults"]
    assert packet["contract_ref"]["revision"] == "sha256:" + hashlib.sha256(
        (ROOT / "docs/protocols/knowledge-lifecycle.md").read_bytes()
    ).hexdigest()


def test_protocol_document_does_not_overclaim_publication_atomicity():
    protocol = (ROOT / "docs/protocols/knowledge-lifecycle.md").read_text(encoding="utf-8")

    assert "These writes currently commit separately" in protocol
    assert "Retrying publication is not guaranteed idempotent" in protocol


def test_protocol_document_keeps_artifact_export_read_only():
    protocol = (ROOT / "docs/protocols/knowledge-lifecycle.md").read_text(encoding="utf-8")

    assert "knowledge-artifact/v1 exporter writes a complete NDJSON projection" in protocol
    assert "It is read-only: it does not\nadvance a watermark, transition a candidate" in protocol
