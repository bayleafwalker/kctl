import json
import os
import sqlite3
from pathlib import Path
from typing import Callable, Union

VALID_CANDIDATE_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"approved", "rejected"},
    "approved": {"published"},
    "rejected": set(),
    "published": set(),
}

DEFAULT_EXTRACT_SCOPE_KEY = (
    "scope:v1|sprint=*|events="
    "blocker-resolved,claim-ambiguity-detected,claim-handoff,"
    "claim-ownership-corrected,coordination-failure,decision,"
    "lesson-learned,pattern-noted,risk-accepted"
)
COORDINATION_EVENT_TYPES = {
    "claim-handoff",
    "claim-ownership-corrected",
    "claim-ambiguity-detected",
    "coordination-failure",
}

def _migrate_8_widen_entry_categories(conn: sqlite3.Connection) -> None:
    """Admit `tenet` and `direction` as knowledge categories.

    Tenets, directions, practices and decisions are all claims in the
    metanarrative model (agentops `templates/dispatch/model/README.md`), so a
    published claim should keep its kind rather than flatten to `decision`.

    SQLite cannot alter a CHECK constraint, so the table is rebuilt. This runs in
    Python because `knowledge_entry.superseded_by` references the table itself:
    foreign keys have to be off for the swap, and `PRAGMA foreign_keys` is a no-op
    inside a transaction -- so each toggle commits first, or the pragma silently
    does nothing and the connection is left with enforcement off. Migration 1 is left
    untouched -- a fresh database reaches the same shape by running 1 through 8,
    which is what keeps new and existing databases from diverging the way they did
    at migration 7.
    """

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            CREATE TABLE knowledge_entry_migrated (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id   INTEGER NOT NULL REFERENCES knowledge_candidate(id),
                title          TEXT    NOT NULL,
                body           TEXT    NOT NULL,
                tags           TEXT    NOT NULL DEFAULT '[]',
                category       TEXT    NOT NULL
                                       CHECK (category IN (
                                           'decision', 'pattern', 'lesson', 'risk',
                                           'reference', 'tenet', 'direction'
                                       )),
                source_sprint  TEXT    NOT NULL,
                source_track   TEXT,
                created_at     TEXT    NOT NULL,
                superseded_by  INTEGER REFERENCES knowledge_entry_migrated(id),
                source_kind    TEXT    NOT NULL DEFAULT 'durable'
            )
            """
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(knowledge_entry)")]
        shared = [
            name
            for name in columns
            if name
            in {
                "id",
                "candidate_id",
                "title",
                "body",
                "tags",
                "category",
                "source_sprint",
                "source_track",
                "created_at",
                "superseded_by",
                "source_kind",
            }
        ]
        joined = ", ".join(shared)
        conn.execute(
            f"INSERT INTO knowledge_entry_migrated ({joined}) "
            f"SELECT {joined} FROM knowledge_entry"
        )
        conn.execute("DROP TABLE knowledge_entry")
        conn.execute("ALTER TABLE knowledge_entry_migrated RENAME TO knowledge_entry")
    finally:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_9_admit_non_sprintctl_candidates(conn: sqlite3.Connection) -> None:
    """Admit candidates that did not come from a sprintctl event.

    `knowledge_candidate` was shaped entirely around extraction: every row needed a
    `source_event_id INTEGER NOT NULL UNIQUE` and a `source_sprint_id INTEGER NOT
    NULL`, because the only way in was `kctl extract` reading sprintctl events. That
    made the documented claims path impossible to walk. agentops
    `templates/dispatch/model/README.md` says "kctl is the claims store… `publish`
    hands a claim over as a knowledge entry", but a metanarrative claim has no
    sprintctl event and no sprint, so it could not become a candidate and therefore
    could not be published. The store's own contract was unreachable through its own
    schema.

    Both columns become nullable and a `source_origin` discriminator is added,
    defaulting to `sprintctl` so every existing row keeps its meaning. Extraction is
    unchanged: it still supplies an event id, and the UNIQUE constraint still dedupes
    it, because SQLite permits many NULLs in a UNIQUE column and constrains only the
    non-null ones — which is exactly the semantics wanted here.

    SQLite cannot drop NOT NULL in place, so the table is rebuilt. Foreign keys are
    toggled around the swap because `knowledge_entry.candidate_id` references this
    table, and `PRAGMA foreign_keys` is a no-op inside a transaction — so each toggle
    commits first, following migration 8.
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_candidate)")}
    if "source_origin" in columns:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            CREATE TABLE knowledge_candidate_migrated (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source_event_id  INTEGER UNIQUE,
                source_sprint_id INTEGER,
                source_item_id   INTEGER,
                source_track     TEXT,
                source_actor     TEXT,
                source_type      TEXT,
                source_created_at TEXT,
                source_payload   TEXT,
                source_origin    TEXT    NOT NULL DEFAULT 'sprintctl',
                event_type       TEXT    NOT NULL,
                candidate_kind   TEXT    NOT NULL DEFAULT 'durable',
                summary          TEXT    NOT NULL,
                detail           TEXT,
                tags             TEXT,
                confidence       TEXT,
                status           TEXT    NOT NULL DEFAULT 'candidate'
                                         CHECK (status IN ('candidate', 'approved', 'rejected', 'published')),
                extracted_at     TEXT    NOT NULL,
                reviewed_at      TEXT,
                reviewed_by      TEXT,
                review_notes     TEXT
            )
            """
        )
        existing = [row[1] for row in conn.execute("PRAGMA table_info(knowledge_candidate)")]
        target = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_candidate_migrated)")}
        shared = [name for name in existing if name in target]
        joined = ", ".join(shared)
        conn.execute(
            f"INSERT INTO knowledge_candidate_migrated ({joined}) "
            f"SELECT {joined} FROM knowledge_candidate"
        )
        conn.execute("DROP TABLE knowledge_candidate")
        conn.execute(
            "ALTER TABLE knowledge_candidate_migrated RENAME TO knowledge_candidate"
        )
    finally:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_7_fix_source_kind_column_name(conn: sqlite3.Connection) -> None:
    """Repair knowledge_entry.source_candidate_kind -> source_kind (see migration 7 note)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_entry)")}
    if "source_kind" in columns:
        return
    if "source_candidate_kind" in columns:
        conn.execute(
            "ALTER TABLE knowledge_entry RENAME COLUMN source_candidate_kind TO source_kind"
        )
    else:
        conn.execute(
            "ALTER TABLE knowledge_entry ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'durable'"
        )


_MIGRATIONS: list[Union[str, Callable[[sqlite3.Connection], None]]] = [
    # Migration 1: initial schema (Phase 1 + Phase 2 tables pre-created)
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidate (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_event_id  INTEGER NOT NULL UNIQUE,
        source_sprint_id INTEGER NOT NULL,
        source_item_id   INTEGER,
        event_type       TEXT    NOT NULL,
        summary          TEXT    NOT NULL,
        detail           TEXT,
        tags             TEXT,
        confidence       TEXT,
        status           TEXT    NOT NULL DEFAULT 'candidate'
                                 CHECK (status IN ('candidate', 'approved', 'rejected', 'published')),
        extracted_at     TEXT    NOT NULL,
        reviewed_at      TEXT,
        reviewed_by      TEXT,
        review_notes     TEXT
    );

    CREATE TABLE IF NOT EXISTS knowledge_entry (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id   INTEGER NOT NULL REFERENCES knowledge_candidate(id),
        title          TEXT    NOT NULL,
        body           TEXT    NOT NULL,
        tags           TEXT    NOT NULL DEFAULT '[]',
        category       TEXT    NOT NULL
                               CHECK (category IN ('decision', 'pattern', 'lesson', 'risk', 'reference')),
        source_sprint  TEXT    NOT NULL,
        source_track   TEXT,
        created_at     TEXT    NOT NULL,
        superseded_by  INTEGER REFERENCES knowledge_entry(id)
    );

    CREATE TABLE IF NOT EXISTS extractor_state (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        sprintctl_db_path  TEXT    NOT NULL UNIQUE,
        last_event_id      INTEGER NOT NULL DEFAULT 0,
        last_run_at        TEXT    NOT NULL
    );
    """,
    # Migration 2: add source_track to knowledge_candidate
    """
    ALTER TABLE knowledge_candidate ADD COLUMN source_track TEXT
    """,
    # Migration 3: preserve source event context for handoff and coordination analysis.
    """
    ALTER TABLE knowledge_candidate ADD COLUMN source_actor TEXT;
    ALTER TABLE knowledge_candidate ADD COLUMN source_type TEXT;
    ALTER TABLE knowledge_candidate ADD COLUMN source_created_at TEXT;
    ALTER TABLE knowledge_candidate ADD COLUMN source_payload TEXT
    """,
    # Migration 4: extractor state must be scoped by extraction filter to avoid
    # filtered runs advancing a single global watermark incorrectly.
    f"""
    CREATE TABLE IF NOT EXISTS extractor_state_v2 (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        sprintctl_db_path  TEXT    NOT NULL,
        scope_key          TEXT    NOT NULL,
        last_event_id      INTEGER NOT NULL DEFAULT 0,
        last_run_at        TEXT    NOT NULL,
        UNIQUE (sprintctl_db_path, scope_key)
    );

    INSERT OR IGNORE INTO extractor_state_v2
        (sprintctl_db_path, scope_key, last_event_id, last_run_at)
    SELECT sprintctl_db_path, '{DEFAULT_EXTRACT_SCOPE_KEY}', last_event_id, last_run_at
    FROM extractor_state
    """,
    # Migration 5: durable knowledge and coordination items are reviewed
    # separately; backfill existing rows from event_type.
    """
    ALTER TABLE knowledge_candidate ADD COLUMN candidate_kind TEXT NOT NULL DEFAULT 'durable';

    UPDATE knowledge_candidate
    SET candidate_kind = CASE
        WHEN event_type IN (
            'claim-handoff',
            'claim-ownership-corrected',
            'claim-ambiguity-detected',
            'coordination-failure'
        ) THEN 'coordination'
        ELSE 'durable'
    END
    """,
    # Migration 6: published entries must preserve which stream they came from
    # so durable and coordination knowledge can render separately.
    """
    ALTER TABLE knowledge_entry ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'durable';

    UPDATE knowledge_entry
    SET source_kind = COALESCE(
        (
            SELECT candidate_kind
            FROM knowledge_candidate
            WHERE knowledge_candidate.id = knowledge_entry.candidate_id
        ),
        'durable'
    )
    """,
    # Migration 7: repair DBs where an earlier revision of migration 6 created
    # knowledge_entry.source_candidate_kind instead of source_kind. That column
    # was renamed in source without bumping the migration version, so any DB
    # that had already run migration 6 under the old name never picked up the
    # rename (schema_version stayed at 6, the version check never re-triggers
    # it). Conditional on prior state, so this must run in Python, not raw SQL.
    _migrate_7_fix_source_kind_column_name,
    # Migration 8: widen knowledge_entry.category so a published claim keeps its
    # kind. Rebuilds the table, because SQLite cannot alter a CHECK.
    _migrate_8_widen_entry_categories,
    # Migration 9: admit candidates with no sprintctl event behind them, so the
    # claims path the model README already documents can actually be walked.
    _migrate_9_admit_non_sprintctl_candidates,
]


def get_db_path() -> Path:
    env = os.environ.get("KCTL_DB")
    if env:
        return Path(env)
    return Path.home() / ".kctl" / "kctl.db"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_sprintctl_connection(sprintctl_db_path: Path) -> sqlite3.Connection:
    """Open sprintctl DB read-only."""
    uri = f"file:{sprintctl_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (0)")
        current = 0
    else:
        current = row[0]

    for i, migration in enumerate(_MIGRATIONS):
        target_version = i + 1
        if current < target_version:
            if callable(migration):
                migration(conn)
            else:
                for statement in migration.split(";"):
                    stmt = statement.strip()
                    if stmt:
                        conn.execute(stmt)
            conn.execute("UPDATE schema_version SET version = ?", (target_version,))
            current = target_version

    conn.commit()


# --- KnowledgeCandidate ---

def insert_candidate(conn: sqlite3.Connection, candidate: dict) -> int | None:
    """Insert a candidate. Returns the new row id, or None if already exists."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO knowledge_candidate
            (source_event_id, source_sprint_id, source_item_id, source_track,
             source_actor, source_type, source_created_at, source_payload,
             source_origin, event_type, candidate_kind, summary, detail, tags,
             confidence, status, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
        """,
        (
            candidate.get("source_event_id"),
            candidate.get("source_sprint_id"),
            candidate.get("source_item_id"),
            candidate.get("track_name"),
            candidate.get("source_actor"),
            candidate.get("source_type"),
            candidate.get("source_created_at"),
            candidate.get("source_payload"),
            candidate.get("source_origin", "sprintctl"),
            candidate["event_type"],
            candidate.get("candidate_kind", "durable"),
            candidate["summary"],
            candidate.get("detail"),
            candidate.get("tags", "[]"),
            candidate.get("confidence"),
            candidate["extracted_at"],
        ),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM knowledge_candidate WHERE id = ?", (candidate_id,)
    ).fetchone()
    return dict(row) if row else None


def list_candidates(
    conn: sqlite3.Connection,
    status: str | None = "candidate",
    tag: str | None = None,
    sprint_id: int | None = None,
    candidate_kind: str | None = None,
) -> list[dict]:
    where_clauses = []
    params: list = []

    if status is not None:
        where_clauses.append("status = ?")
        params.append(status)
    if sprint_id is not None:
        where_clauses.append("source_sprint_id = ?")
        params.append(sprint_id)
    if candidate_kind is not None:
        where_clauses.append("candidate_kind = ?")
        params.append(candidate_kind)

    query = "SELECT * FROM knowledge_candidate"
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    query += " ORDER BY extracted_at DESC"

    rows = conn.execute(query, params).fetchall()
    results = [dict(r) for r in rows]

    if tag is not None:
        results = [r for r in results if tag in json.loads(r.get("tags") or "[]")]

    return results


def transition_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    new_status: str,
    reviewed_at: str,
    reviewed_by: str,
    review_notes: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    tags: str | None = None,
) -> dict:
    """
    Transition a candidate's status. Enforces valid transitions.
    Returns the updated candidate dict.
    """
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate #{candidate_id} not found")

    current_status = candidate["status"]
    allowed = VALID_CANDIDATE_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition candidate #{candidate_id} from '{current_status}' to '{new_status}'"
        )

    updates = {
        "status": new_status,
        "reviewed_at": reviewed_at,
        "reviewed_by": reviewed_by,
        "review_notes": review_notes,
    }
    if title is not None:
        updates["summary"] = title
    if detail is not None:
        updates["detail"] = detail
    if tags is not None:
        updates["tags"] = tags

    set_clauses = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [candidate_id]
    conn.execute(
        f"UPDATE knowledge_candidate SET {set_clauses} WHERE id = ?", params
    )
    conn.commit()
    return get_candidate(conn, candidate_id)


# --- KnowledgeEntry ---

def insert_entry(conn: sqlite3.Connection, entry: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO knowledge_entry
            (candidate_id, title, body, tags, category, source_sprint, source_track, source_kind, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry["candidate_id"],
            entry["title"],
            entry["body"],
            entry.get("tags", "[]"),
            entry["category"],
            entry["source_sprint"],
            entry.get("source_track"),
            entry.get("source_kind", "durable"),
            entry["created_at"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM knowledge_entry WHERE id = ?", (entry_id,)
    ).fetchone()
    return dict(row) if row else None


def list_entries(
    conn: sqlite3.Connection,
    category: str | None = None,
    tag: str | None = None,
    sprint_id: int | None = None,
    source_kind: str | None = None,
) -> list[dict]:
    where_clauses = []
    params: list = []

    if category is not None:
        where_clauses.append("category = ?")
        params.append(category)
    if sprint_id is not None:
        where_clauses.append("source_sprint = ?")
        params.append(str(sprint_id))
    if source_kind is not None:
        where_clauses.append("source_kind = ?")
        params.append(source_kind)

    query = "SELECT * FROM knowledge_entry"
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    query += " ORDER BY created_at DESC"

    rows = conn.execute(query, params).fetchall()
    results = [dict(r) for r in rows]

    if tag is not None:
        results = [r for r in results if tag in json.loads(r.get("tags") or "[]")]

    return results


# --- ExtractorState ---

def get_extractor_state(
    conn: sqlite3.Connection,
    sprintctl_db_path: str,
    scope_key: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM extractor_state_v2
        WHERE sprintctl_db_path = ? AND scope_key = ?
        """,
        (sprintctl_db_path, scope_key),
    ).fetchone()
    return dict(row) if row else None


def update_extractor_state(
    conn: sqlite3.Connection,
    sprintctl_db_path: str,
    scope_key: str,
    last_event_id: int,
    last_run_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO extractor_state_v2 (sprintctl_db_path, scope_key, last_event_id, last_run_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sprintctl_db_path, scope_key) DO UPDATE SET
            last_event_id = excluded.last_event_id,
            last_run_at   = excluded.last_run_at
        """,
        (sprintctl_db_path, scope_key, last_event_id, last_run_at),
    )
    conn.commit()


def set_entry_superseded_by(
    conn: sqlite3.Connection,
    entry_id: int,
    superseded_by: int,
) -> None:
    if entry_id == superseded_by:
        raise ValueError("An entry cannot supersede itself")

    entry = get_entry(conn, entry_id)
    if entry is None:
        raise ValueError(f"Entry #{entry_id} not found")

    successor = get_entry(conn, superseded_by)
    if successor is None:
        raise ValueError(f"Entry #{superseded_by} not found")

    if entry.get("superseded_by") and entry["superseded_by"] != superseded_by:
        raise ValueError(
            f"Entry #{entry_id} is already superseded by entry #{entry['superseded_by']}"
        )

    conn.execute(
        "UPDATE knowledge_entry SET superseded_by = ? WHERE id = ?",
        (superseded_by, entry_id),
    )
    conn.commit()


# --- Schema validation for sprintctl DB ---

REQUIRED_SPRINTCTL_TABLES = {"sprint", "track", "work_item", "event"}
REQUIRED_EVENT_COLUMNS = {
    "id",
    "sprint_id",
    "work_item_id",
    "source_type",
    "actor",
    "event_type",
    "payload",
    "created_at",
}
REQUIRED_WORK_ITEM_COLUMNS = {"id", "title", "track_id", "status", "updated_at"}
# Statuses kctl depends on — preflight query and staleness logic both reference these.
REQUIRED_WORK_ITEM_STATUSES = {"pending", "active", "done", "blocked"}


def validate_sprintctl_schema(sprintctl_conn: sqlite3.Connection) -> None:
    """Raise ValueError if sprintctl DB schema is incompatible."""
    tables = {
        row[0]
        for row in sprintctl_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = REQUIRED_SPRINTCTL_TABLES - tables
    if missing_tables:
        raise ValueError(
            f"sprintctl DB schema mismatch — missing tables: {', '.join(sorted(missing_tables))}. "
            "Check that sprintctl is up to date."
        )

    cols = {
        row[1]
        for row in sprintctl_conn.execute("PRAGMA table_info(event)").fetchall()
    }
    missing_cols = REQUIRED_EVENT_COLUMNS - cols
    if missing_cols:
        raise ValueError(
            f"sprintctl DB schema mismatch — event table missing columns: {', '.join(sorted(missing_cols))}. "
            "Check that sprintctl is up to date."
        )

    wi_cols = {
        row[1]
        for row in sprintctl_conn.execute("PRAGMA table_info(work_item)").fetchall()
    }
    missing_wi_cols = REQUIRED_WORK_ITEM_COLUMNS - wi_cols
    if missing_wi_cols:
        raise ValueError(
            f"sprintctl DB schema mismatch — work_item table missing columns: {', '.join(sorted(missing_wi_cols))}. "
            "Check that sprintctl is up to date."
        )

    # Verify the work_item CHECK constraint covers all status values kctl depends on.
    # Probe by attempting an INSERT with each required status value against an in-memory
    # shadow of the DDL, avoiding any write to the read-only sprintctl connection.
    # Reads the DDL from sqlite_master — no write access required.
    ddl_row = sprintctl_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_item'"
    ).fetchone()
    ddl = ddl_row[0] if ddl_row else ""
    # Use a regex to extract all quoted tokens from the CHECK clause so the
    # check is insensitive to whitespace or quote style (single vs double) in the DDL.
    import re as _re
    quoted_tokens = set(_re.findall(r"""['"]([\w-]+)['"]""", ddl))
    missing_statuses = REQUIRED_WORK_ITEM_STATUSES - quoted_tokens
    if missing_statuses:
        raise ValueError(
            f"sprintctl DB schema mismatch — work_item.status CHECK constraint missing "
            f"expected values: {', '.join(sorted(missing_statuses))}. "
            "Check that sprintctl is up to date."
        )
