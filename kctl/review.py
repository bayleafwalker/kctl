import json
import sqlite3

from . import db as _db


def approve_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    now: str,
    reviewed_by: str = "human",
    title: str | None = None,
    detail: str | None = None,
    tags: str | None = None,
) -> dict:
    """Approve a candidate, optionally editing title, detail, and tags."""
    tags_json: str | None = None
    if tags is not None:
        try:
            parsed = json.loads(tags)
            if not isinstance(parsed, list):
                raise ValueError("tags must be a JSON array")
            tags_json = json.dumps(parsed)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid tags JSON: {exc}") from exc

    return _db.transition_candidate(
        conn,
        candidate_id=candidate_id,
        new_status="approved",
        reviewed_at=now,
        reviewed_by=reviewed_by,
        title=title,
        detail=detail,
        tags=tags_json,
    )


def reject_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    now: str,
    reviewed_by: str = "human",
    reason: str | None = None,
) -> dict:
    """Reject a candidate with an optional reason."""
    return _db.transition_candidate(
        conn,
        candidate_id=candidate_id,
        new_status="rejected",
        reviewed_at=now,
        reviewed_by=reviewed_by,
        review_notes=reason,
    )
