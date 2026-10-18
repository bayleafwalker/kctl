import json
import sqlite3

from . import db as _db

VALID_CATEGORIES = {
    "decision",
    "pattern",
    "lesson",
    "risk",
    "reference",
    # Claim kinds from the metanarrative model (the retired agentops
    # dispatch model README). A published tenet or direction keeps
    # its kind here rather than flattening to `decision`.
    "tenet",
    "direction",
}


def publish_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    title: str | None,
    body: str,
    category: str,
    tags: str | None,
    now: str,
    supersedes_entry_id: int | None = None,
    coordination: bool = False,
) -> dict:
    """Publish in one local transaction, retaining the original review evidence.

    The caller must provide an idle connection: this command owns its commit
    and rollback and must never take over unrelated caller writes.
    """
    if conn.in_transaction:
        raise ValueError("Cannot publish inside an active transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        entry = _publish_candidate(
            conn, candidate_id, title, body, category, tags, now,
            supersedes_entry_id, coordination,
        )
        conn.commit()
        return entry
    except BaseException:
        conn.rollback()
        raise


def _check_publication(conn: sqlite3.Connection, candidate: dict) -> None:
    """Refuse historical partial/duplicate state without rewriting its evidence."""
    count = conn.execute(
        "SELECT COUNT(*) FROM knowledge_entry WHERE candidate_id = ?",
        (candidate["id"],),
    ).fetchone()[0]
    expected = 1 if candidate["status"] == "published" else 0
    if count != expected:
        raise ValueError(
            f"Candidate #{candidate['id']} has inconsistent publication "
            f"(status {candidate['status']}, {count} entries); "
            "explicit reconciliation is required"
        )


def _publish_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    title: str | None,
    body: str,
    category: str,
    tags: str | None,
    now: str,
    supersedes_entry_id: int | None,
    coordination: bool,
) -> dict:
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"
        )

    tags_json = "[]"
    if tags is not None:
        try:
            parsed = json.loads(tags)
            if not isinstance(parsed, list):
                raise ValueError("--tags must be a JSON array")
            tags_json = json.dumps(parsed)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid tags JSON: {exc}") from exc

    candidate = _db.get_candidate(conn, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate #{candidate_id} not found")
    _check_publication(conn, candidate)
    candidate_kind = candidate.get("candidate_kind", "durable")
    if candidate_kind == "coordination" and not coordination:
        raise ValueError(
            f"Candidate #{candidate_id} is 'coordination' — pass coordination=True or use kctl publish --coordination"
        )
    if candidate["status"] != "approved":
        raise ValueError(
            f"Candidate #{candidate_id} is '{candidate['status']}' — only approved candidates can be published"
        )
    if supersedes_entry_id is not None:
        target = _db.get_entry(conn, supersedes_entry_id)
        if target is None:
            raise ValueError(f"Entry #{supersedes_entry_id} not found")
        target_candidate = _db.get_candidate(conn, target["candidate_id"])
        if target_candidate is None:
            raise ValueError(
                f"Entry #{supersedes_entry_id} has inconsistent publication "
                "(missing candidate); explicit reconciliation is required"
            )
        _check_publication(conn, target_candidate)

    effective_title = title or candidate["summary"]
    if not effective_title:
        raise ValueError("Title is required (candidate has no summary)")

    # Resolve source sprint name from the source_sprint_id stored on the candidate.
    # We only have the ID here; store it as a string so render can use it.
    source_sprint = str(candidate["source_sprint_id"])

    entry = {
        "candidate_id": candidate_id,
        "title": effective_title,
        "body": body,
        "tags": tags_json if tags is not None else (candidate.get("tags") or "[]"),
        "category": category,
        "source_sprint": source_sprint,
        "source_track": candidate.get("source_track"),
        "source_kind": candidate_kind,
        "created_at": now,
    }
    entry_id = _db.insert_entry(conn, entry, commit=False)
    if supersedes_entry_id is not None:
        _db.set_entry_superseded_by(conn, supersedes_entry_id, entry_id, commit=False)

    # Transition candidate to published
    _db.transition_candidate(
        conn,
        candidate_id=candidate_id,
        new_status="published",
        reviewed_at=candidate["reviewed_at"],
        reviewed_by=candidate["reviewed_by"],
        review_notes=candidate["review_notes"],
        commit=False,
    )

    return _db.get_entry(conn, entry_id)
