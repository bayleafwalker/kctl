import json
import sqlite3

from . import db as _db

VALID_CATEGORIES = {"decision", "pattern", "lesson", "risk", "reference"}


def publish_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    title: str | None,
    body: str,
    category: str,
    tags: str | None,
    now: str,
    supersedes_entry_id: int | None = None,
    allow_coordination: bool = False,
) -> dict:
    """Promote an approved candidate to a knowledge_entry."""
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
    candidate_kind = candidate.get("candidate_kind", "durable")
    if candidate_kind == "coordination" and not allow_coordination:
        raise ValueError(
            f"Candidate #{candidate_id} is 'coordination' — use --coordination to publish an approved coordination candidate"
        )
    if candidate_kind not in {"durable", "coordination"}:
        raise ValueError(
            f"Candidate #{candidate_id} has unsupported candidate kind '{candidate_kind}'"
        )
    if candidate["status"] != "approved":
        raise ValueError(
            f"Candidate #{candidate_id} is '{candidate['status']}' — only approved candidates can be published"
        )
    if supersedes_entry_id is not None and _db.get_entry(conn, supersedes_entry_id) is None:
        raise ValueError(f"Entry #{supersedes_entry_id} not found")

    effective_title = title or candidate["summary"]
    if not effective_title:
        raise ValueError("Title is required (candidate has no summary)")

    # Resolve source sprint name from the source_sprint_id stored on the candidate.
    # We only have the ID here; store it as a string so render can use it.
    source_sprint = str(candidate["source_sprint_id"])

    entry = {
        "candidate_id": candidate_id,
        "source_candidate_kind": candidate_kind,
        "title": effective_title,
        "body": body,
        "tags": tags_json if tags is not None else (candidate.get("tags") or "[]"),
        "category": category,
        "source_sprint": source_sprint,
        "source_track": candidate.get("source_track"),
        "created_at": now,
    }
    entry_id = _db.insert_entry(conn, entry)
    if supersedes_entry_id is not None:
        _db.set_entry_superseded_by(conn, supersedes_entry_id, entry_id)

    # Transition candidate to published
    _db.transition_candidate(
        conn,
        candidate_id=candidate_id,
        new_status="published",
        reviewed_at=now,
        reviewed_by="publish",
    )

    return _db.get_entry(conn, entry_id)
