"""Local-to-central knowledge transfer artifact and import contract.

The transfer artifact is a recovery and rollout input, not a new canonical
document store.  Published bodies are present only so their digest can be
verified; the central database persists the Git reference and digest, never
the body or title.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid5

from . import artifact as _artifact
from . import db as _db
from . import proposal as _proposal


SCHEMA_VERSION = "kctl-central-transfer/v1"
GIT_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TRANSFER_NAMESPACE = UUID("573e2b2b-b2d6-43bd-8ec8-af519c74521c")


class TransferArtifactError(ValueError):
    """A local transfer artifact is malformed or internally inconsistent."""


class TransferConflictError(RuntimeError):
    """An idempotent import key already exists with different immutable data."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _artifact_digest(value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "artifact_digest"}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TransferArtifactError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TransferArtifactError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _timestamp(value: Any, field: str) -> str:
    raw = _text(value, field)
    if not _artifact.UTC_TIMESTAMP_RE.fullmatch(raw):
        raise TransferArtifactError(f"{field} must be an RFC 3339 UTC timestamp")
    return raw


def _git_revision(value: Any, field: str = "git_revision") -> str:
    raw = _text(value, field)
    if not GIT_REVISION_RE.fullmatch(raw):
        raise TransferArtifactError(
            f"{field} must be a full 40- or 64-hex Git revision"
        )
    return raw


def _tags(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise TransferArtifactError(f"{field} must be a JSON array") from exc
    if (
        not isinstance(value, list)
        or any(not isinstance(tag, str) or not tag for tag in value)
        or len(value) != len(set(value))
    ):
        raise TransferArtifactError(
            f"{field} must be an array of unique non-empty strings"
        )
    return value


def _json_value(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise TransferArtifactError(f"{field} must contain valid JSON") from exc


def _content_path(value: Any) -> str:
    raw = _text(value, "content_path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw or "\x00" in raw:
        raise TransferArtifactError(
            "content_path must be a repository-relative POSIX path without '..'"
        )
    return raw


def _stable_uuid(kind: str, repo_id: str, local_id: int) -> str:
    return str(uuid5(TRANSFER_NAMESPACE, f"{kind}:{repo_id}:{local_id}"))


def _database_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _row_mapping(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if row is None:
        raise TransferConflictError("central row disappeared during import")
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row, strict=True))


def _candidate_record(
    candidate: dict[str, Any], *, git_revision: str
) -> dict[str, Any]:
    candidate_id = _positive_int(candidate.get("id"), "candidate.id")
    status = candidate.get("status")
    if status not in _db.VALID_CANDIDATE_TRANSITIONS:
        raise TransferArtifactError(f"candidate #{candidate_id} has invalid status")
    candidate_kind = candidate.get("candidate_kind", "durable")
    if candidate_kind not in _artifact.STREAMS:
        raise TransferArtifactError(f"candidate #{candidate_id} has invalid kind")
    summary = _text(candidate.get("summary"), "candidate.summary")
    detail = candidate.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise TransferArtifactError("candidate.detail must be a string or null")
    review = None
    if status != "candidate":
        review = {
            "decision": "rejected" if status == "rejected" else "approved",
            "reviewed_at": _timestamp(
                candidate.get("reviewed_at"), "review.reviewed_at"
            ),
            "reviewed_by": _text(candidate.get("reviewed_by"), "review.reviewed_by"),
            "review_notes": candidate.get("review_notes"),
        }
    return {
        "local_candidate_id": candidate_id,
        "source": {
            "event_id": _positive_int(
                candidate.get("source_event_id"), "source.event_id"
            ),
            "sprint_id": _positive_int(
                candidate.get("source_sprint_id"), "source.sprint_id"
            ),
            "item_id": _optional_positive_int(
                candidate.get("source_item_id"), "source.item_id"
            ),
            "track": _optional_text(candidate.get("source_track"), "source.track"),
            "actor": _optional_text(candidate.get("source_actor"), "source.actor"),
            "type": _optional_text(candidate.get("source_type"), "source.type"),
            "created_at": (
                _timestamp(candidate.get("source_created_at"), "source.created_at")
                if candidate.get("source_created_at") is not None
                else None
            ),
            "payload": _json_value(candidate.get("source_payload"), "source.payload"),
        },
        "event_type": _text(candidate.get("event_type"), "candidate.event_type"),
        "candidate_kind": candidate_kind,
        "summary": summary,
        "detail": detail,
        "tags": _tags(candidate.get("tags") or "[]", "candidate.tags"),
        "confidence": _optional_text(
            candidate.get("confidence"), "candidate.confidence"
        ),
        "status": status,
        "content_digest": _proposal.proposal_digest(summary, detail),
        "basis_git_revision": git_revision,
        "extracted_at": _timestamp(
            candidate.get("extracted_at"), "candidate.extracted_at"
        ),
        "review": review,
    }


def build_artifact(
    conn: sqlite3.Connection,
    *,
    repo_id: str,
    git_revision: str,
    content_path: str,
    exported_at: str,
) -> dict[str, Any]:
    """Build a deterministic, self-validating local-to-central snapshot."""
    _artifact.validate_repo_id(repo_id)
    _git_revision(git_revision)
    _content_path(content_path)
    _timestamp(exported_at, "exported_at")
    candidates = sorted(
        _db.list_candidates(conn, status=None, candidate_kind=None),
        key=lambda value: value["id"],
    )
    candidate_records = [
        _candidate_record(candidate, git_revision=git_revision)
        for candidate in candidates
    ]
    by_id = {candidate["id"]: candidate for candidate in candidates}
    publications: list[dict[str, Any]] = []
    for entry in sorted(
        _db.list_entries(conn, source_kind=None), key=lambda value: value["id"]
    ):
        entry_id = _positive_int(entry.get("id"), "publication.local_entry_id")
        candidate = by_id.get(entry.get("candidate_id"))
        if candidate is None:
            raise TransferArtifactError(
                f"entry #{entry_id} references missing candidate #{entry.get('candidate_id')}"
            )
        title = _text(entry.get("title"), "publication.title")
        body = _text(entry.get("body"), "publication.body")
        publications.append(
            {
                "local_entry_id": entry_id,
                "local_candidate_id": _positive_int(
                    entry.get("candidate_id"), "publication.local_candidate_id"
                ),
                "document_id": f"{repo_id}:knowledge-base",
                "content_path": content_path,
                "content_anchor": f"entry-{entry_id}",
                "git_revision": git_revision,
                "title": title,
                "body": body,
                "content_digest": _artifact.content_digest(title, body),
                "category": entry.get("category"),
                "source_kind": entry.get("source_kind", "durable"),
                "tags": _tags(entry.get("tags") or "[]", "publication.tags"),
                "published_at": _timestamp(
                    entry.get("created_at"), "publication.published_at"
                ),
                "superseded_by_local_entry_id": _optional_positive_int(
                    entry.get("superseded_by"),
                    "publication.superseded_by_local_entry_id",
                ),
            }
        )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo_id": repo_id,
        "git_revision": git_revision,
        "exported_at": exported_at,
        "candidates": candidate_records,
        "publications": publications,
    }
    result["artifact_digest"] = _artifact_digest(result)
    validate_artifact(result)
    return result


def write_artifact(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace one mode-0600 transfer artifact after validation."""
    validate_artifact(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(temporary_path, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_artifact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransferArtifactError(f"could not read transfer artifact: {exc}") from exc
    if not isinstance(value, dict):
        raise TransferArtifactError("transfer artifact must be a JSON object")
    validate_artifact(value)
    return value


def validate_artifact(value: dict[str, Any]) -> None:
    """Reject malformed content, references, cycles, or digest drift."""
    if value.get("schema_version") != SCHEMA_VERSION:
        raise TransferArtifactError(f"schema_version must be {SCHEMA_VERSION}")
    repo_id = _text(value.get("repo_id"), "repo_id")
    try:
        _artifact.validate_repo_id(repo_id)
    except ValueError as exc:
        raise TransferArtifactError(str(exc)) from exc
    git_revision = _git_revision(value.get("git_revision"))
    _timestamp(value.get("exported_at"), "exported_at")
    digest = _text(value.get("artifact_digest"), "artifact_digest")
    if not DIGEST_RE.fullmatch(digest) or digest != _artifact_digest(value):
        raise TransferArtifactError(
            "artifact_digest does not match the artifact content"
        )

    candidates = value.get("candidates")
    publications = value.get("publications")
    if not isinstance(candidates, list) or not isinstance(publications, list):
        raise TransferArtifactError("candidates and publications must be arrays")
    candidate_ids: set[int] = set()
    candidate_statuses: dict[int, str] = {}
    source_events: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise TransferArtifactError("each candidate must be an object")
        candidate_id = _positive_int(
            candidate.get("local_candidate_id"), "candidate.local_candidate_id"
        )
        source = candidate.get("source")
        if not isinstance(source, dict):
            raise TransferArtifactError("candidate.source must be an object")
        source_event = _positive_int(source.get("event_id"), "source.event_id")
        if candidate_id in candidate_ids or source_event in source_events:
            raise TransferArtifactError(
                "candidate and source-event identities must be unique"
            )
        candidate_ids.add(candidate_id)
        source_events.add(source_event)
        _positive_int(source.get("sprint_id"), "source.sprint_id")
        _optional_positive_int(source.get("item_id"), "source.item_id")
        for field in ("track", "actor", "type"):
            _optional_text(source.get(field), f"source.{field}")
        if source.get("created_at") is not None:
            _timestamp(source.get("created_at"), "source.created_at")
        _text(candidate.get("event_type"), "candidate.event_type")
        if candidate.get("candidate_kind") not in _artifact.STREAMS:
            raise TransferArtifactError("candidate.candidate_kind is unsupported")
        status = candidate.get("status")
        if status not in _db.VALID_CANDIDATE_TRANSITIONS:
            raise TransferArtifactError("candidate.status is unsupported")
        candidate_statuses[candidate_id] = status
        summary = _text(candidate.get("summary"), "candidate.summary")
        detail = candidate.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise TransferArtifactError("candidate.detail must be a string or null")
        _tags(candidate.get("tags"), "candidate.tags")
        _optional_text(candidate.get("confidence"), "candidate.confidence")
        candidate_digest = _text(candidate.get("content_digest"), "content_digest")
        if candidate_digest != _proposal.proposal_digest(summary, detail):
            raise TransferArtifactError(
                f"candidate #{candidate_id} content_digest does not match content"
            )
        if (
            _git_revision(candidate.get("basis_git_revision"), "basis_git_revision")
            != git_revision
        ):
            raise TransferArtifactError(
                f"candidate #{candidate_id} basis_git_revision differs from artifact"
            )
        _timestamp(candidate.get("extracted_at"), "candidate.extracted_at")
        review = candidate.get("review")
        if status == "candidate" and review is not None:
            raise TransferArtifactError("unreviewed candidate cannot carry a review")
        if status != "candidate":
            if not isinstance(review, dict):
                raise TransferArtifactError(
                    "reviewed candidate must carry review evidence"
                )
            expected_decision = "rejected" if status == "rejected" else "approved"
            if review.get("decision") != expected_decision:
                raise TransferArtifactError(
                    "review decision disagrees with candidate status"
                )
            _timestamp(review.get("reviewed_at"), "review.reviewed_at")
            _text(review.get("reviewed_by"), "review.reviewed_by")
            _optional_text(review.get("review_notes"), "review.review_notes")

    publication_ids: set[int] = set()
    publication_candidate_ids: set[int] = set()
    supersession: dict[int, int | None] = {}
    for publication in publications:
        if not isinstance(publication, dict):
            raise TransferArtifactError("each publication must be an object")
        entry_id = _positive_int(
            publication.get("local_entry_id"), "publication.local_entry_id"
        )
        if entry_id in publication_ids:
            raise TransferArtifactError("publication identities must be unique")
        publication_ids.add(entry_id)
        candidate_id = _positive_int(
            publication.get("local_candidate_id"), "publication.local_candidate_id"
        )
        if candidate_id not in candidate_ids:
            raise TransferArtifactError(
                f"publication #{entry_id} references a missing candidate"
            )
        if candidate_id in publication_candidate_ids:
            raise TransferArtifactError("a candidate may have only one publication")
        publication_candidate_ids.add(candidate_id)
        if candidate_statuses[candidate_id] != "published":
            raise TransferArtifactError(
                f"publication #{entry_id} candidate is not published"
            )
        _text(publication.get("document_id"), "publication.document_id")
        _content_path(publication.get("content_path"))
        _text(publication.get("content_anchor"), "publication.content_anchor")
        if _git_revision(publication.get("git_revision")) != git_revision:
            raise TransferArtifactError(
                f"publication #{entry_id} git_revision differs from artifact"
            )
        title = _text(publication.get("title"), "publication.title")
        body = _text(publication.get("body"), "publication.body")
        if publication.get("content_digest") != _artifact.content_digest(title, body):
            raise TransferArtifactError(
                f"publication #{entry_id} content_digest does not match content"
            )
        if publication.get("category") not in _artifact.CATEGORIES:
            raise TransferArtifactError("publication.category is unsupported")
        if publication.get("source_kind") not in _artifact.STREAMS:
            raise TransferArtifactError("publication.source_kind is unsupported")
        _tags(publication.get("tags"), "publication.tags")
        _timestamp(publication.get("published_at"), "publication.published_at")
        target = _optional_positive_int(
            publication.get("superseded_by_local_entry_id"),
            "publication.superseded_by_local_entry_id",
        )
        if target == entry_id:
            raise TransferArtifactError("publication cannot supersede itself")
        supersession[entry_id] = target

    for entry_id, target in supersession.items():
        if target is not None and target not in publication_ids:
            raise TransferArtifactError(
                f"publication #{entry_id} has a missing supersession target"
            )
        visited = {entry_id}
        cursor = target
        while cursor is not None:
            if cursor in visited:
                raise TransferArtifactError(
                    "publication supersession graph contains a cycle"
                )
            visited.add(cursor)
            cursor = supersession[cursor]
    published_candidates = {
        candidate_id
        for candidate_id, status in candidate_statuses.items()
        if status == "published"
    }
    if publication_candidate_ids != published_candidates:
        raise TransferArtifactError(
            "published candidate and publication-reference identities differ"
        )


def import_artifact(conn: Any, *, schema: str, value: dict[str, Any]) -> dict[str, Any]:
    """Import one validated artifact exactly once under a per-repo lock.

    The caller must establish runtime schema compatibility first.  This path
    performs DML only and deliberately has no migration fallback.
    """
    validate_artifact(value)
    repo_id = value["repo_id"]
    schema_ident = f'"{schema}"'
    inserted_candidates = 0
    inserted_reviews = 0
    inserted_publications = 0
    candidate_ids: dict[int, str] = {}
    publication_ids: dict[int, str] = {}

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"kctl-central-import:{schema}:{repo_id}",),
            )
        for candidate in value["candidates"]:
            source = candidate["source"]
            local_id = candidate["local_candidate_id"]
            stable_id = _stable_uuid("candidate", repo_id, local_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {schema_ident}.knowledge_candidate (
                        candidate_id, repo_id, local_candidate_id, source_event_id,
                        source_sprint_id, source_item_id, source_track, source_actor,
                        source_type, source_created_at, source_payload, event_type,
                        candidate_kind, summary, detail, tags, confidence, status,
                        content_digest, basis_git_revision, extracted_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (repo_id, local_candidate_id) DO NOTHING
                    """,
                    (
                        stable_id,
                        repo_id,
                        local_id,
                        source["event_id"],
                        source["sprint_id"],
                        source.get("item_id"),
                        source.get("track"),
                        source.get("actor"),
                        source.get("type"),
                        source.get("created_at"),
                        json.dumps(source.get("payload")),
                        candidate["event_type"],
                        candidate["candidate_kind"],
                        candidate["summary"],
                        candidate.get("detail"),
                        json.dumps(candidate["tags"]),
                        candidate.get("confidence"),
                        candidate["status"],
                        candidate["content_digest"],
                        candidate["basis_git_revision"],
                        candidate["extracted_at"],
                    ),
                )
                inserted_candidates += cur.rowcount
                cur.execute(
                    f"""
                    SELECT candidate_id::text AS candidate_id,
                           repo_id, local_candidate_id, source_event_id,
                           source_sprint_id, source_item_id, source_track,
                           source_actor, source_type, source_created_at,
                           source_payload, event_type, candidate_kind, summary,
                           detail, tags, confidence, status, content_digest,
                           basis_git_revision, extracted_at
                    FROM {schema_ident}.knowledge_candidate
                    WHERE repo_id = %s AND local_candidate_id = %s
                    """,
                    (repo_id, local_id),
                )
                row = cur.fetchone()
            candidate_columns = (
                "candidate_id",
                "repo_id",
                "local_candidate_id",
                "source_event_id",
                "source_sprint_id",
                "source_item_id",
                "source_track",
                "source_actor",
                "source_type",
                "source_created_at",
                "source_payload",
                "event_type",
                "candidate_kind",
                "summary",
                "detail",
                "tags",
                "confidence",
                "status",
                "content_digest",
                "basis_git_revision",
                "extracted_at",
            )
            actual = _row_mapping(row, candidate_columns)
            expected = {
                "candidate_id": stable_id,
                "repo_id": repo_id,
                "local_candidate_id": local_id,
                "source_event_id": source["event_id"],
                "source_sprint_id": source["sprint_id"],
                "source_item_id": source.get("item_id"),
                "source_track": source.get("track"),
                "source_actor": source.get("actor"),
                "source_type": source.get("type"),
                "source_created_at": _database_timestamp(source.get("created_at")),
                "source_payload": source.get("payload"),
                "event_type": candidate["event_type"],
                "candidate_kind": candidate["candidate_kind"],
                "summary": candidate["summary"],
                "detail": candidate.get("detail"),
                "tags": candidate["tags"],
                "confidence": candidate.get("confidence"),
                "status": candidate["status"],
                "content_digest": candidate["content_digest"],
                "basis_git_revision": candidate["basis_git_revision"],
                "extracted_at": _database_timestamp(candidate["extracted_at"]),
            }
            if actual != expected:
                raise TransferConflictError(
                    f"candidate #{local_id} conflicts with an existing central record"
                )
            candidate_ids[local_id] = stable_id
            review = candidate.get("review")
            if review is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {schema_ident}.knowledge_review (
                            candidate_id, decision, reviewed_at, reviewed_by,
                            review_notes, content_digest, basis_git_revision
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (candidate_id) DO NOTHING
                        """,
                        (
                            stable_id,
                            review["decision"],
                            review["reviewed_at"],
                            review["reviewed_by"],
                            review.get("review_notes"),
                            candidate["content_digest"],
                            candidate["basis_git_revision"],
                        ),
                    )
                    inserted_reviews += cur.rowcount
                    cur.execute(
                        f"""
                        SELECT candidate_id::text AS candidate_id, decision,
                               reviewed_at, reviewed_by, review_notes,
                               content_digest, basis_git_revision
                        FROM {schema_ident}.knowledge_review
                        WHERE candidate_id = %s
                        """,
                        (stable_id,),
                    )
                    row = cur.fetchone()
                review_columns = (
                    "candidate_id",
                    "decision",
                    "reviewed_at",
                    "reviewed_by",
                    "review_notes",
                    "content_digest",
                    "basis_git_revision",
                )
                actual_review = _row_mapping(row, review_columns)
                expected_review = {
                    "candidate_id": stable_id,
                    "decision": review["decision"],
                    "reviewed_at": _database_timestamp(review["reviewed_at"]),
                    "reviewed_by": review["reviewed_by"],
                    "review_notes": review.get("review_notes"),
                    "content_digest": candidate["content_digest"],
                    "basis_git_revision": candidate["basis_git_revision"],
                }
                if actual_review != expected_review:
                    raise TransferConflictError(
                        f"candidate #{local_id} review conflicts with central state"
                    )

        for publication in value["publications"]:
            local_id = publication["local_entry_id"]
            stable_id = _stable_uuid("publication", repo_id, local_id)
            candidate_id = candidate_ids[publication["local_candidate_id"]]
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {schema_ident}.knowledge_publication_reference (
                        publication_id, repo_id, local_entry_id, candidate_id,
                        document_id, content_path, content_anchor, git_revision,
                        content_digest, category, source_kind, tags, published_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                    )
                    ON CONFLICT (repo_id, local_entry_id) DO NOTHING
                    """,
                    (
                        stable_id,
                        repo_id,
                        local_id,
                        candidate_id,
                        publication["document_id"],
                        publication["content_path"],
                        publication["content_anchor"],
                        publication["git_revision"],
                        publication["content_digest"],
                        publication["category"],
                        publication["source_kind"],
                        json.dumps(publication["tags"]),
                        publication["published_at"],
                    ),
                )
                inserted_publications += cur.rowcount
                cur.execute(
                    f"""
                    SELECT publication_id::text AS publication_id, repo_id,
                           local_entry_id, candidate_id::text AS candidate_id,
                           document_id, content_path, content_anchor,
                           git_revision, content_digest, category, source_kind,
                           tags, published_at
                    FROM {schema_ident}.knowledge_publication_reference
                    WHERE repo_id = %s AND local_entry_id = %s
                    """,
                    (repo_id, local_id),
                )
                row = cur.fetchone()
            publication_columns = (
                "publication_id",
                "repo_id",
                "local_entry_id",
                "candidate_id",
                "document_id",
                "content_path",
                "content_anchor",
                "git_revision",
                "content_digest",
                "category",
                "source_kind",
                "tags",
                "published_at",
            )
            actual = _row_mapping(row, publication_columns)
            expected = {
                "publication_id": stable_id,
                "repo_id": repo_id,
                "local_entry_id": local_id,
                "candidate_id": candidate_id,
                "document_id": publication["document_id"],
                "content_path": publication["content_path"],
                "content_anchor": publication["content_anchor"],
                "git_revision": publication["git_revision"],
                "content_digest": publication["content_digest"],
                "category": publication["category"],
                "source_kind": publication["source_kind"],
                "tags": publication["tags"],
                "published_at": _database_timestamp(publication["published_at"]),
            }
            if actual != expected:
                raise TransferConflictError(
                    f"publication #{local_id} conflicts with an existing central record"
                )
            publication_ids[local_id] = stable_id

        for publication in value["publications"]:
            local_id = publication["local_entry_id"]
            target_local_id = publication.get("superseded_by_local_entry_id")
            expected_target = (
                publication_ids[target_local_id]
                if target_local_id is not None
                else None
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT superseded_by::text
                    FROM {schema_ident}.knowledge_publication_reference
                    WHERE publication_id = %s
                    """,
                    (publication_ids[local_id],),
                )
                row = cur.fetchone()
                current_target = (
                    next(iter(row.values())) if isinstance(row, dict) else row[0]
                )
                if current_target not in (None, expected_target):
                    raise TransferConflictError(
                        f"publication #{local_id} supersession conflicts with central state"
                    )
                if current_target is None and expected_target is not None:
                    cur.execute(
                        f"""
                        UPDATE {schema_ident}.knowledge_publication_reference
                        SET superseded_by = %s
                        WHERE publication_id = %s AND superseded_by IS NULL
                        """,
                        (expected_target, publication_ids[local_id]),
                    )

    return {
        "schema_version": "kctl-central-import-result/v1",
        "repo_id": repo_id,
        "git_revision": value["git_revision"],
        "artifact_digest": value["artifact_digest"],
        "inserted_candidates": inserted_candidates,
        "inserted_reviews": inserted_reviews,
        "inserted_publications": inserted_publications,
        "candidate_count": len(value["candidates"]),
        "publication_count": len(value["publications"]),
    }
