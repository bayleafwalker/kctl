from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from kctl import db, publish
from kctl.cli import cli


REVIEWED = "2026-10-18T08:00:00Z"
PUBLISHED = "2026-10-18T09:00:00Z"


def _approved(conn, event_id=1):
    candidate_id = db.insert_candidate(conn, dict(
        source_event_id=event_id, source_sprint_id=1, event_type="decision",
        summary=f"Lesson {event_id}", extracted_at=REVIEWED,
    ))
    db.transition_candidate(conn, candidate_id, "approved", REVIEWED, "reviewer",
                            review_notes="Reviewed original source")
    return candidate_id


def _publish(conn, candidate_id, supersedes=None):
    return publish.publish_candidate(
        conn, candidate_id, None, "Reviewed body", "lesson", None,
        PUBLISHED, supersedes_entry_id=supersedes,
    )


def _historical_entry(conn, candidate_id):
    return db.insert_entry(conn, dict(
        candidate_id=candidate_id, title="Historical", body="Original body",
        category="lesson", source_sprint="1", created_at=REVIEWED,
    ))


def test_publish_preserves_review_evidence(kctl_conn):
    candidate_id = _approved(kctl_conn)
    before = db.get_candidate(kctl_conn, candidate_id)
    entry = _publish(kctl_conn, candidate_id)
    after = db.get_candidate(kctl_conn, candidate_id)
    assert after == {**before, "status": "published"}
    assert entry["created_at"] == PUBLISHED


@pytest.mark.parametrize("fault", ["insert_entry", "set_entry_superseded_by", "transition_candidate"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_publication_fault_rolls_back_and_retry_converges(
    kctl_conn, kctl_db_path, monkeypatch, fault, error_type,
):
    old_id = _approved(kctl_conn)
    old_entry = _publish(kctl_conn, old_id)
    candidate_id = _approved(kctl_conn, 2)
    before = list(kctl_conn.iterdump())
    original = getattr(db, fault)

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise error_type("injected after " + fault)

    with monkeypatch.context() as patch:
        patch.setattr(db, fault, fail_after_write)
        with pytest.raises(error_type, match="injected after"):
            _publish(kctl_conn, candidate_id, old_entry["id"])
    assert not kctl_conn.in_transaction
    assert list(kctl_conn.iterdump()) == before
    # Reopen the durable store, retry, and check the state-machine outcome.
    retry = db.get_connection(kctl_db_path)
    try:
        before_candidate = db.get_candidate(retry, candidate_id)
        new_entry = _publish(retry, candidate_id, old_entry["id"])
        assert db.get_candidate(retry, candidate_id) == {**before_candidate, "status": "published"}
        assert db.get_entry(retry, old_entry["id"])["superseded_by"] == new_entry["id"]
        assert len(db.list_entries(retry)) == 2
        stable = list(retry.iterdump())
        with pytest.raises(ValueError, match="only approved candidates"):
            _publish(retry, candidate_id, old_entry["id"])
        assert list(retry.iterdump()) == stable
    finally:
        retry.close()


@pytest.mark.parametrize("status, entries", [
    ("approved", 1), ("approved", 2), ("published", 0), ("published", 2),
])
def test_historical_inconsistency_is_rejected_without_repair(kctl_conn, status, entries):
    candidate_id = _approved(kctl_conn)
    for _ in range(entries):
        _historical_entry(kctl_conn, candidate_id)
    if status == "published":
        db.transition_candidate(kctl_conn, candidate_id, status, REVIEWED, "legacy")
    before = list(kctl_conn.iterdump())
    with pytest.raises(ValueError, match="inconsistent publication.*explicit reconciliation"):
        _publish(kctl_conn, candidate_id)
    assert list(kctl_conn.iterdump()) == before


def test_historical_partial_supersession_target_is_rejected(kctl_conn):
    partial_id = _approved(kctl_conn)
    partial_entry = _historical_entry(kctl_conn, partial_id)
    candidate_id = _approved(kctl_conn, 2)
    before = list(kctl_conn.iterdump())
    with pytest.raises(ValueError, match="inconsistent publication.*explicit reconciliation"):
        _publish(kctl_conn, candidate_id, partial_entry)
    assert list(kctl_conn.iterdump()) == before


def test_publication_does_not_take_over_a_caller_transaction(kctl_conn):
    candidate_id = _approved(kctl_conn)
    kctl_conn.execute("UPDATE knowledge_candidate SET summary = 'Uncommitted' WHERE id = ?", (candidate_id,))
    with pytest.raises(ValueError, match="active transaction"):
        _publish(kctl_conn, candidate_id)
    assert kctl_conn.in_transaction
    assert db.list_entries(kctl_conn) == []
    kctl_conn.rollback()
    assert db.get_candidate(kctl_conn, candidate_id)["summary"] == "Lesson 1"


@pytest.mark.parametrize("fault", ["insert_entry", "set_entry_superseded_by", "transition_candidate"])
def test_process_exit_before_commit_leaves_no_publication_prefix(
    kctl_conn, kctl_db_path, fault,
):
    old_entry = _publish(kctl_conn, _approved(kctl_conn))
    candidate_id = _approved(kctl_conn, 2)
    before = list(kctl_conn.iterdump())
    child = subprocess.run([
        sys.executable, "-c", """
import os, sys
from pathlib import Path
from kctl import db, publish
conn = db.get_connection(Path(sys.argv[1]))
original = getattr(db, sys.argv[4])
def exit_after_write(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(71)
setattr(db, sys.argv[4], exit_after_write)
publish.publish_candidate(conn, int(sys.argv[2]), None, 'Body', 'lesson', None,
                          '2026-10-18T09:00:00Z', supersedes_entry_id=int(sys.argv[3]))
""", str(kctl_db_path), str(candidate_id), str(old_entry["id"]), fault,
    ], capture_output=True, text=True, timeout=15)
    assert child.returncode == 71, child.stderr
    assert list(kctl_conn.iterdump()) == before
    _publish(kctl_conn, candidate_id, old_entry["id"])
    assert len(db.list_entries(kctl_conn)) == 2


def test_commit_failure_rolls_back_before_retry(tmp_path):
    class FailingCommit(sqlite3.Connection):
        fail = False

        def commit(self):
            if self.fail:
                self.fail = False
                raise sqlite3.OperationalError("injected commit failure")
            return super().commit()

    conn = sqlite3.connect(tmp_path / "commit.db", factory=FailingCommit)
    conn.row_factory = sqlite3.Row
    try:
        db.init_db(conn)
        candidate_id = _approved(conn)
        before = list(conn.iterdump())
        conn.fail = True
        with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
            _publish(conn, candidate_id)
        assert not conn.in_transaction
        assert list(conn.iterdump()) == before
        _publish(conn, candidate_id)
        assert len(db.list_entries(conn)) == 1
    finally:
        conn.close()


def test_supersession_refusal_does_not_leave_a_new_entry(kctl_conn):
    old_entry = _publish(kctl_conn, _approved(kctl_conn))
    _publish(kctl_conn, _approved(kctl_conn, 2), old_entry["id"])
    candidate_id = _approved(kctl_conn, 3)
    before = list(kctl_conn.iterdump())
    with pytest.raises(ValueError, match="already superseded"):
        _publish(kctl_conn, candidate_id, old_entry["id"])
    assert list(kctl_conn.iterdump()) == before


def test_cli_reports_historical_partial_publication(kctl_conn, runner):
    candidate_id = _approved(kctl_conn)
    _historical_entry(kctl_conn, candidate_id)
    before = list(kctl_conn.iterdump())
    result = runner.invoke(cli, ["publish", "--id", str(candidate_id), "--body", "Body", "--category", "lesson"])
    assert result.exit_code != 0
    assert "explicit reconciliation" in result.output
    assert list(kctl_conn.iterdump()) == before
