import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

from kctl import db as _db
from kctl import extract as _extract


def _load_sprintctl():
    try:
        return import_module("sprintctl.db")
    except ImportError:
        sibling = Path(__file__).resolve().parents[2] / "sprintctl"
        if sibling.exists():
            sys.path.insert(0, str(sibling))
            return import_module("sprintctl.db")
    pytest.skip("sprintctl source not available for integration test")


def test_run_preflight_via_cli_fallback(monkeypatch, sc_db_path):
    conn = _db.get_sprintctl_connection(sc_db_path)

    def _raise_import_error(_conn, _sprint_id):
        raise ImportError("no sprintctl module")

    class _Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "sprint": {"id": 1, "name": "Sprint 1"},
                "stale_items": [{"id": 1, "status": "active"}],
                "threshold_hours": 4.0,
                "pending_threshold_hours": None,
            }
        )
        stderr = ""

    monkeypatch.setattr(_extract, "_run_preflight_via_import", _raise_import_error)
    monkeypatch.setattr(_extract.subprocess, "run", lambda *args, **kwargs: _Proc())

    warnings = _extract.run_preflight(
        conn,
        sprintctl_db_path=sc_db_path,
    )
    conn.close()

    assert warnings == ["Sprint 'Sprint 1' has 1 stale item(s) (1 active > 4 hours)"]


def test_run_preflight_respects_pending_stale_threshold_with_real_sprintctl(monkeypatch, tmp_path):
    sdb = _load_sprintctl()

    sc_db_path = tmp_path / "sprintctl.db"
    conn = sdb.get_connection(sc_db_path)
    sdb.init_db(conn)
    sprint_id = sdb.create_sprint(conn, "Sprint A", status="active")
    track_id = sdb.get_or_create_track(conn, sprint_id, "backend")
    active_id = sdb.create_work_item(conn, sprint_id, track_id, "Implement auth")
    pending_id = sdb.create_work_item(conn, sprint_id, track_id, "Backlog item")
    conn.execute(
        "UPDATE work_item SET status = 'active', updated_at = '2026-04-01T00:00:00Z' WHERE id = ?",
        (active_id,),
    )
    conn.execute(
        "UPDATE work_item SET status = 'pending', updated_at = '2026-03-20T00:00:00Z' WHERE id = ?",
        (pending_id,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("SPRINTCTL_PENDING_STALE_THRESHOLD", "24")
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    warnings = _extract.run_preflight(
        sc_conn,
        sprint_id=sprint_id,
        sprintctl_db_path=sc_db_path,
    )
    sc_conn.close()

    assert warnings == [
        "Sprint 'Sprint A' has 2 stale item(s) (1 active > 4 hours, 1 pending > 24 hours)"
    ]


def test_extract_candidates_with_real_sprintctl_payloads(tmp_path):
    sdb = _load_sprintctl()

    sc_db_path = tmp_path / "sprintctl.db"
    conn = sdb.get_connection(sc_db_path)
    sdb.init_db(conn)
    sprint_id = sdb.create_sprint(conn, "Sprint A", status="active")
    track_id = sdb.get_or_create_track(conn, sprint_id, "backend")
    item_id = sdb.create_work_item(conn, sprint_id, track_id, "Implement auth")
    sdb.create_event(
        conn,
        sprint_id,
        actor="agent-1",
        event_type="decision",
        work_item_id=item_id,
        payload={
            "summary": "Use RS256",
            "detail": "Avoid shared secret coordination",
            "tags": ["auth", "architecture"],
            "git_branch": "feat/auth",
            "git_sha": "abc123",
        },
    )
    claim_id = sdb.create_claim(
        conn,
        item_id,
        "agent-1",
        runtime_session_id="sess-1",
        instance_id="inst-1",
    )
    claim = sdb.get_claim(conn, claim_id, include_secret=True)
    sdb.handoff_claim(
        conn,
        claim_id,
        claim["claim_token"],
        actor="agent-2",
        performed_by="agent-1",
        note="handoff note",
    )
    conn.close()

    kctl_conn = _db.get_connection(tmp_path / "kctl.db")
    _db.init_db(kctl_conn)
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created, structured_count = _extract.extract_candidates(
        sc_conn,
        kctl_conn,
        str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES,
        0,
        None,
        "2026-04-02T00:00:00Z",
    )
    sc_conn.close()
    kctl_conn.close()

    assert structured_count == 2
    by_type = {row["event_type"]: row for row in created}
    assert by_type["decision"]["candidate_kind"] == "durable"
    assert by_type["claim-handoff"]["candidate_kind"] == "coordination"
    assert json.loads(by_type["decision"]["source_payload"])["git_branch"] == "feat/auth"
    assert json.loads(by_type["claim-handoff"]["source_payload"])["to_identity"]["actor"] == "agent-2"
