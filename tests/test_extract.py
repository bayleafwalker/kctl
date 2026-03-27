import json

import pytest

from kctl import db as _db
from kctl import extract as _extract
from kctl.cli import cli
from kctl.extract import build_candidate, extract_candidates
from tests.conftest import add_event

NOW = "2026-03-26T10:00:00Z"


# ---------------------------------------------------------------------------
# build_candidate
# ---------------------------------------------------------------------------

def test_build_candidate_full_payload():
    event = {
        "id": 1,
        "sprint_id": 1,
        "work_item_id": 1,
        "event_type": "decision",
        "payload": json.dumps({
            "summary": "Use RS256",
            "detail": "Symmetric HMAC breaks across services",
            "tags": ["auth", "architecture"],
            "confidence": "high",
        }),
        "item_title": "Implement auth",
        "track_name": "backend",
    }
    c, structured = build_candidate(event, NOW)
    assert c["summary"] == "Use RS256"
    assert c["detail"] == "Symmetric HMAC breaks across services"
    assert json.loads(c["tags"]) == ["auth", "architecture"]
    assert c["confidence"] == "high"
    assert c["source_event_id"] == 1
    assert c["extracted_at"] == NOW
    assert structured is True


def test_build_candidate_empty_payload():
    event = {
        "id": 2,
        "sprint_id": 1,
        "work_item_id": 1,
        "event_type": "lesson-learned",
        "payload": "{}",
        "item_title": "Fix deploy",
        "track_name": "infra",
    }
    c, structured = build_candidate(event, NOW)
    assert c["summary"] == "lesson-learned: Fix deploy"
    assert json.loads(c["tags"]) == []
    assert c["confidence"] is None
    assert structured is False


def test_build_candidate_no_item():
    event = {
        "id": 3,
        "sprint_id": 1,
        "work_item_id": None,
        "event_type": "pattern-noted",
        "payload": None,
        "item_title": None,
        "track_name": None,
    }
    c, structured = build_candidate(event, NOW)
    assert c["summary"] == "pattern-noted: no item"
    assert structured is False


def test_build_candidate_invalid_payload_json():
    event = {
        "id": 4,
        "sprint_id": 1,
        "work_item_id": None,
        "event_type": "decision",
        "payload": "not valid json",
        "item_title": "Some task",
        "track_name": None,
    }
    c, structured = build_candidate(event, NOW)
    assert c["summary"] == "decision: Some task"
    assert structured is False


# ---------------------------------------------------------------------------
# extract_candidates — integration against fixture sprintctl DB
# ---------------------------------------------------------------------------

def test_extract_creates_candidates(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Use WAL mode", "tags": ["db"]})
    add_event(sc_db_path, "pattern-noted", {"summary": "Cache at edge"})

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    _db.validate_sprintctl_schema(sc_conn)

    created, structured_count = extract_candidates(
        sprintctl_conn=sc_conn,
        kctl_conn=kctl_conn,
        sprintctl_db_path=str(sc_db_path),
        event_types=_extract.DEFAULT_EVENT_TYPES,
        since_event_id=0,
        sprint_id=None,
        now=NOW,
    )
    sc_conn.close()

    assert len(created) == 2
    assert structured_count == 2
    summaries = {c["summary"] for c in created}
    assert "Use WAL mode" in summaries
    assert "Cache at edge" in summaries


def test_extract_ignores_non_target_events(sc_db_path, kctl_conn):
    add_event(sc_db_path, "status-update", {"summary": "Nothing special"})
    add_event(sc_db_path, "decision", {"summary": "Keep it"})

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created, _ = extract_candidates(
        sprintctl_conn=sc_conn,
        kctl_conn=kctl_conn,
        sprintctl_db_path=str(sc_db_path),
        event_types=_extract.DEFAULT_EVENT_TYPES,
        since_event_id=0,
        sprint_id=None,
        now=NOW,
    )
    sc_conn.close()

    assert len(created) == 1
    assert created[0]["summary"] == "Keep it"


def test_extract_is_idempotent(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Auth decision"})

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created_first, _ = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, 0, None, NOW,
    )
    sc_conn.close()

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created_second, _ = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, 0, None, NOW,
    )
    sc_conn.close()

    assert len(created_first) == 1
    assert len(created_second) == 0  # duplicate skipped


def test_extract_incremental(sc_db_path, kctl_conn):
    eid1 = add_event(sc_db_path, "decision", {"summary": "First"})
    eid2 = add_event(sc_db_path, "blocker-resolved", {"summary": "Second"})

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    # Only extract events after eid1
    created, _ = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, since_event_id=eid1, sprint_id=None, now=NOW,
    )
    sc_conn.close()

    assert len(created) == 1
    assert created[0]["summary"] == "Second"


def test_extract_updates_extractor_state(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "State test"})

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, 0, None, NOW,
    )  # tuple return ignored here
    sc_conn.close()

    state = _db.get_extractor_state(kctl_conn, str(sc_db_path))
    assert state is not None
    assert state["last_event_id"] > 0
    assert state["last_run_at"] == NOW


def test_extract_sprint_filter(sc_db_path, kctl_conn, sc_conn):
    # Add a second sprint and events for each
    sc_conn2 = sqlite3.connect(str(sc_db_path))
    sc_conn2.execute(
        "INSERT INTO sprint (id, name, goal, start_date, end_date, status) VALUES (2,'S2','',?,?,'planned')",
        ("2026-04-01", "2026-04-30"),
    )
    sc_conn2.execute(
        "INSERT INTO event (sprint_id, work_item_id, event_type, payload) VALUES (2, NULL, 'decision', ?)",
        ('{"summary": "Sprint 2 decision"}',),
    )
    sc_conn2.commit()
    sc_conn2.close()

    add_event(sc_db_path, "decision", {"summary": "Sprint 1 decision"}, sprint_id=1)

    sc_conn3 = _db.get_sprintctl_connection(sc_db_path)
    created, _ = extract_candidates(
        sc_conn3, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, 0, sprint_id=1, now=NOW,
    )
    sc_conn3.close()

    assert all(c["source_sprint_id"] == 1 for c in created)


import sqlite3  # noqa: E402 (placed here for the inline helper above)


# ---------------------------------------------------------------------------
# --full flag resets watermark
# ---------------------------------------------------------------------------

def test_extract_full_flag_rescans_all_events(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Event A"})

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created_first, _ = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, 0, None, NOW,
    )
    sc_conn.close()
    assert len(created_first) == 1

    # Simulate --full by passing since_event_id=0 again (full flag resets to 0)
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created_full, _ = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, since_event_id=0, sprint_id=None, now=NOW,
    )
    sc_conn.close()
    # Idempotent — same event, no new candidates created
    assert len(created_full) == 0


def test_cli_extract_full_rescans(sc_db_path, kctl_conn, runner):
    add_event(sc_db_path, "decision", {"summary": "Rescan me"})

    # First extract
    runner.invoke(cli, ["extract", "--sprintctl-db", str(sc_db_path), "--no-preflight"])
    # Second with --full
    result = runner.invoke(cli, ["extract", "--sprintctl-db", str(sc_db_path), "--no-preflight", "--full"])
    assert result.exit_code == 0, result.output
    # Should report 0 new (idempotent) but succeed
    assert "0 new candidates" in result.output


# ---------------------------------------------------------------------------
# validate_sprintctl_schema — error cases
# ---------------------------------------------------------------------------

def test_validate_schema_missing_table(tmp_path):
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sprint (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    sc_conn = sqlite3.connect(str(path))
    sc_conn.row_factory = sqlite3.Row
    import pytest
    with pytest.raises(ValueError, match="missing tables"):
        _db.validate_sprintctl_schema(sc_conn)
    sc_conn.close()


def test_validate_schema_missing_event_columns(tmp_path):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sprint (id INTEGER PRIMARY KEY, name TEXT, goal TEXT,
            start_date TEXT, end_date TEXT, status TEXT, kind TEXT);
        CREATE TABLE track (id INTEGER PRIMARY KEY, sprint_id INTEGER, name TEXT, description TEXT);
        CREATE TABLE work_item (id INTEGER PRIMARY KEY, track_id INTEGER, sprint_id INTEGER,
            title TEXT, description TEXT,
            status TEXT CHECK (status IN ('pending', 'active', 'done', 'blocked')),
            assignee TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE event (id INTEGER PRIMARY KEY, sprint_id INTEGER);
    """)
    conn.commit()
    conn.close()

    sc_conn = sqlite3.connect(str(path))
    sc_conn.row_factory = sqlite3.Row
    import pytest
    with pytest.raises(ValueError, match="missing columns"):
        _db.validate_sprintctl_schema(sc_conn)
    sc_conn.close()


# ---------------------------------------------------------------------------
# track_name preserved through extract → candidate
# ---------------------------------------------------------------------------

def test_extract_preserves_track_name(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Track test"}, work_item_id=1)

    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created, _ = extract_candidates(
        sc_conn, kctl_conn, str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES, 0, None, NOW,
    )
    sc_conn.close()

    assert len(created) == 1
    # track_name is stored on the candidate dict during extraction
    assert created[0]["track_name"] == "backend"

    # Verify it was persisted in the DB
    candidates = _db.list_candidates(kctl_conn, status="candidate")
    assert candidates[0]["source_track"] == "backend"
