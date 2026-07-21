"""Click-independent application core for the served knowledge workflow.

The local SQLite workflow remains owned by the existing ``db``, ``review``,
and ``publish`` modules.  This boundary operates only on the compatible
central PostgreSQL schema introduced for Vuoro.  It stores reviewable
candidate content, but publication records contain Git references and digests
only; document bodies remain canonical in Git.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import PurePosixPath
import re
from typing import Any, Callable, Iterator, Mapping
from uuid import UUID, uuid5

from . import proposal
from .central_schema import (
    CURRENT_SCHEMA_VERSION,
    DOMAIN_API_VERSION,
    _connect,
    _env_dsn,
    _identifier,
    check_compatibility,
    require_runtime_compatibility,
)
from .transfer import TRANSFER_NAMESPACE


MAX_READ_LIMIT = 100
GIT_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CANDIDATE_STATUSES = {"candidate", "approved", "rejected", "published"}
CANDIDATE_KINDS = {"durable", "coordination"}
CATEGORIES = {"decision", "pattern", "lesson", "risk", "reference"}

ConnectionFactory = Callable[[], Any]


class KnowledgeApplicationError(RuntimeError):
    """Base class for stable served-domain rejections."""

    code = "knowledge-rejected"
    http_status = 409


class KnowledgeInputError(KnowledgeApplicationError):
    code = "knowledge-input-invalid"
    http_status = 422


class KnowledgeNotFoundError(KnowledgeApplicationError):
    code = "knowledge-not-found"
    http_status = 404


class KnowledgeConflictError(KnowledgeApplicationError):
    code = "knowledge-evidence-conflict"
    http_status = 409


class KnowledgeTransitionError(KnowledgeApplicationError):
    code = "knowledge-transition-rejected"
    http_status = 409


class StaleBasisError(KnowledgeApplicationError):
    code = "knowledge-stale-basis"
    http_status = 409


@dataclass(frozen=True)
class MutationEvidence:
    """Transport evidence used to authorize one served mutation."""

    actor: str
    environment: str
    request_id: str
    catalog_revision: str
    idempotency_key: str
    basis_revision: str | None


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if not isinstance(row, Mapping):
        raise RuntimeError("central knowledge connections must return mapping rows")
    return _json_value(dict(row))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise KnowledgeInputError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise KnowledgeInputError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _git_revision(value: Any, field: str = "basis_revision") -> str:
    revision = _text(value, field)
    if not GIT_REVISION_RE.fullmatch(revision):
        raise KnowledgeInputError(f"{field} must be a full 40- or 64-hex Git revision")
    return revision


def _digest(value: Any, field: str = "content_digest") -> str:
    digest = _text(value, field)
    if not DIGEST_RE.fullmatch(digest):
        raise KnowledgeInputError(f"{field} must be a sha256 digest")
    return digest


def _uuid(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        parsed = UUID(raw)
    except ValueError as error:
        raise KnowledgeInputError(f"{field} must be a UUID") from error
    canonical = str(parsed)
    if not UUID_RE.fullmatch(canonical):
        raise KnowledgeInputError(f"{field} must be a canonical UUID")
    return canonical


def _timestamp(value: Any, field: str) -> str:
    raw = _text(value, field)
    if not raw.endswith("Z"):
        raise KnowledgeInputError(f"{field} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise KnowledgeInputError(
            f"{field} must be an RFC 3339 UTC timestamp"
        ) from error
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise KnowledgeInputError(f"{field} must be an RFC 3339 UTC timestamp")
    return parsed.isoformat().replace("+00:00", "Z")


def _tags(value: Any, field: str = "tags") -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(tag, str) or not tag for tag in value)
        or len(value) != len(set(value))
    ):
        raise KnowledgeInputError(
            f"{field} must be an array of unique non-empty strings"
        )
    return list(value)


def _content_path(value: Any) -> str:
    raw = _text(value, "content_path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw or "\x00" in raw:
        raise KnowledgeInputError(
            "content_path must be a repository-relative POSIX path without '..'"
        )
    return raw


def _repo_id(value: Any) -> str:
    repo = _text(value, "repo_id")
    if not REPO_ID_RE.fullmatch(repo):
        raise KnowledgeInputError("repo_id contains unsupported characters")
    return repo


def _stable_id(kind: str, repo_id: str, local_id: int) -> str:
    return str(uuid5(TRANSFER_NAMESPACE, f"{kind}:{repo_id}:{local_id}"))


def _evidence(evidence: MutationEvidence, *, expected_basis: str) -> None:
    _text(evidence.actor, "actor")
    _text(evidence.environment, "environment")
    _text(evidence.request_id, "request_id")
    _text(evidence.catalog_revision, "catalog_revision")
    _text(evidence.idempotency_key, "idempotency_key")
    basis = _git_revision(evidence.basis_revision)
    if basis != expected_basis:
        raise StaleBasisError(
            f"basis revision {basis} does not match required revision {expected_basis}"
        )


def _limit(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_READ_LIMIT
    ):
        raise KnowledgeInputError(f"limit must be between 1 and {MAX_READ_LIMIT}")
    return value


class CentralKnowledgeApplication:
    """Application boundary over one compatible central knowledge schema."""

    def __init__(
        self,
        *,
        schema: str = "knowledge",
        connection_factory: ConnectionFactory | None = None,
        expected_environment_name: str | None = None,
        expected_environment_class: str | None = None,
    ) -> None:
        self.schema = _identifier(schema, "schema")
        self._schema = f'"{self.schema}"'
        self._connection_factory = connection_factory
        self.expected_environment_name = expected_environment_name or os.environ.get(
            "KCTL_CENTRAL_EXPECTED_ENVIRONMENT_NAME"
        )
        self.expected_environment_class = expected_environment_class or os.environ.get(
            "KCTL_CENTRAL_EXPECTED_ENVIRONMENT_CLASS"
        )

    def _open(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        return _connect(_env_dsn("KCTL_CENTRAL_RUNTIME_DSN"))

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self._open() as conn:
            require_runtime_compatibility(
                conn,
                schema=self.schema,
                expected_environment_name=self.expected_environment_name,
                expected_environment_class=self.expected_environment_class,
            )
            conn.rollback()
            yield conn

    def compatibility(self) -> dict[str, Any]:
        with self._open() as conn:
            return check_compatibility(
                conn,
                schema=self.schema,
                expected_role_kind="runtime",
                expected_environment_name=self.expected_environment_name,
                expected_environment_class=self.expected_environment_class,
            ).to_dict()

    def _candidate(
        self, cur: Any, candidate_id: str, *, lock: bool = False
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        cur.execute(
            f"SELECT * FROM {self._schema}.knowledge_candidate "
            f"WHERE candidate_id = %s{suffix}",
            (candidate_id,),
        )
        return _row(cur.fetchone())

    def _review(self, cur: Any, candidate_id: str) -> dict[str, Any] | None:
        cur.execute(
            f"SELECT * FROM {self._schema}.knowledge_review WHERE candidate_id = %s",
            (candidate_id,),
        )
        return _row(cur.fetchone())

    def _publication(
        self, cur: Any, publication_id: str, *, lock: bool = False
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        cur.execute(
            f"SELECT * FROM {self._schema}.knowledge_publication_reference "
            f"WHERE publication_id = %s{suffix}",
            (publication_id,),
        )
        return _row(cur.fetchone())

    def intake_candidate(
        self, candidate: dict[str, Any], *, evidence: MutationEvidence
    ) -> dict[str, Any]:
        """Insert one extracted candidate, or replay its immutable identity."""
        repo_id = _repo_id(candidate.get("repo_id"))
        local_id = _positive_int(
            candidate.get("local_candidate_id"), "local_candidate_id"
        )
        source_event_id = _positive_int(
            candidate.get("source_event_id"), "source_event_id"
        )
        source_sprint_id = _positive_int(
            candidate.get("source_sprint_id"), "source_sprint_id"
        )
        source_item_id = _optional_positive_int(
            candidate.get("source_item_id"), "source_item_id"
        )
        event_type = _text(candidate.get("event_type"), "event_type")
        kind = candidate.get("candidate_kind")
        if kind not in CANDIDATE_KINDS:
            raise KnowledgeInputError("candidate_kind is unsupported")
        summary = _text(candidate.get("summary"), "summary")
        detail = _optional_text(candidate.get("detail"), "detail")
        tags = _tags(candidate.get("tags", []))
        confidence = _optional_text(candidate.get("confidence"), "confidence")
        basis = _git_revision(candidate.get("basis_git_revision"), "basis_git_revision")
        content_digest = _digest(candidate.get("content_digest"))
        if content_digest != proposal.proposal_digest(summary, detail):
            raise KnowledgeInputError(
                "content_digest does not match summary and detail"
            )
        extracted_at = _timestamp(candidate.get("extracted_at"), "extracted_at")
        source_created_at = candidate.get("source_created_at")
        if source_created_at is not None:
            source_created_at = _timestamp(source_created_at, "source_created_at")
        source_payload = candidate.get("source_payload")
        try:
            json.dumps(source_payload)
        except (TypeError, ValueError) as error:
            raise KnowledgeInputError(
                "source_payload must be JSON-compatible"
            ) from error
        _evidence(evidence, expected_basis=basis)
        stable_id = _stable_id("candidate", repo_id, local_id)
        expected = {
            "candidate_id": stable_id,
            "repo_id": repo_id,
            "local_candidate_id": local_id,
            "source_event_id": source_event_id,
            "source_sprint_id": source_sprint_id,
            "source_item_id": source_item_id,
            "source_track": _optional_text(
                candidate.get("source_track"), "source_track"
            ),
            "source_actor": _optional_text(
                candidate.get("source_actor"), "source_actor"
            ),
            "source_type": _optional_text(candidate.get("source_type"), "source_type"),
            "source_created_at": source_created_at,
            "source_payload": source_payload,
            "event_type": event_type,
            "candidate_kind": kind,
            "summary": summary,
            "detail": detail,
            "tags": tags,
            "confidence": confidence,
            "content_digest": content_digest,
            "basis_git_revision": basis,
            "extracted_at": extracted_at,
        }
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"kctl-candidate-intake:{self.schema}:{repo_id}",),
                )
                cur.execute(
                    f"""
                    INSERT INTO {self._schema}.knowledge_candidate (
                        candidate_id, repo_id, local_candidate_id, source_event_id,
                        source_sprint_id, source_item_id, source_track, source_actor,
                        source_type, source_created_at, source_payload, event_type,
                        candidate_kind, summary, detail, tags, confidence, status,
                        content_digest, basis_git_revision, extracted_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s, %s::jsonb, %s, 'candidate', %s, %s, %s
                    ) ON CONFLICT DO NOTHING
                    """,
                    (
                        stable_id,
                        repo_id,
                        local_id,
                        source_event_id,
                        source_sprint_id,
                        source_item_id,
                        expected["source_track"],
                        expected["source_actor"],
                        expected["source_type"],
                        source_created_at,
                        json.dumps(source_payload),
                        event_type,
                        kind,
                        summary,
                        detail,
                        json.dumps(tags),
                        confidence,
                        content_digest,
                        basis,
                        extracted_at,
                    ),
                )
                inserted = bool(cur.rowcount)
                cur.execute(
                    f"SELECT * FROM {self._schema}.knowledge_candidate "
                    "WHERE repo_id = %s AND (local_candidate_id = %s OR source_event_id = %s) "
                    "ORDER BY candidate_id",
                    (repo_id, local_id, source_event_id),
                )
                matches = [_row(value) for value in cur.fetchall()]
                if len(matches) != 1:
                    raise KnowledgeConflictError(
                        "candidate local and source-event identities disagree"
                    )
                actual = matches[0]
                assert actual is not None
                if any(actual.get(field) != value for field, value in expected.items()):
                    raise KnowledgeConflictError(
                        "candidate identity already carries different immutable evidence"
                    )
        return {
            "candidate": actual,
            "evidence_ref": f"knowledge:candidate:{stable_id}",
            "replayed": not inserted,
        }

    def list_candidates(
        self,
        *,
        repo_id: str,
        status: str | None = None,
        candidate_kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        repo_id = _repo_id(repo_id)
        limit = _limit(limit)
        if status is not None and status not in CANDIDATE_STATUSES:
            raise KnowledgeInputError("status is unsupported")
        if candidate_kind is not None and candidate_kind not in CANDIDATE_KINDS:
            raise KnowledgeInputError("candidate_kind is unsupported")
        clauses = ["repo_id = %s"]
        values: list[Any] = [repo_id]
        if status is not None:
            clauses.append("status = %s")
            values.append(status)
        if candidate_kind is not None:
            clauses.append("candidate_kind = %s")
            values.append(candidate_kind)
        values.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {self._schema}.knowledge_candidate WHERE "
                + " AND ".join(clauses)
                + " ORDER BY extracted_at DESC, candidate_id DESC LIMIT %s",
                tuple(values),
            )
            candidates = [_row(value) for value in cur.fetchall()]
        return {"candidates": candidates, "count": len(candidates), "limit": limit}

    def show_candidate(self, *, candidate_id: str) -> dict[str, Any]:
        candidate_id = _uuid(candidate_id, "candidate_id")
        with self.connection() as conn, conn.cursor() as cur:
            candidate = self._candidate(cur, candidate_id)
            review = self._review(cur, candidate_id) if candidate is not None else None
        return {"candidate": candidate, "review": review}

    def _review_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        notes: str | None,
        evidence: MutationEvidence,
    ) -> dict[str, Any]:
        candidate_id = _uuid(candidate_id, "candidate_id")
        notes = _optional_text(notes, "review_notes")
        if decision not in {"approved", "rejected"}:
            raise KnowledgeInputError("decision is unsupported")
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                candidate = self._candidate(cur, candidate_id, lock=True)
                if candidate is None:
                    raise KnowledgeNotFoundError(
                        f"candidate {candidate_id} was not found"
                    )
                _evidence(evidence, expected_basis=candidate["basis_git_revision"])
                existing = self._review(cur, candidate_id)
                if existing is not None:
                    expected = {
                        "candidate_id": candidate_id,
                        "decision": decision,
                        "reviewed_by": evidence.actor,
                        "review_notes": notes,
                        "content_digest": candidate["content_digest"],
                        "basis_git_revision": candidate["basis_git_revision"],
                    }
                    if any(
                        existing.get(field) != value
                        for field, value in expected.items()
                    ):
                        raise KnowledgeConflictError(
                            "candidate already carries different review evidence"
                        )
                    replayed = True
                else:
                    if candidate["status"] != "candidate":
                        raise KnowledgeTransitionError(
                            f"candidate is {candidate['status']}, not candidate"
                        )
                    cur.execute(
                        f"""
                        INSERT INTO {self._schema}.knowledge_review (
                            candidate_id, decision, reviewed_at, reviewed_by,
                            review_notes, content_digest, basis_git_revision
                        ) VALUES (%s, %s, clock_timestamp(), %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            candidate_id,
                            decision,
                            evidence.actor,
                            notes,
                            candidate["content_digest"],
                            candidate["basis_git_revision"],
                        ),
                    )
                    existing = _row(cur.fetchone())
                    cur.execute(
                        f"UPDATE {self._schema}.knowledge_candidate SET status = %s "
                        "WHERE candidate_id = %s AND status = 'candidate' RETURNING *",
                        (decision, candidate_id),
                    )
                    candidate = _row(cur.fetchone())
                    if candidate is None:
                        raise KnowledgeTransitionError(
                            "candidate transition lost its basis"
                        )
                    replayed = False
        return {
            "candidate": candidate,
            "review": existing,
            "evidence_ref": f"knowledge:review:{candidate_id}",
            "replayed": replayed,
        }

    def approve_candidate(
        self,
        *,
        candidate_id: str,
        notes: str | None = None,
        evidence: MutationEvidence,
    ) -> dict[str, Any]:
        return self._review_candidate(
            candidate_id=candidate_id,
            decision="approved",
            notes=notes,
            evidence=evidence,
        )

    def reject_candidate(
        self,
        *,
        candidate_id: str,
        reason: str | None = None,
        evidence: MutationEvidence,
    ) -> dict[str, Any]:
        return self._review_candidate(
            candidate_id=candidate_id,
            decision="rejected",
            notes=reason,
            evidence=evidence,
        )

    def record_publication_reference(
        self, publication: dict[str, Any], *, evidence: MutationEvidence
    ) -> dict[str, Any]:
        """Atomically record one Git-owned publication reference."""
        repo_id = _repo_id(publication.get("repo_id"))
        local_id = _positive_int(publication.get("local_entry_id"), "local_entry_id")
        publication_id = _stable_id("publication", repo_id, local_id)
        candidate_id = _uuid(publication.get("candidate_id"), "candidate_id")
        git_revision = _git_revision(publication.get("git_revision"), "git_revision")
        _evidence(evidence, expected_basis=git_revision)
        category = publication.get("category")
        if category not in CATEGORIES:
            raise KnowledgeInputError("category is unsupported")
        source_kind = publication.get("source_kind")
        if source_kind not in CANDIDATE_KINDS:
            raise KnowledgeInputError("source_kind is unsupported")
        supersedes = publication.get("supersedes_publication_id")
        if supersedes is not None:
            supersedes = _uuid(supersedes, "supersedes_publication_id")
            if supersedes == publication_id:
                raise KnowledgeInputError("a publication cannot supersede itself")
        expected = {
            "publication_id": publication_id,
            "repo_id": repo_id,
            "local_entry_id": local_id,
            "candidate_id": candidate_id,
            "document_id": _text(publication.get("document_id"), "document_id"),
            "content_path": _content_path(publication.get("content_path")),
            "content_anchor": _text(
                publication.get("content_anchor"), "content_anchor"
            ),
            "git_revision": git_revision,
            "content_digest": _digest(publication.get("content_digest")),
            "category": category,
            "source_kind": source_kind,
            "tags": _tags(publication.get("tags", [])),
            "published_at": _timestamp(publication.get("published_at"), "published_at"),
            "inline_supersedes": supersedes,
        }
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"kctl-publication:{self.schema}:{repo_id}",),
                )
                candidate = self._candidate(cur, candidate_id, lock=True)
                if candidate is None or candidate["repo_id"] != repo_id:
                    raise KnowledgeNotFoundError(
                        "publication candidate was not found in repository"
                    )
                if candidate["candidate_kind"] != source_kind:
                    raise KnowledgeConflictError(
                        "publication source_kind differs from candidate"
                    )
                review = self._review(cur, candidate_id)
                if review is None or review["decision"] != "approved":
                    raise KnowledgeTransitionError(
                        "only approved candidates can be published"
                    )
                if supersedes is not None:
                    inline_target = self._publication(cur, supersedes, lock=True)
                    if inline_target is None:
                        raise KnowledgeNotFoundError(
                            "inline supersession publication was not found"
                        )
                    if inline_target["repo_id"] != repo_id:
                        raise KnowledgeConflictError(
                            "inline supersession must remain in one repository"
                        )
                cur.execute(
                    f"""
                    INSERT INTO {self._schema}.knowledge_publication_reference (
                        publication_id, repo_id, local_entry_id, candidate_id,
                        document_id, content_path, content_anchor, git_revision,
                        content_digest, category, source_kind, tags, published_at,
                        inline_supersedes
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        publication_id,
                        repo_id,
                        local_id,
                        candidate_id,
                        expected["document_id"],
                        expected["content_path"],
                        expected["content_anchor"],
                        git_revision,
                        expected["content_digest"],
                        category,
                        source_kind,
                        json.dumps(expected["tags"]),
                        expected["published_at"],
                        supersedes,
                    ),
                )
                inserted = bool(cur.rowcount)
                cur.execute(
                    f"SELECT * FROM {self._schema}.knowledge_publication_reference "
                    "WHERE repo_id = %s AND (local_entry_id = %s OR candidate_id = %s) "
                    "ORDER BY publication_id",
                    (repo_id, local_id, candidate_id),
                )
                matches = [_row(value) for value in cur.fetchall()]
                if len(matches) != 1:
                    raise KnowledgeConflictError(
                        "publication local and candidate identities disagree"
                    )
                actual = matches[0]
                assert actual is not None
                if any(actual.get(field) != value for field, value in expected.items()):
                    raise KnowledgeConflictError(
                        "publication identity already carries different immutable evidence"
                    )
                if inserted and candidate["status"] != "approved":
                    raise KnowledgeTransitionError(
                        f"candidate is {candidate['status']}, not approved"
                    )
                if inserted:
                    if supersedes is not None:
                        self._link_supersession(
                            cur, supersedes, publication_id, repo_id
                        )
                elif supersedes is not None:
                    self._require_supersession_edge(
                        cur, supersedes, publication_id, repo_id
                    )
                cur.execute(
                    f"UPDATE {self._schema}.knowledge_candidate SET status = 'published' "
                    "WHERE candidate_id = %s AND status = 'approved'",
                    (candidate_id,),
                )
                if not inserted and candidate["status"] != "published":
                    raise KnowledgeConflictError(
                        "existing publication disagrees with candidate lifecycle"
                    )
                candidate = self._candidate(cur, candidate_id)
        return {
            "publication": actual,
            "candidate": candidate,
            "evidence_ref": f"knowledge:publication:{publication_id}",
            "replayed": not inserted,
        }

    def _require_supersession_edge(
        self, cur: Any, predecessor_id: str, successor_id: str, repo_id: str
    ) -> None:
        predecessor = self._publication(cur, predecessor_id, lock=True)
        successor = self._publication(cur, successor_id, lock=True)
        if predecessor is None or successor is None:
            raise KnowledgeConflictError(
                "recorded inline supersession target is missing"
            )
        if predecessor["repo_id"] != repo_id or successor["repo_id"] != repo_id:
            raise KnowledgeConflictError(
                "recorded inline supersession crossed a repository boundary"
            )
        if predecessor.get("superseded_by") != successor_id:
            raise KnowledgeConflictError(
                "recorded inline supersession edge no longer matches central state"
            )

    def _link_supersession(
        self, cur: Any, predecessor_id: str, successor_id: str, repo_id: str
    ) -> bool:
        predecessor = self._publication(cur, predecessor_id, lock=True)
        successor = self._publication(cur, successor_id, lock=True)
        if predecessor is None or successor is None:
            raise KnowledgeNotFoundError("supersession publication was not found")
        if predecessor["repo_id"] != repo_id or successor["repo_id"] != repo_id:
            raise KnowledgeConflictError("supersession must remain in one repository")
        current = predecessor.get("superseded_by")
        if current == successor_id:
            return True
        if current is not None:
            raise KnowledgeConflictError(
                "publication already has a different successor"
            )
        cursor = successor
        visited = {predecessor_id}
        while cursor.get("superseded_by") is not None:
            target = cursor["superseded_by"]
            if target in visited:
                raise KnowledgeConflictError(
                    "publication supersession would create a cycle"
                )
            visited.add(target)
            cursor = self._publication(cur, target, lock=True)
            if cursor is None:
                raise KnowledgeConflictError(
                    "publication supersession target disappeared"
                )
        cur.execute(
            f"UPDATE {self._schema}.knowledge_publication_reference SET superseded_by = %s "
            "WHERE publication_id = %s AND superseded_by IS NULL",
            (successor_id, predecessor_id),
        )
        if cur.rowcount != 1:
            raise KnowledgeConflictError(
                "publication supersession changed concurrently"
            )
        return False

    def supersede_publication(
        self,
        *,
        predecessor_id: str,
        successor_id: str,
        evidence: MutationEvidence,
    ) -> dict[str, Any]:
        predecessor_id = _uuid(predecessor_id, "predecessor_id")
        successor_id = _uuid(successor_id, "successor_id")
        if predecessor_id == successor_id:
            raise KnowledgeInputError("a publication cannot supersede itself")
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                successor = self._publication(cur, successor_id)
                if successor is None:
                    raise KnowledgeNotFoundError("successor publication was not found")
                _evidence(evidence, expected_basis=successor["git_revision"])
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"kctl-publication:{self.schema}:{successor['repo_id']}",),
                )
                replayed = self._link_supersession(
                    cur, predecessor_id, successor_id, successor["repo_id"]
                )
                predecessor = self._publication(cur, predecessor_id)
                successor = self._publication(cur, successor_id)
        return {
            "predecessor": predecessor,
            "successor": successor,
            "evidence_ref": f"knowledge:supersession:{predecessor_id}:{successor_id}",
            "replayed": replayed,
        }

    def list_publications(
        self,
        *,
        repo_id: str,
        category: str | None = None,
        source_kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        repo_id = _repo_id(repo_id)
        limit = _limit(limit)
        if category is not None and category not in CATEGORIES:
            raise KnowledgeInputError("category is unsupported")
        if source_kind is not None and source_kind not in CANDIDATE_KINDS:
            raise KnowledgeInputError("source_kind is unsupported")
        clauses = ["repo_id = %s"]
        values: list[Any] = [repo_id]
        if category is not None:
            clauses.append("category = %s")
            values.append(category)
        if source_kind is not None:
            clauses.append("source_kind = %s")
            values.append(source_kind)
        values.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {self._schema}.knowledge_publication_reference WHERE "
                + " AND ".join(clauses)
                + " ORDER BY published_at DESC, publication_id DESC LIMIT %s",
                tuple(values),
            )
            publications = [_row(value) for value in cur.fetchall()]
        return {
            "publications": publications,
            "count": len(publications),
            "limit": limit,
        }

    def show_publication(self, *, publication_id: str) -> dict[str, Any]:
        publication_id = _uuid(publication_id, "publication_id")
        with self.connection() as conn, conn.cursor() as cur:
            publication = self._publication(cur, publication_id)
        return {"publication": publication}


def compatibility_record(
    application: CentralKnowledgeApplication | None = None,
) -> dict[str, Any]:
    compatibility = (application or CentralKnowledgeApplication()).compatibility()
    return {
        "api_version": DOMAIN_API_VERSION,
        "schema_version": str(
            compatibility["installed_schema_version"]
            if compatibility["installed_schema_version"] is not None
            else CURRENT_SCHEMA_VERSION
        ),
        "state": "compatible" if compatibility["compatible"] else "incompatible",
        "reason": None
        if compatibility["compatible"]
        else ", ".join(compatibility["reasons"]),
    }


__all__ = [
    "CentralKnowledgeApplication",
    "KnowledgeApplicationError",
    "KnowledgeConflictError",
    "KnowledgeInputError",
    "KnowledgeNotFoundError",
    "KnowledgeTransitionError",
    "MAX_READ_LIMIT",
    "MutationEvidence",
    "StaleBasisError",
    "compatibility_record",
]
