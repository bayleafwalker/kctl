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




class KnowledgeApplicationCore:
    """Connection and shared record lookup services."""
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



