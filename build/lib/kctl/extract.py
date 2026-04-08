import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import db as _db

DEFAULT_DURABLE_EVENT_TYPES = {
    "decision",
    "blocker-resolved",
    "pattern-noted",
    "risk-accepted",
    "lesson-learned",
}
DEFAULT_COORDINATION_EVENT_TYPES = {
    "claim-handoff",
    "claim-ownership-corrected",
    "claim-ambiguity-detected",
    "coordination-failure",
}
DEFAULT_EVENT_TYPES = DEFAULT_DURABLE_EVENT_TYPES | DEFAULT_COORDINATION_EVENT_TYPES


def get_sprintctl_db_path() -> Path:
    env = os.environ.get("SPRINTCTL_DB")
    if env:
        return Path(env)
    return Path.home() / ".sprintctl" / "sprintctl.db"


def resolve_event_types(raw: str | None = None) -> set[str]:
    configured = raw if raw is not None else os.environ.get("KCTL_EVENT_TYPES")
    if not configured:
        return set(DEFAULT_EVENT_TYPES)

    event_types = {item.strip() for item in configured.split(",") if item.strip()}
    if not event_types:
        raise ValueError("Event type filter cannot be empty")
    return event_types


def build_scope_key(event_types: set[str], sprint_id: int | None) -> str:
    sprint_label = str(sprint_id) if sprint_id is not None else "*"
    event_label = ",".join(sorted(event_types))
    return f"scope:v1|sprint={sprint_label}|events={event_label}"


def candidate_kind_for_event_type(event_type: str) -> str:
    if event_type in DEFAULT_COORDINATION_EVENT_TYPES:
        return "coordination"
    return "durable"


def build_candidate(event: dict, extracted_at: str) -> tuple[dict, bool]:
    """
    Build a candidate dict from a sprintctl event.
    Returns (candidate, has_structured_payload) where has_structured_payload is True
    when the event carried a non-empty JSON object with at least a 'summary' key.
    """
    raw_payload = event.get("payload") if event.get("payload") else "{}"
    try:
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, TypeError):
        payload = {}

    has_structured_payload = bool(payload.get("summary"))

    item_title = event.get("item_title") if event.get("item_title") else "no item"
    summary = payload.get("summary") or f'{event["event_type"]}: {item_title}'

    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    candidate = {
        "source_event_id": event["id"],
        "source_sprint_id": event["sprint_id"],
        "source_item_id": event["work_item_id"],
        "source_actor": event.get("actor"),
        "source_type": event.get("source_type"),
        "source_created_at": event.get("created_at"),
        "source_payload": raw_payload,
        "event_type": event["event_type"],
        "candidate_kind": candidate_kind_for_event_type(event["event_type"]),
        "summary": summary,
        "detail": payload.get("detail"),
        "tags": json.dumps(tags),
        "confidence": payload.get("confidence"),
        "track_name": event.get("track_name") if event.get("track_name") else None,
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
    if not event_types:
        raise ValueError("Event type filter cannot be empty")

    scope_key = build_scope_key(event_types, sprint_id)
    placeholders = ",".join("?" * len(event_types))
    params: list = [since_event_id, *event_types]

    sprint_filter = ""
    if sprint_id is not None:
        sprint_filter = " AND e.sprint_id = ?"
        params.append(sprint_id)

    query = f"""
        SELECT
            e.id, e.sprint_id, e.work_item_id, e.source_type, e.actor,
            e.event_type, e.payload, e.created_at,
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

    _db.update_extractor_state(
        kctl_conn,
        sprintctl_db_path=sprintctl_db_path,
        scope_key=scope_key,
        last_event_id=max_event_id,
        last_run_at=now,
    )
    return created, structured_count


def _format_hours_label(hours: float | None) -> str:
    if hours is None:
        return "off"
    if hours == int(hours):
        return f"{int(hours)} hours"
    return f"{hours:.1f} hours"


def _build_warning_from_report(report: dict) -> str | None:
    stale_items = report.get("stale_items") or []
    if not stale_items:
        return None

    sprint = report["sprint"]
    active_stale = sum(1 for item in stale_items if item.get("status") == "active")
    pending_stale = sum(1 for item in stale_items if item.get("status") == "pending")

    threshold = report.get("threshold")
    active_hours = (
        threshold.total_seconds() / 3600
        if threshold is not None
        else report.get("threshold_hours")
    )
    pending_threshold = report.get("pending_threshold")
    pending_hours = (
        pending_threshold.total_seconds() / 3600
        if pending_threshold is not None
        else report.get("pending_threshold_hours")
    )

    detail_parts = []
    if active_stale:
        detail_parts.append(
            f"{active_stale} active > {_format_hours_label(active_hours)}"
        )
    if pending_stale:
        detail_parts.append(
            f"{pending_stale} pending > {_format_hours_label(pending_hours)}"
        )

    detail = ", ".join(detail_parts) if detail_parts else f"{len(stale_items)} stale"
    return (
        f"Sprint '{sprint['name']}' has {len(stale_items)} stale item(s) "
        f"({detail})"
    )


def _resolve_preflight_targets(
    sprintctl_conn: sqlite3.Connection,
    sprint_id: int | None,
) -> list[dict]:
    if sprint_id is not None:
        row = sprintctl_conn.execute(
            "SELECT id, name, status FROM sprint WHERE id = ?",
            (sprint_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Sprint #{sprint_id} not found")
        return [dict(row)]

    rows = sprintctl_conn.execute(
        "SELECT id, name, status FROM sprint WHERE status = 'active' ORDER BY id ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def _run_preflight_via_import(
    sprintctl_conn: sqlite3.Connection,
    sprint_id: int | None,
) -> list[str]:
    from sprintctl import maintain as sc_maintain  # type: ignore[import]

    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    for sprint in _resolve_preflight_targets(sprintctl_conn, sprint_id):
        report = sc_maintain.check(sprintctl_conn, sprint["id"], now)
        warning = _build_warning_from_report(report)
        if warning:
            warnings.append(warning)
    return warnings


def _run_preflight_via_cli(
    sprintctl_conn: sqlite3.Connection,
    sprintctl_db_path: str | Path,
    sprint_id: int | None,
) -> list[str]:
    warnings: list[str] = []
    env = os.environ.copy()
    env["SPRINTCTL_DB"] = str(sprintctl_db_path)

    for sprint in _resolve_preflight_targets(sprintctl_conn, sprint_id):
        proc = subprocess.run(
            [
                "sprintctl",
                "maintain",
                "check",
                "--sprint-id",
                str(sprint["id"]),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "preflight command failed")

        payload = json.loads(proc.stdout)
        warning = _build_warning_from_report(payload)
        if warning:
            warnings.append(warning)

    return warnings


def run_preflight(
    sprintctl_conn: sqlite3.Connection,
    *,
    sprint_id: int | None = None,
    sprintctl_db_path: str | Path | None = None,
) -> list[str]:
    """
    Check for stale work items in active sprints.
    Returns a list of warning strings (empty = all clear).
    Tries sprintctl's Python API first; falls back to the CLI JSON contract.
    Extraction always proceeds regardless of warnings.
    """
    try:
        return _run_preflight_via_import(sprintctl_conn, sprint_id)
    except Exception as import_exc:  # noqa: BLE001
        if sprintctl_db_path is None:
            return [f"Preflight check failed: {import_exc}"]

    try:
        return _run_preflight_via_cli(sprintctl_conn, sprintctl_db_path, sprint_id)
    except Exception as cli_exc:  # noqa: BLE001
        return [f"Preflight check failed: {cli_exc}"]
