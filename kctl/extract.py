import json
import os
import sqlite3
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


def build_candidate(event: dict, extracted_at: str) -> dict:
    raw_payload = event["payload"] if event["payload"] else "{}"
    try:
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, TypeError):
        payload = {}

    item_title = event["item_title"] if event["item_title"] else "no item"
    summary = payload.get("summary") or f'{event["event_type"]}: {item_title}'

    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    return {
        "source_event_id": event["id"],
        "source_sprint_id": event["sprint_id"],
        "source_item_id": event["work_item_id"],
        "event_type": event["event_type"],
        "summary": summary,
        "detail": payload.get("detail"),
        "tags": json.dumps(tags),
        "confidence": payload.get("confidence"),
        "extracted_at": extracted_at,
    }


def extract_candidates(
    sprintctl_conn: sqlite3.Connection,
    kctl_conn: sqlite3.Connection,
    sprintctl_db_path: str,
    event_types: set[str],
    since_event_id: int,
    sprint_id: int | None,
    now: str,
) -> list[dict]:
    """
    Scan sprintctl events for knowledge-bearing entries.
    Returns list of newly created candidates (skips duplicates).
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
    max_event_id = since_event_id

    for ev in events:
        ev_dict = dict(ev)
        if _db.candidate_exists(kctl_conn, ev_dict["id"]):
            if ev_dict["id"] > max_event_id:
                max_event_id = ev_dict["id"]
            continue

        candidate = build_candidate(ev_dict, now)
        _db.insert_candidate(kctl_conn, candidate)
        created.append(candidate)

        if ev_dict["id"] > max_event_id:
            max_event_id = ev_dict["id"]

    _db.update_extractor_state(kctl_conn, sprintctl_db_path, max_event_id, now)
    return created


def run_preflight(sprintctl_conn: sqlite3.Connection) -> list[str]:
    """
    Try to import sprintctl.calc for maintain-check diagnostics.
    Returns a list of warning strings (empty = all clear).
    Falls back to subprocess if import fails.
    Extraction always proceeds regardless.
    """
    warnings: list[str] = []

    try:
        from sprintctl import db as sc_db  # type: ignore[import]
        from sprintctl import calc as sc_calc  # type: ignore[import]

        sprints = sc_db.list_sprints(sprintctl_conn)
        for sprint in sprints:
            if sprint.get("status") == "active":
                items = sc_db.list_work_items(sprintctl_conn, sprint_id=sprint["id"])
                stale = [i for i in items if sc_calc.is_stale(i)]
                if stale:
                    warnings.append(
                        f"Sprint '{sprint['name']}' has {len(stale)} stale item(s)"
                    )
        return warnings
    except ImportError:
        pass

    # Subprocess fallback
    import subprocess
    try:
        result = subprocess.run(
            ["sprintctl", "maintain", "check", "--json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            warnings.append(f"sprintctl maintain check failed: {result.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        warnings.append(
            "Could not run sprintctl maintain check — sprintctl not found in PATH. "
            "Proceeding with extraction."
        )

    return warnings
