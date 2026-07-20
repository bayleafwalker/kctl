import json

from kctl import db as _db
from kctl import proposal as _proposal
from kctl import publish as _publish
from kctl import review as _review
from kctl.cli import cli
from kctl.extract import DEFAULT_EVENT_TYPES, extract_candidates
from tests.conftest import add_event


NOW = "2026-07-17T18:00:00Z"
NOW2 = "2026-07-17T18:01:00Z"


def _seed_approved(sc_db_path, kctl_conn, *, event_type="decision", summary="Approved knowledge"):
    add_event(sc_db_path, event_type, {"summary": summary, "tags": ["ops"]})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()
    candidate = _db.list_candidates(kctl_conn, status="candidate")[-1]
    _review.approve_candidate(kctl_conn, candidate["id"], now=NOW2, reviewed_by="human-reviewer")
    return candidate["id"]


def _read_snapshot(root, repo_id):
    path = root / repo_id / "knowledge" / _proposal.PROPOSAL_FILENAME
    return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_cli_export_proposal_writes_approved_unpublished_candidates(
    sc_db_path, kctl_conn, runner, tmp_path
):
    durable_id = _seed_approved(sc_db_path, kctl_conn, summary="Durable approved knowledge")
    coordination_id = _seed_approved(
        sc_db_path,
        kctl_conn,
        event_type="claim-handoff",
        summary="Coordination approved knowledge",
    )

    result = runner.invoke(
        cli,
        ["export-proposal", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"],
    )

    assert result.exit_code == 0, result.output
    assert "Wrote 2 approved knowledge proposal(s)" in result.output
    assert "proposal only" in result.output

    path, records = _read_snapshot(tmp_path, "kctl")
    assert path.exists()
    assert [record["candidate_id"] for record in records] == sorted(
        record["candidate_id"] for record in records
    )
    assert {record["candidate_id"] for record in records} == {durable_id, coordination_id}
    assert {record["stream"] for record in records} == {"durable", "coordination"}

    for record in records:
        assert record["schema_version"] == "knowledge-proposal/v1"
        assert record["repo_id"] == "kctl"
        assert record["status"] == "approved"
        assert record["suggested_owner_repo"] == "kctl"
        assert record["suggested_next_action"] == "propose sprintctl item add in repo kctl"
        assert record["provenance"]["event_ref"] == (
            f"sprintctl:event:{record['provenance']['event_id']}"
        )
        assert record["content_digest"] == _proposal.proposal_digest(
            record["summary"], record["detail"]
        )
        # kctl never claims write access anywhere in the record.
        assert "sprintctl_item_id" not in record


def test_export_proposal_omits_published_and_rejected_and_pending_candidates(
    sc_db_path, kctl_conn, runner, tmp_path
):
    approved_id = _seed_approved(sc_db_path, kctl_conn, summary="Still approved")
    published_id = _seed_approved(sc_db_path, kctl_conn, summary="Already published")
    _publish.publish_candidate(
        kctl_conn,
        published_id,
        None,
        "Body",
        "decision",
        None,
        NOW2,
    )

    add_event(sc_db_path, "decision", {"summary": "Never reviewed"})
    add_event(sc_db_path, "decision", {"summary": "Will be rejected"})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()

    def _candidate_by_summary(summary: str) -> dict:
        matches = [
            c for c in _db.list_candidates(kctl_conn, status="candidate")
            if c["summary"] == summary
        ]
        assert len(matches) == 1
        return matches[0]

    pending_candidate = _candidate_by_summary("Never reviewed")
    rejected_candidate = _candidate_by_summary("Will be rejected")
    _review.reject_candidate(kctl_conn, rejected_candidate["id"], now=NOW2)

    result = runner.invoke(
        cli,
        ["export-proposal", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"],
    )

    assert result.exit_code == 0, result.output
    path, records = _read_snapshot(tmp_path, "kctl")
    assert {record["candidate_id"] for record in records} == {approved_id}
    assert published_id not in {record["candidate_id"] for record in records}
    assert pending_candidate["id"] not in {record["candidate_id"] for record in records}
    assert rejected_candidate["id"] not in {record["candidate_id"] for record in records}


def test_export_proposal_replaces_snapshot_instead_of_appending(sc_db_path, kctl_conn, runner, tmp_path):
    _seed_approved(sc_db_path, kctl_conn)

    command = ["export-proposal", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"]
    first = runner.invoke(cli, command)
    second = runner.invoke(cli, command)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    path, records = _read_snapshot(tmp_path, "kctl")
    assert len(records) == 1
    assert path.read_text(encoding="utf-8").count("\n") == 1
    assert list(path.parent.glob(f".{_proposal.PROPOSAL_FILENAME}.*.tmp")) == []


def test_export_proposal_writes_zero_byte_snapshot_when_nothing_is_approved(runner, tmp_path):
    result = runner.invoke(
        cli,
        ["export-proposal", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"],
    )

    assert result.exit_code == 0, result.output
    path, records = _read_snapshot(tmp_path, "kctl")
    assert path.read_bytes() == b""
    assert records == []


def test_export_proposal_supports_distinct_suggested_owner_repo(sc_db_path, kctl_conn, runner, tmp_path):
    _seed_approved(sc_db_path, kctl_conn)

    result = runner.invoke(
        cli,
        [
            "export-proposal",
            "--artifacts-root", str(tmp_path),
            "--repo-id", "kctl",
            "--suggested-owner-repo", "agentops",
        ],
    )

    assert result.exit_code == 0, result.output
    _, records = _read_snapshot(tmp_path, "kctl")
    assert len(records) == 1
    assert records[0]["suggested_owner_repo"] == "agentops"
    assert records[0]["suggested_next_action"] == "propose sprintctl item add in repo agentops"


def test_export_proposal_rejects_invalid_stored_tags(sc_db_path, kctl_conn, runner, tmp_path):
    candidate_id = _seed_approved(sc_db_path, kctl_conn)
    kctl_conn.execute(
        "UPDATE knowledge_candidate SET tags = ? WHERE id = ?", ('[1]', candidate_id)
    )
    kctl_conn.commit()

    result = runner.invoke(
        cli,
        ["export-proposal", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"],
    )

    assert result.exit_code != 0
    assert "tags must be a JSON array" in result.output
    assert not (tmp_path / "kctl" / "knowledge" / _proposal.PROPOSAL_FILENAME).exists()
