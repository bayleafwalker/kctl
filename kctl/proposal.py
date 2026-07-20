"""Read-only backlog-proposal export for approved-but-not-yet-published knowledge.

This module never touches sprintctl. It only reads kctl's own SQLite store
and projects "approved" candidates -- knowledge a human reviewer has already
confirmed is worth acting on, but that kctl has not (yet) promoted into its
own knowledge base via `kctl publish` -- into a deterministic NDJSON
snapshot.

Each record additionally carries a suggested owner repository and a
suggested next action string. Both are propositions for a human or a
separately authorized agent to evaluate. Per the ownership matrix in
docs/plans/agentops/state-event-command-matrix.md, this is an "observation
(proposal artifact)": it never mutates authoritative state. kctl does not
create sprintctl items, call any sprintctl mutation command, or treat a
proposal as accepted state -- acceptance only happens when a human or agent
runs an explicit sprintctl command afterward.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import db as _db
from .artifact import UTC_TIMESTAMP_RE, validate_repo_id, write_snapshot

PROPOSAL_FILENAME = "knowledge-proposal-v1.ndjson"
STREAMS = {"durable", "coordination"}


def proposal_path(artifacts_root: Path, repo_id: str) -> Path:
    """Return the single v1 proposal snapshot destination for a repository scope."""
    validate_repo_id(repo_id)
    return artifacts_root / repo_id / "knowledge" / PROPOSAL_FILENAME


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
        raise ValueError(
            "candidate tags must be a JSON array of unique non-empty strings"
        ) from exc
    if (
        not isinstance(tags, list)
        or any(not isinstance(tag, str) or not tag for tag in tags)
        or len(tags) != len(set(tags))
    ):
        raise ValueError("candidate tags must be a JSON array of unique non-empty strings")
    return tags


def proposal_digest(summary: str, detail: str | None) -> str:
    """Return a stable digest over the proposed knowledge content."""
    if not isinstance(summary, str) or not summary:
        raise ValueError("candidate summary must be a non-empty string")
    canonical = json.dumps(
        {"detail": detail, "summary": summary},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def build_record(
    *,
    repo_id: str,
    candidate: dict[str, Any],
    suggested_owner_repo: str,
    rendered_at: str,
) -> dict[str, Any]:
    """Project one approved candidate into a knowledge-proposal/v1 record."""
    validate_repo_id(repo_id)
    validate_repo_id(suggested_owner_repo)
    candidate_id = _positive_int(candidate.get("id"), "candidate id")
    if candidate.get("status") != "approved":
        raise ValueError(
            f"candidate #{candidate_id} is not 'approved'; proposals only cover "
            "approved-but-not-yet-published candidates"
        )
    stream = candidate.get("candidate_kind", "durable")
    if stream not in STREAMS:
        raise ValueError(f"candidate #{candidate_id} has unsupported stream {stream!r}")

    event_id = _positive_int(candidate.get("source_event_id"), "source event id")
    summary = _optional_text(candidate.get("summary"), "summary")
    if summary is None:
        raise ValueError(f"candidate #{candidate_id} has no summary")

    return {
        "schema_version": "knowledge-proposal/v1",
        "repo_id": repo_id,
        "candidate_id": candidate_id,
        "stream": stream,
        "status": "approved",
        "summary": summary,
        "detail": candidate.get("detail"),
        "content_digest": proposal_digest(summary, candidate.get("detail")),
        "tags": _tags(candidate.get("tags")),
        "provenance": {
            "event_id": event_id,
            "event_ref": f"sprintctl:event:{event_id}",
            "sprint_id": _positive_int(candidate.get("source_sprint_id"), "source sprint id"),
            "item_id": _optional_positive_int(candidate.get("source_item_id"), "source item id"),
            "track": _optional_text(candidate.get("source_track"), "source track"),
            "reviewed_at": _timestamp(candidate.get("reviewed_at"), "reviewed_at"),
            "reviewed_by": _optional_text(candidate.get("reviewed_by"), "reviewed_by"),
        },
        "suggested_owner_repo": suggested_owner_repo,
        "suggested_next_action": f"propose sprintctl item add in repo {suggested_owner_repo}",
        "extracted_at": _timestamp(candidate.get("extracted_at"), "extracted_at"),
        "rendered_at": _timestamp(rendered_at, "rendered_at"),
    }


def build_snapshot(
    conn,
    *,
    repo_id: str,
    suggested_owner_repo: str,
    rendered_at: str,
) -> list[dict[str, Any]]:
    """Build the complete approved-candidate proposal snapshot in stable candidate-id order."""
    candidates = sorted(
        _db.list_candidates(conn, status="approved", candidate_kind=None),
        key=lambda candidate: candidate["id"],
    )
    return [
        build_record(
            repo_id=repo_id,
            candidate=candidate,
            suggested_owner_repo=suggested_owner_repo,
            rendered_at=rendered_at,
        )
        for candidate in candidates
    ]


def export_snapshot(
    conn,
    *,
    artifacts_root: Path,
    repo_id: str,
    suggested_owner_repo: str,
    rendered_at: str,
) -> tuple[Path, int]:
    """Build and atomically write the read-only proposal snapshot.

    This never calls sprintctl and never marks a candidate as acted upon; it
    only reads kctl's SQLite store. A proposal is not accepted state -- a
    human or separately authorized agent must run an explicit sprintctl
    command to act on it.
    """
    destination = proposal_path(artifacts_root, repo_id)
    records = build_snapshot(
        conn,
        repo_id=repo_id,
        suggested_owner_repo=suggested_owner_repo,
        rendered_at=rendered_at,
    )
    write_snapshot(destination, records)
    return destination, len(records)
