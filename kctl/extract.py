import json
import os
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

from . import db as _db

DEFAULT_EVENT_TYPES = {
    "decision",
    "blocker-resolved",
    "pattern-noted",
    "risk-accepted",
    "lesson-learned",
}


def get_sprintctl_db_path() -> Path:
    env = os.environ.get("SPRINTCTL_DB")
    if env:
        return Path(env)
    return Path.home() / ".sprintctl" / "sprintctl.db"


def build_candidate(event: dict, extracted_at: str) -> tuple[dict, bool]:
    """
    Build a candidate dict from a sprintctl event.
    Returns (candidate, has_structured_payload) where has_structured_payload is True
    when the event carried a non-empty JSON object with at least a 'summary' key.
    """
    raw_payload = event["payload"] if event["payload"] else "{}"
    try:
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, TypeError):
        payload = {}

    has_structured_payload = bool(payload.get("summary"))

    item_title = event["item_title"] if event["item_title"] else "no item"
    summary = payload.get("summary") or f'{event["event_type"]}: {item_title}'

    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    candidate = {
        "source_event_id": event["id"],
        "source_sprint_id": event["sprint_id"],
        "source_item_id": event["work_item_id"],
        "event_type": event["event_type"],
        "summary": summary,
        "detail": payload.get("detail"),
        "tags": json.dumps(tags),
        "confidence": payload.get("confidence"),
        "track_name": event["track_name"] if event["track_name"] else None,
        "extracted_at": extracted_at,
    }
    return candidate, has_structured_payload


def extract_candidates(
    sprintctl_conn: sqlite3.Connection,
    kctl_conn: sqlite3.Connection,
    sprintctl_db_path: str,
    event_types: set[str],
    since_event_id: int,
    sprint_id: int | None,
    now: str,
) -> tuple[list[dict], int]:
    """
    Scan sprintctl events for knowledge-bearing entries.
    Returns (created_candidates, structured_count) where structured_count is how many
    of the new candidates had a structured payload (vs. bare defaults).
    Idempotent: UNIQUE constraint on source_event_id prevents double-insertion.
    """
    placeholders = ",".join("?" * len(event_types))
    params: list = [since_event_id, *event_types]

    sprint_filter = ""
    if sprint_id is not None:
        sprint_filter = " AND e.sprint_id = ?"
        params.append(sprint_id)

    query = f"""
        SELECT
            e.id, e.sprint_id, e.work_item_id, e.event_type, e.payload,
            wi.title AS item_title,
            t.name   AS track_name
        FROM event e
        LEFT JOIN work_item wi ON e.work_item_id = wi.id
        LEFT JOIN track t      ON wi.track_id = t.id
        WHERE e.id > ?
          AND e.event_type IN ({placeholders})
          {sprint_filter}
        ORDER BY e.id ASC
    """

    events = sprintctl_conn.execute(query, params).fetchall()

    created = []
    structured_count = 0
    max_event_id = since_event_id

    for ev in events:
        ev_dict = dict(ev)
        if ev_dict["id"] > max_event_id:
            max_event_id = ev_dict["id"]

        candidate, has_structured = build_candidate(ev_dict, now)
        row_id = _db.insert_candidate(kctl_conn, candidate)
        if row_id is None:
            continue  # duplicate — UNIQUE constraint fired

        created.append(candidate)
        if has_structured:
            structured_count += 1

    _db.update_extractor_state(kctl_conn, sprintctl_db_path, max_event_id, now)
    return created, structured_count


def run_preflight(sprintctl_conn: sqlite3.Connection) -> list[str]:
    """
    Check for stale work items in active sprints.
    Returns a list of warning strings (empty = all clear).
    Tries to import sprintctl.calc first; falls back to a direct DB query.
    Extraction always proceeds regardless of warnings.
    """
    warnings: list[str] = []

    try:
        from datetime import datetime as _datetime
        from sprintctl import db as sc_db  # type: ignore[import]
        from sprintctl import calc as sc_calc  # type: ignore[import]
        from sprintctl.maintain import DEFAULT_STALE_THRESHOLD  # type: ignore[import]

        _raw = os.environ.get("SPRINTCTL_STALE_THRESHOLD")
        _threshold = timedelta(hours=float(_raw)) if _raw else DEFAULT_STALE_THRESHOLD
        _now = _datetime.now(timezone.utc)
        sprints = sc_db.list_sprints(sprintctl_conn)
        for sprint in sprints:
            if sprint.get("status") == "active":
                items = sc_db.list_work_items(sprintctl_conn, sprint_id=sprint["id"])
                stale = [
                    i for i in items
                    if sc_calc.item_staleness(i, _now, _threshold)["is_stale"]
                ]
                if stale:
                    _hrs = _threshold.total_seconds() / 3600
                    _label = (
                        f"{int(_hrs)} hours" if _hrs == int(_hrs)
                        else f"{_hrs:.1f} hours"
                    )
                    warnings.append(
                        f"Sprint '{sprint['name']}' has {len(stale)} stale item(s) "
                        f"(no activity in last {_label})"
                    )
        return warnings
    except ImportError:
        pass

    # Subprocess fallback removed: sprintctl maintain check --json is not yet implemented.
    # Instead replicate the stale-item check directly against the sprintctl DB.
    _raw = os.environ.get("SPRINTCTL_STALE_THRESHOLD")
    _stale_threshold = timedelta(hours=float(_raw)) if _raw else timedelta(hours=4)
    _threshold_sql = f"-{int(_stale_threshold.total_seconds())} seconds"
    _threshold_label = (
        f"{int(_stale_threshold.total_seconds() / 3600)} hours"
        if _stale_threshold.total_seconds() % 3600 == 0
        else f"{_stale_threshold.total_seconds() / 3600:.1f} hours"
    )
    try:
        active_sprints = sprintctl_conn.execute(
            "SELECT id, name FROM sprint WHERE status = 'active'"
        ).fetchall()
        for sprint in active_sprints:
            # Mirror sprintctl calc.item_staleness: active items idle > threshold are stale.
            # pending/done/blocked items are never stale (matches sprintctl default behaviour).
            stale_count = sprintctl_conn.execute(
                """
                SELECT COUNT(*) FROM work_item
                WHERE sprint_id = ?
                  AND status = 'active'
                  AND updated_at < datetime('now', ?)
                """,
                (sprint["id"], _threshold_sql),
            ).fetchone()[0]
            if stale_count:
                warnings.append(
                    f"Sprint '{sprint['name']}' has {stale_count} stale item(s) "
                    f"(no activity in last {_threshold_label})"
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Preflight check failed: {exc}")

    return warnings
