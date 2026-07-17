"""Read-only sprintctl event sources for kctl extraction."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db as _db


class SprintctlSourceError(ValueError):
    """The configured sprintctl source cannot be used for extraction."""


def _iso_timestamp(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _normalise_remote_event(row: dict[str, Any]) -> dict[str, Any]:
    event = dict(row)
    payload = event.get("payload")
    if not isinstance(payload, str):
        event["payload"] = json.dumps(payload if payload is not None else {}, sort_keys=True)
    event["created_at"] = _iso_timestamp(event.get("created_at"))
    return event


@dataclass
class LocalSprintctlSource:
    path: Path
    conn: Any

    @property
    def source_id(self) -> str:
        return str(self.path)

    def fetch_events(
        self,
        *,
        since_event_id: int,
        event_types: set[str],
        sprint_id: int | None,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(event_types))
        params: list[Any] = [since_event_id, *sorted(event_types)]
        sprint_filter = ""
        if sprint_id is not None:
            sprint_filter = " AND e.sprint_id = ?"
            params.append(sprint_id)
        rows = self.conn.execute(
            f"""
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
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.conn.close()


@dataclass
class RemoteSprintctlSource:
    conn: Any
    repo_id: str

    @property
    def source_id(self) -> str:
        # Do not put SPRINTCTL_URL here: extractor state must not persist credentials.
        return f"remote://{self.repo_id}"

    def fetch_events(
        self,
        *,
        since_event_id: int,
        event_types: set[str],
        sprint_id: int | None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [self.repo_id, since_event_id, sorted(event_types)]
        sprint_filter = ""
        if sprint_id is not None:
            sprint_filter = " AND e.sprint_id = %s"
            params.append(sprint_id)
        query = f"""
            SELECT
                e.id, e.sprint_id, e.work_item_id, e.source_type, e.actor,
                e.event_type, e.payload, e.created_at,
                wi.title AS item_title,
                t.name   AS track_name
            FROM event e
            LEFT JOIN work_item wi
                ON wi.repo_id = e.repo_id AND wi.id = e.work_item_id
            LEFT JOIN track t
                ON t.repo_id = wi.repo_id AND t.id = wi.track_id
            WHERE e.repo_id = %s
              AND e.id > %s
              AND e.event_type = ANY(%s)
              {sprint_filter}
            ORDER BY e.id ASC
        """
        with self.conn.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return [_normalise_remote_event(row) for row in rows]

    def list_preflight_targets(self, sprint_id: int | None) -> list[dict[str, Any]]:
        query = "SELECT id, name, status FROM sprint WHERE repo_id = %s"
        params: list[Any] = [self.repo_id]
        if sprint_id is not None:
            query += " AND id = %s"
            params.append(sprint_id)
        else:
            query += " AND status = 'active'"
        query += " ORDER BY id ASC"
        with self.conn.cursor() as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        self.conn.close()


def _resolve_remote_repo_id(explicit_repo_id: str | None) -> str:
    configured = explicit_repo_id or os.environ.get("KCTL_SPRINTCTL_REPO_ID")
    if configured:
        return configured

    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        marker = directory / ".sprintctl" / "backend.json"
        if marker.exists():
            return directory.name
        if (directory / ".git").exists():
            return directory.name
    raise SprintctlSourceError(
        "Could not resolve remote sprintctl repo ID. Run inside the source repository "
        "or set KCTL_SPRINTCTL_REPO_ID."
    )


def _connect_remote(url: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg.rows import dict_row  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SprintctlSourceError(
            "Remote extraction requires psycopg. Install kctl with its remote extra: kctl[remote]."
        ) from exc

    # The server also enforces this default for every statement in this connection.
    try:
        return psycopg.connect(
            url,
            row_factory=dict_row,
            autocommit=True,
            options="-c default_transaction_read_only=on",
        )
    except Exception as exc:  # noqa: BLE001
        raise SprintctlSourceError(f"could not connect to sprintctl remote source: {exc}") from exc


def open_sprintctl_source(
    *,
    sprintctl_db: str | None = None,
    remote_repo_id: str | None = None,
) -> LocalSprintctlSource | RemoteSprintctlSource:
    # An explicit database path is an unambiguous local-source request. This
    # also lets operators inspect a frozen local snapshot from a remote-mode
    # shell without changing its environment.
    mode = "local" if sprintctl_db is not None else os.environ.get("SPRINTCTL_BACKEND", "local")
    if mode not in {"local", "remote"}:
        raise SprintctlSourceError(
            f"Invalid SPRINTCTL_BACKEND={mode!r}; expected 'local' or 'remote'."
        )

    if mode == "local":
        path = Path(sprintctl_db) if sprintctl_db else Path(
            os.environ.get("SPRINTCTL_DB", Path.home() / ".sprintctl" / "sprintctl.db")
        )
        if not path.exists():
            raise SprintctlSourceError(f"sprintctl DB not found at {path}")
        try:
            conn = _db.get_sprintctl_connection(path)
            _db.validate_sprintctl_schema(conn)
        except Exception as exc:  # noqa: BLE001
            raise SprintctlSourceError(f"could not open sprintctl DB: {exc}") from exc
        return LocalSprintctlSource(path=path, conn=conn)

    url = os.environ.get("SPRINTCTL_URL")
    if not url:
        raise SprintctlSourceError("SPRINTCTL_BACKEND=remote requires SPRINTCTL_URL.")
    return RemoteSprintctlSource(
        conn=_connect_remote(url),
        repo_id=_resolve_remote_repo_id(remote_repo_id),
    )
