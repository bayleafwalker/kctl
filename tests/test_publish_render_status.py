import json

import pytest

from kctl import db as _db
from kctl import publish as _publish
from kctl import review as _review
from kctl.cli import cli
from kctl.extract import extract_candidates, DEFAULT_EVENT_TYPES
from tests.conftest import add_event

NOW = "2026-03-27T10:00:00Z"
NOW2 = "2026-03-27T11:00:00Z"


def _seed_approved(sc_db_path, kctl_conn, summary="Test decision") -> int:
    add_event(sc_db_path, "decision", {"summary": summary})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)  # tuple ignored
    sc_conn.close()
    candidates = _db.list_candidates(kctl_conn, status="candidate")
    cid = candidates[-1]["id"]
    _review.approve_candidate(kctl_conn, cid, now=NOW2)
    return cid


# ---------------------------------------------------------------------------
# publish — unit
# ---------------------------------------------------------------------------

def test_publish_creates_entry(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn)
    entry = _publish.publish_candidate(
        kctl_conn, candidate_id=cid, title="Use RS256",
        body="Symmetric HMAC breaks across services.",
        category="decision", tags='["auth"]', now=NOW2,
    )
    assert entry["title"] == "Use RS256"
    assert entry["category"] == "decision"
    assert entry["body"] == "Symmetric HMAC breaks across services."
    assert json.loads(entry["tags"]) == ["auth"]


def test_publish_transitions_candidate_to_published(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn)
    _publish.publish_candidate(
        kctl_conn, candidate_id=cid, title=None,
        body="body", category="lesson", tags=None, now=NOW2,
    )
    c = _db.get_candidate(kctl_conn, cid)
    assert c["status"] == "published"


def test_publish_uses_candidate_summary_as_default_title(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn, summary="Default title candidate")
    entry = _publish.publish_candidate(
        kctl_conn, candidate_id=cid, title=None,
        body="body", category="pattern", tags=None, now=NOW2,
    )
    assert entry["title"] == "Default title candidate"


def test_publish_rejects_non_approved_candidate(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Still a candidate"})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)  # tuple ignored
    sc_conn.close()
    candidates = _db.list_candidates(kctl_conn, status="candidate")
    cid = candidates[-1]["id"]

    with pytest.raises(ValueError, match="only approved candidates can be published"):
        _publish.publish_candidate(
            kctl_conn, candidate_id=cid, title=None,
            body="body", category="decision", tags=None, now=NOW2,
        )


def test_publish_rejects_invalid_category(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn)
    with pytest.raises(ValueError, match="Invalid category"):
        _publish.publish_candidate(
            kctl_conn, candidate_id=cid, title=None,
            body="body", category="unknown", tags=None, now=NOW2,
        )


def test_publish_rejects_invalid_tags(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn)
    with pytest.raises(ValueError, match="Invalid tags JSON"):
        _publish.publish_candidate(
            kctl_conn, candidate_id=cid, title=None,
            body="body", category="decision", tags="not-json", now=NOW2,
        )


# ---------------------------------------------------------------------------
# render — unit via list_entries
# ---------------------------------------------------------------------------

def test_list_entries_returns_published(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn)
    _publish.publish_candidate(
        kctl_conn, candidate_id=cid, title="Entry A",
        body="body", category="decision", tags='["x"]', now=NOW2,
    )
    entries = _db.list_entries(kctl_conn)
    assert len(entries) == 1
    assert entries[0]["title"] == "Entry A"


def test_list_entries_category_filter(sc_db_path, kctl_conn):
    cid1 = _seed_approved(sc_db_path, kctl_conn, "First")
    cid2 = _seed_approved(sc_db_path, kctl_conn, "Second")
    _publish.publish_candidate(kctl_conn, cid1, None, "body", "decision", None, NOW2)
    _publish.publish_candidate(kctl_conn, cid2, None, "body", "lesson", None, NOW2)

    decisions = _db.list_entries(kctl_conn, category="decision")
    lessons = _db.list_entries(kctl_conn, category="lesson")
    assert len(decisions) == 1
    assert len(lessons) == 1


def test_list_entries_sprint_filter(sc_db_path, kctl_conn):
    # Both candidates come from sprint_id=1 (fixture default); source_sprint is stored as str("1")
    cid1 = _seed_approved(sc_db_path, kctl_conn, "Sprint 1 entry")
    cid2 = _seed_approved(sc_db_path, kctl_conn, "Also sprint 1")
    _publish.publish_candidate(kctl_conn, cid1, None, "body", "decision", None, NOW2)
    _publish.publish_candidate(kctl_conn, cid2, None, "body", "lesson", None, NOW2)

    sprint1 = _db.list_entries(kctl_conn, sprint_id=1)
    sprint9 = _db.list_entries(kctl_conn, sprint_id=9)
    assert len(sprint1) == 2
    assert len(sprint9) == 0


def test_cli_render_sprint_filter(sc_db_path, kctl_conn, runner):
    cid = _seed_approved(sc_db_path, kctl_conn, "Sprint-scoped entry")
    _publish.publish_candidate(kctl_conn, cid, "Sprint entry", "body", "decision", None, NOW2)

    result_all = runner.invoke(cli, ["render"])
    result_sprint1 = runner.invoke(cli, ["render", "--sprint-id", "1"])
    result_sprint9 = runner.invoke(cli, ["render", "--sprint-id", "9"])

    assert "Sprint entry" in result_all.output
    assert "Sprint entry" in result_sprint1.output
    assert "Sprint entry" not in result_sprint9.output
    assert "No published entries" in result_sprint9.output


def test_list_entries_tag_filter(sc_db_path, kctl_conn):
    cid1 = _seed_approved(sc_db_path, kctl_conn, "Tagged")
    cid2 = _seed_approved(sc_db_path, kctl_conn, "Untagged")
    _publish.publish_candidate(kctl_conn, cid1, None, "body", "decision", '["auth"]', NOW2)
    _publish.publish_candidate(kctl_conn, cid2, None, "body", "decision", '["db"]', NOW2)

    auth = _db.list_entries(kctl_conn, tag="auth")
    assert len(auth) == 1
    assert auth[0]["title"] == "Tagged"


# ---------------------------------------------------------------------------
# CLI — publish command
# ---------------------------------------------------------------------------

def test_cli_publish_command(sc_db_path, kctl_conn, runner):
    cid = _seed_approved(sc_db_path, kctl_conn, "CLI publish test")
    result = runner.invoke(cli, [
        "publish",
        "--id", str(cid),
        "--body", "Full detail body.",
        "--category", "decision",
    ])
    assert result.exit_code == 0, result.output
    assert "Published entry #" in result.output


def test_cli_publish_rejects_candidate(sc_db_path, kctl_conn, runner):
    add_event(sc_db_path, "decision", {"summary": "Still candidate"})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)  # tuple ignored
    sc_conn.close()
    candidates = _db.list_candidates(kctl_conn, status="candidate")
    cid = candidates[-1]["id"]

    result = runner.invoke(cli, [
        "publish", "--id", str(cid), "--body", "x", "--category", "decision",
    ])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI — render command
# ---------------------------------------------------------------------------

def test_cli_render_outputs_markdown(sc_db_path, kctl_conn, runner):
    cid = _seed_approved(sc_db_path, kctl_conn, "RS256 decision")
    _publish.publish_candidate(
        kctl_conn, cid, "Use RS256", "RS256 is better.", "decision", '["auth"]', NOW2,
    )
    result = runner.invoke(cli, ["render"])
    assert result.exit_code == 0, result.output
    assert "# Knowledge Base" in result.output
    assert "## Decisions" in result.output
    assert "### Use RS256" in result.output
    assert "RS256 is better." in result.output


def test_cli_render_empty(runner):
    result = runner.invoke(cli, ["render"])
    assert result.exit_code == 0
    assert "No published entries" in result.output


def test_cli_render_to_file(sc_db_path, kctl_conn, runner, tmp_path):
    cid = _seed_approved(sc_db_path, kctl_conn, "File output test")
    _publish.publish_candidate(kctl_conn, cid, None, "body", "lesson", None, NOW2)

    out_file = tmp_path / "kb.md"
    result = runner.invoke(cli, ["render", "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text()
    assert "# Knowledge Base" in content


# ---------------------------------------------------------------------------
# CLI — status command
# ---------------------------------------------------------------------------

def test_cli_status_shows_pipeline_counts(sc_db_path, kctl_conn, runner):
    # One candidate, one approved, one published
    cid1 = _seed_approved(sc_db_path, kctl_conn, "Approved one")  # already approved
    cid2_raw = add_event(sc_db_path, "pattern-noted", {"summary": "Pending candidate"})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)  # tuple ignored
    sc_conn.close()

    _publish.publish_candidate(kctl_conn, cid1, None, "body", "decision", None, NOW2)

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "awaiting review" in result.output
    assert "published" in result.output


def test_cli_status_sprint_filter(sc_db_path, kctl_conn, runner):
    cid = _seed_approved(sc_db_path, kctl_conn, "Sprint-filtered")
    result = runner.invoke(cli, ["status", "--sprint-id", "1"])
    assert result.exit_code == 0
    assert "sprint 1" in result.output


# ---------------------------------------------------------------------------
# extract — quality reporting
# ---------------------------------------------------------------------------

def test_extract_quality_counts(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Structured"})
    add_event(sc_db_path, "decision", {})  # bare — no summary key

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created, structured_count = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW,
    )
    sc_conn.close()

    assert len(created) == 2
    assert structured_count == 1


def test_cli_extract_reports_quality(sc_db_path, kctl_conn, runner):
    add_event(sc_db_path, "decision", {"summary": "With payload"})
    add_event(sc_db_path, "lesson-learned", {})  # bare

    result = runner.invoke(cli, ["extract", "--sprintctl-db", str(sc_db_path), "--no-preflight"])
    assert result.exit_code == 0, result.output
    assert "structured payload" in result.output
    assert "bare event" in result.output
