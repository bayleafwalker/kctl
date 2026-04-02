import json

import pytest

from kctl import db as _db
from kctl import review as _review
from tests.conftest import add_event
from kctl.extract import extract_candidates, DEFAULT_EVENT_TYPES

NOW = "2026-03-26T10:00:00Z"
NOW2 = "2026-03-26T11:00:00Z"


def _seed_candidate(sc_db_path, kctl_conn, summary="Test decision") -> int:
    add_event(sc_db_path, "decision", {"summary": summary})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)  # tuple return ignored
    sc_conn.close()
    candidates = _db.list_candidates(kctl_conn, status="candidate")
    return candidates[-1]["id"]


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def test_approve_transitions_to_approved(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    updated = _review.approve_candidate(kctl_conn, cid, now=NOW2)
    assert updated["status"] == "approved"
    assert updated["reviewed_at"] == NOW2
    assert updated["reviewed_by"] == "human"


def test_approve_allows_title_override(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    updated = _review.approve_candidate(kctl_conn, cid, now=NOW2, title="Revised title")
    assert updated["summary"] == "Revised title"


def test_approve_allows_tags_override(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    updated = _review.approve_candidate(kctl_conn, cid, now=NOW2, tags='["auth","lessons"]')
    assert json.loads(updated["tags"]) == ["auth", "lessons"]


def test_approve_allows_detail_override(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    updated = _review.approve_candidate(
        kctl_conn,
        cid,
        now=NOW2,
        detail="Expanded durable detail",
    )
    assert updated["detail"] == "Expanded durable detail"


def test_approve_rejects_invalid_tags_json(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    with pytest.raises(ValueError, match="Invalid tags JSON"):
        _review.approve_candidate(kctl_conn, cid, now=NOW2, tags="not json")


def test_approve_rejects_tags_not_array(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    with pytest.raises(ValueError, match="must be a JSON array"):
        _review.approve_candidate(kctl_conn, cid, now=NOW2, tags='{"key": "val"}')


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------

def test_reject_transitions_to_rejected(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    updated = _review.reject_candidate(kctl_conn, cid, now=NOW2, reason="duplicate")
    assert updated["status"] == "rejected"
    assert updated["review_notes"] == "duplicate"


# ---------------------------------------------------------------------------
# invalid transitions
# ---------------------------------------------------------------------------

def test_cannot_approve_rejected_candidate(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    _review.reject_candidate(kctl_conn, cid, now=NOW)
    with pytest.raises(ValueError, match="Cannot transition"):
        _review.approve_candidate(kctl_conn, cid, now=NOW2)


def test_cannot_reject_approved_candidate(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    _review.approve_candidate(kctl_conn, cid, now=NOW)
    with pytest.raises(ValueError, match="Cannot transition"):
        _review.reject_candidate(kctl_conn, cid, now=NOW2)


def test_cannot_approve_published_candidate(sc_db_path, kctl_conn):
    cid = _seed_candidate(sc_db_path, kctl_conn)
    _db.transition_candidate(kctl_conn, cid, "approved", NOW, "human")
    _db.transition_candidate(kctl_conn, cid, "published", NOW, "human")
    with pytest.raises(ValueError, match="Cannot transition"):
        _review.approve_candidate(kctl_conn, cid, now=NOW2)


def test_candidate_not_found_raises(kctl_conn):
    with pytest.raises(ValueError, match="not found"):
        _review.approve_candidate(kctl_conn, 9999, now=NOW)


# ---------------------------------------------------------------------------
# list_candidates filtering
# ---------------------------------------------------------------------------

def test_list_candidates_status_filter(sc_db_path, kctl_conn):
    cid1 = _seed_candidate(sc_db_path, kctl_conn, "First")
    cid2 = _seed_candidate(sc_db_path, kctl_conn, "Second")
    _review.approve_candidate(kctl_conn, cid1, now=NOW2)

    candidates_only = _db.list_candidates(kctl_conn, status="candidate")
    approved_only = _db.list_candidates(kctl_conn, status="approved")

    assert all(c["status"] == "candidate" for c in candidates_only)
    assert all(c["status"] == "approved" for c in approved_only)
    assert len(approved_only) == 1


def test_list_candidates_tag_filter(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Auth decision", "tags": ["auth", "security"]})
    add_event(sc_db_path, "decision", {"summary": "DB decision", "tags": ["database"]})
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)  # tuple return ignored
    sc_conn.close()

    auth_tagged = _db.list_candidates(kctl_conn, status="candidate", tag="auth")
    assert len(auth_tagged) == 1
    assert auth_tagged[0]["summary"] == "Auth decision"


def test_list_candidates_kind_filter(sc_db_path, kctl_conn):
    add_event(sc_db_path, "decision", {"summary": "Durable"})
    add_event(
        sc_db_path,
        "claim-handoff",
        {"summary": "Coordination", "tags": ["claims"]},
        source_type="system",
    )
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    extract_candidates(sc_conn, kctl_conn, str(sc_db_path), DEFAULT_EVENT_TYPES, 0, None, NOW)
    sc_conn.close()

    durable = _db.list_candidates(kctl_conn, status="candidate", candidate_kind="durable")
    coordination = _db.list_candidates(kctl_conn, status="candidate", candidate_kind="coordination")
    assert [row["summary"] for row in durable] == ["Durable"]
    assert [row["summary"] for row in coordination] == ["Coordination"]
