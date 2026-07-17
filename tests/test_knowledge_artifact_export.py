import json

from kctl import artifact as _artifact
from kctl import db as _db
from kctl import publish as _publish
from kctl import review as _review
from kctl.cli import cli
from kctl.extract import DEFAULT_EVENT_TYPES, extract_candidates
from tests.conftest import add_event


NOW = "2026-07-17T18:00:00Z"
NOW2 = "2026-07-17T18:01:00Z"


def _seed_approved(sc_db_path, kctl_conn, *, event_type="decision", summary="Published knowledge"):
    add_event(sc_db_path, event_type, {"summary": summary})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()
    candidate = _db.list_candidates(kctl_conn, status="candidate")[-1]
    _review.approve_candidate(kctl_conn, candidate["id"], now=NOW2)
    return candidate["id"]


def _read_snapshot(root, repo_id):
    path = root / repo_id / "knowledge" / _artifact.ARTIFACT_FILENAME
    return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_cli_export_writes_complete_published_snapshot(sc_db_path, kctl_conn, runner, tmp_path):
    durable_id = _seed_approved(sc_db_path, kctl_conn, summary="Durable knowledge")
    coordination_id = _seed_approved(
        sc_db_path,
        kctl_conn,
        event_type="claim-handoff",
        summary="Coordination knowledge",
    )
    _publish.publish_candidate(
        kctl_conn,
        durable_id,
        None,
        "Durable body",
        "decision",
        '["architecture"]',
        NOW2,
    )
    _publish.publish_candidate(
        kctl_conn,
        coordination_id,
        None,
        "Coordination body",
        "lesson",
        '["operations"]',
        NOW2,
        coordination=True,
    )

    result = runner.invoke(
        cli,
        ["export", "--artifacts-root", str(tmp_path), "--repo-id", "agentops"],
    )

    assert result.exit_code == 0, result.output
    path, records = _read_snapshot(tmp_path, "agentops")
    assert path.exists()
    assert [record["entry_id"] for record in records] == sorted(
        record["entry_id"] for record in records
    )
    assert {record["stream"] for record in records} == {"durable", "coordination"}
    for record in records:
        assert record["repo_id"] == "agentops"
        assert record["status"] == "published"
        assert record["source"]["event_ref"] == f"sprintctl:event:{record['source']['event_id']}"
        assert record["content_digest"] == _artifact.content_digest(
            record["title"], record["body"]
        )


def test_export_replaces_snapshot_instead_of_appending(sc_db_path, kctl_conn, runner, tmp_path):
    candidate_id = _seed_approved(sc_db_path, kctl_conn)
    _publish.publish_candidate(
        kctl_conn,
        candidate_id,
        None,
        "Body",
        "reference",
        None,
        NOW2,
    )

    command = ["export", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"]
    first = runner.invoke(cli, command)
    second = runner.invoke(cli, command)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    path, records = _read_snapshot(tmp_path, "kctl")
    assert len(records) == 1
    assert path.read_text(encoding="utf-8").count("\n") == 1
    assert list(path.parent.glob(f".{_artifact.ARTIFACT_FILENAME}.*.tmp")) == []


def test_export_writes_zero_byte_snapshot_when_nothing_is_published(runner, tmp_path):
    result = runner.invoke(
        cli,
        ["export", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"],
    )

    assert result.exit_code == 0, result.output
    path, records = _read_snapshot(tmp_path, "kctl")
    assert path.read_bytes() == b""
    assert records == []


def test_export_rejects_invalid_stored_tags(sc_db_path, kctl_conn, runner, tmp_path):
    candidate_id = _seed_approved(sc_db_path, kctl_conn)
    entry = _publish.publish_candidate(
        kctl_conn,
        candidate_id,
        None,
        "Body",
        "decision",
        None,
        NOW2,
    )
    kctl_conn.execute("UPDATE knowledge_entry SET tags = ? WHERE id = ?", ('[1]', entry["id"]))
    kctl_conn.commit()

    result = runner.invoke(
        cli,
        ["export", "--artifacts-root", str(tmp_path), "--repo-id", "kctl"],
    )

    assert result.exit_code != 0
    assert "tags must be a JSON array" in result.output
    assert not (tmp_path / "kctl" / "knowledge" / _artifact.ARTIFACT_FILENAME).exists()
