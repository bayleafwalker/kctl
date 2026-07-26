"""Read-only sprintctl event sources for kctl extraction."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db as _db


class SprintctlSourceError(ValueError):
    """The configured sprintctl source cannot be used for extraction."""


@dataclass(frozen=True)
class ServedProfile:
    """The non-secret fields kctl needs from a Vuoro client profile."""

    name: str
    endpoint: str
    credential_ref: str
    expected_environment: str


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


@dataclass
class ServedSprintctlSource:
    """Read sprintctl events through the served Vuoro operation catalog."""

    profile: ServedProfile
    repo_id: str

    _PAGE_SIZE = 100

    @property
    def source_id(self) -> str:
        # Keep extraction state independent of endpoint and credential changes.
        return f"served://{self.repo_id}"

    def _invoke(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            from vuoro_client import AsyncVuoroClient, Profile  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SprintctlSourceError(
                "Served extraction requires vuoro-client. Install kctl with its served extra: kctl[served]."
            ) from exc

        async def invoke() -> dict[str, Any]:
            profile = Profile(
                name=self.profile.name,
                endpoint=self.profile.endpoint,
                credential_ref=self.profile.credential_ref,
                expected_environment=self.profile.expected_environment,
            )
            async with AsyncVuoroClient(profile, _resolve_file_credential) as client:
                return await client.invoke(operation, arguments, repo_id=self.repo_id)

        try:
            result = asyncio.run(invoke())
        except SprintctlSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SprintctlSourceError(
                f"could not invoke served sprintctl source operation {operation}: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise SprintctlSourceError(
                f"served sprintctl operation {operation} returned a non-object result"
            )
        if result.get("repo_id") != self.repo_id:
            raise SprintctlSourceError(
                f"served sprintctl operation {operation} returned data for an unexpected repository"
            )
        return result

    def fetch_events(
        self,
        *,
        since_event_id: int,
        event_types: set[str],
        sprint_id: int | None,
    ) -> list[dict[str, Any]]:
        if sprint_id is None:
            raise SprintctlSourceError(
                "Served extraction requires --sprint-id because work.read.events is sprint-scoped."
            )

        # ``after_offset`` is an ordinal cursor, not an event ID. Re-read the
        # sprint log and filter on kctl's durable event-ID watermark so a
        # restart cannot skip an event due to different cursor domains.
        offset = 0
        selected: list[dict[str, Any]] = []
        while True:
            result = self._invoke(
                "work.read.events",
                {
                    "sprint_id": sprint_id,
                    "work_item_id": None,
                    "after_offset": offset,
                    "limit": self._PAGE_SIZE,
                },
            )
            events = result.get("events")
            if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
                raise SprintctlSourceError(
                    "served sprintctl work.read.events returned an invalid events list"
                )
            for event in events:
                event_id = event.get("id")
                if not isinstance(event_id, int):
                    raise SprintctlSourceError(
                        "served sprintctl work.read.events returned an event without an integer id"
                    )
                if event_id > since_event_id and event.get("event_type") in event_types:
                    selected.append(_normalise_remote_event(event))
            if len(events) < self._PAGE_SIZE:
                return selected
            offset += len(events)

    def list_preflight_targets(self, sprint_id: int | None) -> list[dict[str, Any]]:
        # Retained for generic source inspection. Served preflight itself uses
        # the owning ``work.maintain.check`` operation below rather than
        # composing a diagnostic from these sprint rows.
        result = self._invoke(
            "work.read.sprints",
            {
                "include_backlog": True,
                "include_archive": True,
                "active_only": sprint_id is None,
            },
        )
        sprints = result.get("sprints")
        if not isinstance(sprints, list) or not all(isinstance(sprint, dict) for sprint in sprints):
            raise SprintctlSourceError(
                "served sprintctl work.read.sprints returned an invalid sprints list"
            )
        if sprint_id is not None:
            sprints = [sprint for sprint in sprints if sprint.get("id") == sprint_id]
        return [
            {"id": sprint["id"], "name": sprint.get("name"), "status": sprint.get("status")}
            for sprint in sprints
            if isinstance(sprint.get("id"), int)
        ]

    def maintain_check(self, sprint_id: int | None) -> dict[str, Any]:
        """Read Sprintctl's owning health diagnostic through Vuoro."""
        return self._invoke("work.maintain.check", {"sprint_id": sprint_id})

    def close(self) -> None:
        # Every invocation owns and closes its short-lived async client.
        return None


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


def _load_served_profile(path: Path) -> ServedProfile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SprintctlSourceError(f"could not read SPRINTCTL_VUORO_PROFILE: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SprintctlSourceError(f"invalid JSON in SPRINTCTL_VUORO_PROFILE: {exc}") from exc
    if not isinstance(raw, dict):
        raise SprintctlSourceError("SPRINTCTL_VUORO_PROFILE must contain a JSON object.")
    if raw.get("schema_version") != "vuoro-client-profile/v1":
        raise SprintctlSourceError(
            "SPRINTCTL_VUORO_PROFILE must use schema_version 'vuoro-client-profile/v1'."
        )
    name = raw.get("id")
    target = raw.get("target")
    credential_ref = raw.get("credential_ref")
    if not isinstance(name, str) or not name:
        raise SprintctlSourceError("SPRINTCTL_VUORO_PROFILE is missing a non-empty 'id'.")
    if not isinstance(target, dict):
        raise SprintctlSourceError("SPRINTCTL_VUORO_PROFILE is missing a 'target' object.")
    endpoint = target.get("endpoint")
    environment = target.get("environment_id")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise SprintctlSourceError("SPRINTCTL_VUORO_PROFILE target.endpoint must be an https:// URL.")
    if not isinstance(environment, str) or not environment:
        raise SprintctlSourceError(
            "SPRINTCTL_VUORO_PROFILE is missing a non-empty target.environment_id."
        )
    if not isinstance(credential_ref, str) or not credential_ref.startswith("file:"):
        raise SprintctlSourceError("SPRINTCTL_VUORO_PROFILE credential_ref must use 'file:'.")
    if target.get("environment_class") == "production" and raw.get("production_endpoint_denied") is True:
        raise SprintctlSourceError(
            "SPRINTCTL_VUORO_PROFILE contradicts its production endpoint policy."
        )
    return ServedProfile(name, endpoint, credential_ref, environment)


def _resolve_file_credential(ref: str) -> str:
    if not ref.startswith("file:"):
        raise SprintctlSourceError("served credential references must use 'file:'.")
    raw_path = ref[len("file:"):]
    if raw_path.startswith("~/"):
        path = Path.home() / raw_path[2:]
    elif raw_path.startswith("/"):
        path = Path(raw_path)
    else:
        raise SprintctlSourceError("served credential file references must be absolute or start with '~/'.")
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise SprintctlSourceError("could not stat the served credential file.") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o600
    ):
        raise SprintctlSourceError(
            "the served credential file must be a current-user-owned regular mode-0600 file."
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise SprintctlSourceError("could not read the served credential file.") from exc
    if value.endswith(b"\n"):
        value = value[:-1]
    if not value:
        raise SprintctlSourceError("the served credential file is empty.")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SprintctlSourceError("the served credential file is not valid UTF-8.") from exc


def open_sprintctl_source(
    *,
    sprintctl_db: str | None = None,
    remote_repo_id: str | None = None,
) -> LocalSprintctlSource | RemoteSprintctlSource | ServedSprintctlSource:
    # An explicit database path is an unambiguous local-source request. This
    # also lets operators inspect a frozen local snapshot from a remote-mode
    # shell without changing its environment.
    mode = "local" if sprintctl_db is not None else os.environ.get("SPRINTCTL_BACKEND", "local")
    if mode not in {"local", "remote", "served"}:
        raise SprintctlSourceError(
            f"Invalid SPRINTCTL_BACKEND={mode!r}; expected 'local', 'remote', or 'served'."
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

    if mode == "remote":
        url = os.environ.get("SPRINTCTL_URL")
        if not url:
            raise SprintctlSourceError("SPRINTCTL_BACKEND=remote requires SPRINTCTL_URL.")
        return RemoteSprintctlSource(
            conn=_connect_remote(url),
            repo_id=_resolve_remote_repo_id(remote_repo_id),
        )

    if os.environ.get("SPRINTCTL_URL"):
        raise SprintctlSourceError(
            "SPRINTCTL_BACKEND=served cannot be combined with SPRINTCTL_URL."
        )
    if sys.version_info < (3, 12):
        raise SprintctlSourceError("SPRINTCTL_BACKEND=served requires Python 3.12+.")
    profile_path = os.environ.get("SPRINTCTL_VUORO_PROFILE")
    if not profile_path:
        raise SprintctlSourceError("SPRINTCTL_BACKEND=served requires SPRINTCTL_VUORO_PROFILE.")
    return ServedSprintctlSource(
        profile=_load_served_profile(Path(profile_path).expanduser()),
        repo_id=_resolve_remote_repo_id(remote_repo_id),
    )
