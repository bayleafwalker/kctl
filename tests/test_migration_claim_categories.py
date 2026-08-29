"""Coverage for migration 8: knowledge categories admit the claim kinds.

Tenets, directions, practices and decisions are all claims in the metanarrative
model (agentops `templates/dispatch/model/README.md`). A published tenet should
keep its kind rather than flatten to `decision`, so `knowledge_entry.category`
had to widen.

SQLite cannot alter a CHECK constraint, so the migration rebuilds the table. The
two things a rebuild can silently break -- the rows, and foreign-key enforcement
on the connection afterwards -- are what these tests hold.
"""

import sqlite3

import pytest

from kctl import db
from kctl.publish import VALID_CATEGORIES


CLAIM_KINDS = {"tenet", "direction"}
LEGACY_CATEGORIES = {"decision", "pattern", "lesson", "risk", "reference"}


def _seed_pre_migration_8_db(conn: sqlite3.Connection) -> None:
    """Build a DB at schema_version=7 carrying entries and a supersession link."""
    original = db._MIGRATIONS
    try:
        db._MIGRATIONS = original[:7]
        db.init_db(conn)
    finally:
        db._MIGRATIONS = original
    conn.execute(
        """
        INSERT INTO knowledge_candidate
            (source_event_id, source_sprint_id, event_type, summary, status, extracted_at)
        VALUES (1, 1, 'decision', 'seed candidate', 'approved', '2026-01-01T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO knowledge_entry
            (id, candidate_id, title, body, category, source_sprint, created_at)
        VALUES (1, 1, 'older', 'body', 'decision', '1', '2026-01-01T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO knowledge_entry
            (id, candidate_id, title, body, category, source_sprint, created_at, superseded_by)
        VALUES (2, 1, 'newer', 'body', 'pattern', '1', '2026-01-02T00:00:00Z', 1)
        """
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "kctl.db")
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


def test_fresh_database_admits_claim_kinds(conn: sqlite3.Connection) -> None:
    db.init_db(conn)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'knowledge_entry'"
    ).fetchone()[0]
    for category in CLAIM_KINDS | LEGACY_CATEGORIES:
        assert f"'{category}'" in sql


def test_upgrade_preserves_rows_and_supersession(conn: sqlite3.Connection) -> None:
    _seed_pre_migration_8_db(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 7

    db.init_db(conn)

    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == len(
        db._MIGRATIONS
    )
    assert conn.execute(
        "SELECT id, title, category, superseded_by FROM knowledge_entry ORDER BY id"
    ).fetchall() == [
        (1, "older", "decision", None),
        (2, "newer", "pattern", 1),
    ]


def test_upgrade_leaves_foreign_key_enforcement_on(conn: sqlite3.Connection) -> None:
    # The rebuild has to drop a self-referencing table, so it turns foreign keys
    # off. `PRAGMA foreign_keys` is a no-op inside a transaction, so a migration
    # that toggles it without committing first leaves the connection unprotected
    # and every later write unchecked.
    _seed_pre_migration_8_db(conn)
    db.init_db(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO knowledge_entry
                (candidate_id, title, body, category, source_sprint, created_at)
            VALUES (999, 'dangling', 'body', 'tenet', '1', '2026-01-03T00:00:00Z')
            """
        )
        conn.commit()


def test_claim_kinds_are_accepted_and_nonsense_is_not(conn: sqlite3.Connection) -> None:
    _seed_pre_migration_8_db(conn)
    db.init_db(conn)

    for index, category in enumerate(sorted(CLAIM_KINDS), start=10):
        conn.execute(
            """
            INSERT INTO knowledge_entry
                (id, candidate_id, title, body, category, source_sprint, created_at)
            VALUES (?, 1, ?, 'body', ?, '1', '2026-01-03T00:00:00Z')
            """,
            (index, f"claim-{category}", category),
        )
    conn.commit()
    assert set(
        row[0] for row in conn.execute("SELECT category FROM knowledge_entry")
    ) >= CLAIM_KINDS

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO knowledge_entry
                (candidate_id, title, body, category, source_sprint, created_at)
            VALUES (1, 'bad', 'body', 'nonsense', '1', '2026-01-03T00:00:00Z')
            """
        )


def test_publishable_categories_match_the_constraint(conn: sqlite3.Connection) -> None:
    # The CLI choice list, the publish guard and the CHECK must not drift apart.
    db.init_db(conn)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'knowledge_entry'"
    ).fetchone()[0]
    for category in VALID_CATEGORIES:
        assert f"'{category}'" in sql
    assert VALID_CATEGORIES == LEGACY_CATEGORIES | CLAIM_KINDS
