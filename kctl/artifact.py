"""Read-only published-knowledge artifact export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import db as _db


ARTIFACT_FILENAME = "knowledge-artifact-v1.ndjson"
REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
STREAMS = {"durable", "coordination"}
CATEGORIES = {"decision", "pattern", "lesson", "risk", "reference"}


def validate_repo_id(repo_id: str) -> str:
    """Validate and return a repository scope identifier."""
    if not REPO_ID_RE.fullmatch(repo_id):
        raise ValueError("repo_id must contain only letters, numbers, '.', '_' or '-'")
    return repo_id


def artifact_path(artifacts_root: Path, repo_id: str) -> Path:
    """Return the single v1 snapshot destination for a repository scope."""
    validate_repo_id(repo_id)
    return artifacts_root / repo_id / "knowledge" / ARTIFACT_FILENAME


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string or null")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        raise ValueError(f"{field} must be an RFC 3339 UTC timestamp")
    return value


def _tags(raw: Any) -> list[str]:
    try:
        tags = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("entry tags must be a JSON array of unique non-empty strings") from exc
    if (
        not isinstance(tags, list)
        or any(not isinstance(tag, str) or not tag for tag in tags)
        or len(tags) != len(set(tags))
    ):
        raise ValueError("entry tags must be a JSON array of unique non-empty strings")
    return tags


def content_digest(title: str, body: str) -> str:
    """Return the contract's stable digest for published content."""
    if not isinstance(title, str) or not title:
        raise ValueError("entry title must be a non-empty string")
    if not isinstance(body, str) or not body:
        raise ValueError("entry body must be a non-empty string")
    canonical = json.dumps(
        {"body": body, "title": title},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def build_record(
    *,
    repo_id: str,
    entry: dict[str, Any],
    candidate: dict[str, Any],
    rendered_at: str,
) -> dict[str, Any]:
    """Project one published entry and its source candidate into v1."""
    validate_repo_id(repo_id)
    entry_id = _positive_int(entry.get("id"), "entry id")
    stream = entry.get("source_kind", "durable")
    if stream not in STREAMS:
        raise ValueError(f"entry #{entry_id} has unsupported stream {stream!r}")
    category = entry.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"entry #{entry_id} has unsupported category {category!r}")
    superseded_by = _optional_positive_int(entry.get("superseded_by"), "superseded_by")
    if superseded_by == entry_id:
        raise ValueError(f"entry #{entry_id} cannot supersede itself")

    event_id = _positive_int(candidate.get("source_event_id"), "source event id")
    return {
        "schema_version": "knowledge-artifact/v1",
        "repo_id": repo_id,
        "entry_id": entry_id,
        "stream": stream,
        "status": "published",
        "title": entry["title"],
        "body": entry["body"],
        "content_digest": content_digest(entry["title"], entry["body"]),
        "category": category,
        "tags": _tags(entry.get("tags")),
        "source": {
            "event_id": event_id,
            "event_ref": f"sprintctl:event:{event_id}",
            "sprint_id": _positive_int(candidate.get("source_sprint_id"), "source sprint id"),
            "item_id": _optional_positive_int(candidate.get("source_item_id"), "source item id"),
            "track": _optional_text(candidate.get("source_track"), "source track"),
        },
        "published_at": _timestamp(entry.get("created_at"), "published_at"),
        "rendered_at": _timestamp(rendered_at, "rendered_at"),
        "superseded_by": superseded_by,
    }


def build_snapshot(
    conn,
    *,
    repo_id: str,
    rendered_at: str,
) -> list[dict[str, Any]]:
    """Build the complete published-entry snapshot in stable entry-id order."""
    entries = sorted(_db.list_entries(conn, source_kind=None), key=lambda entry: entry["id"])
    records: list[dict[str, Any]] = []
    for entry in entries:
        candidate = _db.get_candidate(conn, entry["candidate_id"])
        if candidate is None:
            raise ValueError(
                f"entry #{entry['id']} references missing candidate #{entry['candidate_id']}"
            )
        records.append(
            build_record(
                repo_id=repo_id,
                entry=entry,
                candidate=candidate,
                rendered_at=rendered_at,
            )
        )
    return records


def write_snapshot(destination: Path, records: list[dict[str, Any]]) -> None:
    """Durably replace a snapshot without exposing an incomplete destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary_path, destination)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def export_snapshot(
    conn,
    *,
    artifacts_root: Path,
    repo_id: str,
    rendered_at: str,
) -> tuple[Path, int]:
    """Build and atomically write the contract snapshot."""
    destination = artifact_path(artifacts_root, repo_id)
    records = build_snapshot(conn, repo_id=repo_id, rendered_at=rendered_at)
    write_snapshot(destination, records)
    return destination, len(records)
