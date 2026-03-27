import json
import sqlite3

import pytest
from click.testing import CliRunner

from kctl import db
from kctl.cli import cli


# ---------------------------------------------------------------------------
# kctl fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kctl_db_path(tmp_path, monkeypatch):
    path = tmp_path / "kctl.db"
    monkeypatch.setenv("KCTL_DB", str(path))
    return path


@pytest.fixture
def kctl_conn(kctl_db_path):
    c = db.get_connection(kctl_db_path)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def runner(kctl_db_path):
    return CliRunner()


# ---------------------------------------------------------------------------
# sprintctl fixture DB (in-memory simulation)
# ---------------------------------------------------------------------------

def _init_sprintctl_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sprint (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            goal        TEXT    NOT NULL DEFAULT '',
            start_date  TEXT    NOT NULL,
            end_date    TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'planned'
        );
        CREATE TABLE IF NOT EXISTS track (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            sprint_id INTEGER NOT NULL,
            name      TEXT    NOT NULL,
            description TEXT  NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS work_item (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id   INTEGER NOT NULL,
            sprint_id  INTEGER NOT NULL,
            title      TEXT    NOT NULL,
            status     TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'active', 'done', 'blocked')),
            assignee   TEXT,
            updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE TABLE IF NOT EXISTS event (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sprint_id    INTEGER NOT NULL,
            work_item_id INTEGER,
            source_type  TEXT    NOT NULL DEFAULT 'actor',
            actor        TEXT    NOT NULL DEFAULT 'test',
            event_type   TEXT    NOT NULL,
            payload      TEXT    NOT NULL DEFAULT '{}',
            created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
    """)
    conn.commit()


@pytest.fixture
def sc_db_path(tmp_path):
    """A real sprintctl-like SQLite DB on disk (needed for read-only URI connect)."""
    path = tmp_path / "sprintctl.db"
    conn = sqlite3.connect(str(path))
    _init_sprintctl_schema(conn)

    # Sprint
    conn.execute(
        "INSERT INTO sprint (name, goal, start_date, end_date, status) VALUES (?,?,?,?,?)",
        ("Sprint 1", "Ship Phase 1", "2026-03-01", "2026-03-31", "active"),
    )
    # Track
    conn.execute(
        "INSERT INTO track (sprint_id, name) VALUES (1, 'backend')"
    )
    # Work item
    conn.execute(
        "INSERT INTO work_item (track_id, sprint_id, title) VALUES (1, 1, 'Implement auth')"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def sc_conn(sc_db_path):
    conn = sqlite3.connect(str(sc_db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def add_event(sc_db_path, event_type: str, payload: dict | None = None,
              sprint_id: int = 1, work_item_id: int | None = 1) -> int:
    """Helper: add an event to the fixture sprintctl DB, return its ID."""
    conn = sqlite3.connect(str(sc_db_path))
    payload_str = json.dumps(payload) if payload else "{}"
    cur = conn.execute(
        "INSERT INTO event (sprint_id, work_item_id, event_type, payload) VALUES (?,?,?,?)",
        (sprint_id, work_item_id, event_type, payload_str),
    )
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid
