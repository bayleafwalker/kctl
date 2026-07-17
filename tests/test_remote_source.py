from datetime import datetime, timezone

import pytest

from kctl import db as _db
from kctl import extract as _extract
from kctl import source as _source


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.conn.calls.append((query, params))

    def fetchall(self):
        return self.conn.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def test_remote_source_extracts_tenant_scoped_rows_read_only(tmp_path):
    conn = _Connection(
        [
            {
                "id": 41,
                "sprint_id": 9,
                "work_item_id": 7,
                "source_type": "actor",
                "actor": "codex",
                "event_type": "decision",
                "payload": {"summary": "Use the remote source", "tags": ["remote"]},
                "created_at": datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
                "item_title": "Remote extraction",
                "track_name": "remote-sprintctl",
            }
        ]
    )
    source = _source.RemoteSprintctlSource(conn=conn, repo_id="source-repo")
    kctl_conn = _db.get_connection(tmp_path / "kctl.db")
    _db.init_db(kctl_conn)

    created, structured = _extract.extract_candidates(
        source,
        kctl_conn,
        source.source_id,
        {"decision"},
        0,
        None,
        "2026-07-17T12:01:00Z",
    )

    assert structured == 1
    assert created[0]["source_created_at"] == "2026-07-17T12:00:00Z"
    assert _db.list_candidates(kctl_conn, status="candidate")[0]["summary"] == "Use the remote source"
    query, params = conn.calls[0]
    assert "e.repo_id = %s" in query
    assert params == ["source-repo", 0, ["decision"]]
    assert all(call[0].lstrip().upper().startswith("SELECT") for call in conn.calls)


def test_remote_source_requires_url(monkeypatch):
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)

    with pytest.raises(_source.SprintctlSourceError, match="requires SPRINTCTL_URL"):
        _source.open_sprintctl_source(remote_repo_id="source-repo")
