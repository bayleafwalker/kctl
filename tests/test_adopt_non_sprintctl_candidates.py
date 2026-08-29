"""A claim must be able to reach the knowledge store it is documented to live in.

`templates/dispatch/model/README.md` (agentops) says "kctl is the claims store…
`publish` hands a claim over as a knowledge entry". Until migration 9 that was
unreachable: `knowledge_candidate.source_event_id` was `NOT NULL UNIQUE` and
`source_sprint_id` was `NOT NULL`, so only `extract` -- which reads sprintctl
events -- could create a candidate, and a metanarrative claim has neither.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from kctl import db as _db
from kctl.cli import SERVED_COMMAND_DISPOSITIONS, cli


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "kctl.db"
    conn = _db.get_connection(path)
    _db.init_db(conn)
    conn.close()
    monkeypatch.setenv("KCTL_DB", str(path))
    monkeypatch.setenv("SPRINTCTL_BACKEND", "local")
    return path


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_migration_admits_a_candidate_with_no_sprintctl_event(db_path: Path) -> None:
    conn = _conn(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_candidate)")}
    assert "source_origin" in columns

    first = _db.insert_candidate(
        conn,
        {
            "event_type": "model.claim",
            "summary": "narrow-boundary",
            "extracted_at": "2026-08-29T00:00:00Z",
            "source_origin": "metanarrative",
        },
    )
    second = _db.insert_candidate(
        conn,
        {
            "event_type": "model.claim",
            "summary": "vuoro-non-goals",
            "extracted_at": "2026-08-29T00:00:01Z",
            "source_origin": "metanarrative",
        },
    )
    # Two rows with a NULL source_event_id must both survive: SQLite constrains
    # only the non-null values of a UNIQUE column, which is the semantics relied on.
    assert first is not None and second is not None and first != second


def test_extraction_dedupe_still_holds_for_sprintctl_sourced_rows(db_path: Path) -> None:
    """The positive control: migration 9 must not weaken what it widened around."""
    conn = _conn(db_path)
    payload = {
        "source_event_id": 4242,
        "source_sprint_id": 1,
        "event_type": "decision",
        "summary": "from an event",
        "extracted_at": "2026-08-29T00:00:00Z",
    }
    assert _db.insert_candidate(conn, payload) is not None
    assert _db.insert_candidate(conn, payload) is None  # INSERT OR IGNORE dedupes


def test_existing_rows_default_to_the_sprintctl_origin(db_path: Path) -> None:
    conn = _conn(db_path)
    _db.insert_candidate(
        conn,
        {
            "source_event_id": 7,
            "source_sprint_id": 1,
            "event_type": "decision",
            "summary": "extracted",
            "extracted_at": "2026-08-29T00:00:00Z",
        },
    )
    row = conn.execute(
        "SELECT source_origin FROM knowledge_candidate WHERE source_event_id = 7"
    ).fetchone()
    assert row["source_origin"] == "sprintctl"


def test_adopt_creates_a_candidate_and_says_so(db_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "adopt",
            "--summary", "narrow-boundary",
            "--event-type", "model.claim",
            "--origin", "metanarrative",
            "--actor", "bayleaf",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Adopted candidate #1" in result.output
    assert "metanarrative" in result.output

    row = _conn(db_path).execute("SELECT * FROM knowledge_candidate").fetchone()
    assert row["source_origin"] == "metanarrative"
    assert row["source_event_id"] is None
    assert row["status"] == "candidate"


def test_adopt_does_not_approve_or_publish(db_path: Path) -> None:
    """Adopting is a door, not a decision. The review boundary must be unchanged."""
    CliRunner().invoke(
        cli, ["adopt", "--summary", "s", "--event-type", "model.claim"]
    )
    row = _conn(db_path).execute("SELECT status FROM knowledge_candidate").fetchone()
    assert row["status"] == "candidate"


def test_adopt_is_classified_for_served_mode() -> None:
    """Every top-level command must have a served disposition; the guard at the
    bottom of cli.py enforces it, and this names why `adopt` gets `knowledge`."""
    assert SERVED_COMMAND_DISPOSITIONS["adopt"] == "knowledge"
    assert SERVED_COMMAND_DISPOSITIONS["adopt"] == SERVED_COMMAND_DISPOSITIONS["extract"]
