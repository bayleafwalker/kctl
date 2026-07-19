"""Regression coverage for the migration-6 column rename drift (sprintctl kctl#108).

An earlier revision of migration 6 created knowledge_entry.source_candidate_kind;
source was later edited to create source_kind instead, without bumping the
migration version. Any DB that had already run the old migration 6 was stuck:
schema_version already read 6, so the version-gated loop never re-ran it, and
the column was permanently missing under the name current code expects.
"""

import sqlite3

from kctl import db


def _seed_pre_migration_7_db(conn: sqlite3.Connection, entry_column: str) -> None:
    """Build a DB at schema_version=6, as if migration 6 had used `entry_column`."""
    for statement in db._MIGRATIONS[0].split(";"):  # migration 1: base tables
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    conn.execute(
        f"ALTER TABLE knowledge_entry ADD COLUMN {entry_column} TEXT NOT NULL DEFAULT 'durable'"
    )
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (6)")
    conn.execute(
        """
        INSERT INTO knowledge_candidate
            (source_event_id, source_sprint_id, event_type, summary, status, extracted_at)
        VALUES (1, 1, 'decision', 'seed candidate', 'approved', '2026-01-01T00:00:00Z')
        """
    )
    conn.execute(
        f"""
        INSERT INTO knowledge_entry
            (candidate_id, title, body, category, source_sprint, created_at, {entry_column})
        VALUES (1, 'seed title', 'seed body', 'decision', '1', '2026-01-01T00:00:00Z', 'durable')
        """
    )
    conn.commit()


def test_repairs_old_column_name_and_preserves_data(tmp_path):
    path = tmp_path / "kctl.db"
    conn = sqlite3.connect(str(path))
    _seed_pre_migration_7_db(conn, "source_candidate_kind")

    db.init_db(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_entry)")}
    assert "source_kind" in columns
    assert "source_candidate_kind" not in columns
    row = conn.execute("SELECT title, source_kind FROM knowledge_entry WHERE id = 1").fetchone()
    assert row == ("seed title", "durable")
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == len(db._MIGRATIONS)


def test_noop_when_column_already_correctly_named(tmp_path):
    path = tmp_path / "kctl.db"
    conn = sqlite3.connect(str(path))
    _seed_pre_migration_7_db(conn, "source_kind")

    db.init_db(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_entry)")}
    assert "source_kind" in columns
    row = conn.execute("SELECT title, source_kind FROM knowledge_entry WHERE id = 1").fetchone()
    assert row == ("seed title", "durable")


def test_fresh_db_reaches_current_schema_version_via_normal_path(tmp_path):
    path = tmp_path / "kctl.db"
    conn = db.get_connection(path)

    db.init_db(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_entry)")}
    assert "source_kind" in columns
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == len(db._MIGRATIONS)
