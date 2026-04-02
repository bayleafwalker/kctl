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


def test_publish_rejects_coordination_candidate(sc_db_path, kctl_conn):
    add_event(
        sc_db_path,
        "claim-handoff",
        {"summary": "Coordination event"},
        source_type="system",
    )
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()
    candidates = _db.list_candidates(
        kctl_conn,
        status="candidate",
        candidate_kind="coordination",
    )
    cid = candidates[-1]["id"]
    _review.approve_candidate(kctl_conn, cid, now=NOW2)

    with pytest.raises(ValueError, match="only durable candidates can be published"):
        _publish.publish_candidate(
            kctl_conn,
            candidate_id=cid,
            title=None,
            body="body",
            category="reference",
            tags=None,
            now=NOW2,
        )


def test_publish_marks_superseded_entry(sc_db_path, kctl_conn):
    old_cid = _seed_approved(sc_db_path, kctl_conn, "Old decision")
    old_entry = _publish.publish_candidate(
        kctl_conn,
        candidate_id=old_cid,
        title="Old decision",
        body="old body",
        category="decision",
        tags=None,
        now=NOW2,
    )
    new_cid = _seed_approved(sc_db_path, kctl_conn, "New decision")
    new_entry = _publish.publish_candidate(
        kctl_conn,
        candidate_id=new_cid,
        title="New decision",
        body="new body",
        category="decision",
        tags=None,
        now=NOW2,
        supersedes_entry_id=old_entry["id"],
    )

    refreshed_old = _db.get_entry(kctl_conn, old_entry["id"])
    assert refreshed_old["superseded_by"] == new_entry["id"]


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
    # Track is foregrounded; sprint is a container ref
    assert "track: backend" in result.output
    assert "sprint: 1" in result.output


def test_cli_render_shows_superseded_entry_link(sc_db_path, kctl_conn, runner):
    old_cid = _seed_approved(sc_db_path, kctl_conn, "Old decision")
    old_entry = _publish.publish_candidate(
        kctl_conn, old_cid, "Old decision", "old body", "decision", None, NOW2,
    )
    new_cid = _seed_approved(sc_db_path, kctl_conn, "New decision")
    new_entry = _publish.publish_candidate(
        kctl_conn,
        new_cid,
        "New decision",
        "new body",
        "decision",
        None,
        NOW2,
        supersedes_entry_id=old_entry["id"],
    )

    result = runner.invoke(cli, ["render"])
    assert result.exit_code == 0
    assert f"Superseded by: entry #{new_entry['id']}" in result.output


def test_cli_render_uses_default_project_name(runner):
    """KCTL_PROJECT defaults to 'homelab-analytics', not 'project'."""
    result = runner.invoke(cli, ["render"])
    assert result.exit_code == 0
    assert "homelab-analytics" in result.output


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


def test_cli_status_json_output(sc_db_path, kctl_conn, runner):
    """--json emits valid JSON with counts and approved list including source context."""
    cid = _seed_approved(sc_db_path, kctl_conn, "Approved for JSON")
    result = runner.invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "counts" in data
    assert data["counts"]["approved"] == 1
    assert data["counts"]["candidate"] == 0
    approved = data["approved"]
    assert any(a["id"] == cid for a in approved)
    # Approved entries include source context for agent backlog shaping
    entry = next(a for a in approved if a["id"] == cid)
    assert entry["candidate_kind"] == "durable"
    assert "source_track" in entry
    assert "source_sprint_id" in entry
    assert entry["source_sprint_id"] == 1
    assert data["sprint_id"] is None


def test_cli_status_kind_all_separates_durable_and_coordination(sc_db_path, kctl_conn, runner):
    durable_cid = _seed_approved(sc_db_path, kctl_conn, "Durable JSON")
    add_event(
        sc_db_path,
        "claim-handoff",
        {"summary": "Coordination JSON"},
        source_type="system",
    )
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()

    result = runner.invoke(cli, ["status", "--kind", "all", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["counts_by_kind"]["durable"]["approved"] == 1
    assert data["counts_by_kind"]["coordination"]["candidate"] == 1
    assert any(row["id"] == durable_cid for row in data["approved"])


def test_cli_status_json_sprint_filter(sc_db_path, kctl_conn, runner):
    """--json with --sprint-id scopes counts and sets sprint_id field."""
    _seed_approved(sc_db_path, kctl_conn, "Sprint JSON entry")
    result = runner.invoke(cli, ["status", "--sprint-id", "1", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["sprint_id"] == 1


def test_cli_review_list_json_output(sc_db_path, kctl_conn, runner):
    """review list --json emits a JSON array of candidates."""
    add_event(
        sc_db_path,
        "decision",
        {
            "summary": "JSON candidate",
            "tags": ["auth"],
            "git_branch": "feat/auth",
            "git_sha": "abc123",
        },
    )
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()

    result = runner.invoke(cli, ["review", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["summary"] == "JSON candidate"
    assert rows[0]["status"] == "candidate"
    assert rows[0]["candidate_kind"] == "durable"
    assert rows[0]["tags"] == ["auth"]
    assert rows[0]["provenance"]["git_branch"] == "feat/auth"
    assert rows[0]["provenance"]["git_sha"] == "abc123"


def test_cli_review_list_kind_coordination(sc_db_path, kctl_conn, runner):
    add_event(
        sc_db_path,
        "claim-handoff",
        {
            "summary": "Coordination candidate",
            "mode": "rotate",
            "from_identity": {"actor": "bot-1"},
            "to_identity": {"actor": "bot-2"},
        },
        source_type="system",
    )
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()

    result = runner.invoke(cli, ["review", "list", "--kind", "coordination", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["candidate_kind"] == "coordination"
    assert rows[0]["coordination_context"]["mode"] == "rotate"


def test_cli_review_list_json_empty(runner):
    """review list --json returns empty array when no candidates."""
    result = runner.invoke(cli, ["review", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_cli_review_show_json_output(sc_db_path, kctl_conn, runner):
    add_event(
        sc_db_path,
        "claim-handoff",
        {
            "summary": "Show JSON candidate",
            "tags": ["claims"],
            "mode": "rotate",
            "from_identity": {"actor": "bot-1"},
            "to_identity": {"actor": "bot-2"},
            "git_branch": "feat/claims",
            "git_sha": "deadbeef",
        },
        source_type="system",
        actor="bot-1",
        created_at="2026-03-27T10:30:00Z",
    )
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()

    row = _db.list_candidates(kctl_conn, status="candidate")[0]
    result = runner.invoke(
        cli,
        ["review", "show", "--id", str(row["id"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == row["id"]
    assert data["candidate_kind"] == "coordination"
    assert data["source_type"] == "system"
    assert data["source_actor"] == "bot-1"
    assert data["tags"] == ["claims"]
    assert data["provenance"]["git_branch"] == "feat/claims"
    assert data["provenance"]["git_sha"] == "deadbeef"
    assert data["coordination_context"]["mode"] == "rotate"
    assert data["coordination_context"]["to_identity"]["actor"] == "bot-2"


# ---------------------------------------------------------------------------
# source_track flows through extract → publish → entry
# ---------------------------------------------------------------------------

def test_publish_preserves_source_track(sc_db_path, kctl_conn):
    cid = _seed_approved(sc_db_path, kctl_conn, "Track flow test")
    entry = _publish.publish_candidate(
        kctl_conn, candidate_id=cid, title=None,
        body="body", category="decision", tags=None, now=NOW2,
    )
    assert entry["source_track"] == "backend"


def test_publish_source_track_none_when_no_item(sc_db_path, kctl_conn):
    from tests.conftest import add_event as _add_event
    from kctl.extract import extract_candidates as _ec
    _add_event(sc_db_path, "decision", {"summary": "No item event"}, work_item_id=None)
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    _ec(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()
    candidates = _db.list_candidates(kctl_conn, status="candidate")
    cid = candidates[-1]["id"]
    _review.approve_candidate(kctl_conn, cid, now=NOW2)
    entry = _publish.publish_candidate(
        kctl_conn, candidate_id=cid, title=None,
        body="body", category="decision", tags=None, now=NOW2,
    )
    assert entry["source_track"] is None


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
