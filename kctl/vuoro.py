"""Vuoro knowledge-domain adapter and data-only operation catalog.

The module imports Vuoro protocol classes only during explicit composition.
Standalone kctl commands therefore retain their existing Click/SQLite-only
dependency boundary.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, NoReturn, Protocol

from .application import (
    CentralKnowledgeApplication,
    KnowledgeApplicationError,
    MAX_READ_LIMIT,
    MutationEvidence,
    compatibility_record,
)
from .central_schema import DOMAIN_API_VERSION, SchemaCompatibilityError


SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_FEATURES = ["json-schema-draft-2020-12"]
DOMAIN_NAME = "knowledge"
READ_AUTHORITY = "knowledge.read"
INTAKE_AUTHORITY = "knowledge.candidate.intake"
REVIEW_AUTHORITY = "knowledge.review"
PUBLISH_AUTHORITY = "knowledge.publication-reference.write"

DefinitionFactory = Callable[..., Any]
RejectionFactory = Callable[[str, str, int], BaseException]


class CatalogRegistry(Protocol):
    def register(self, definition: Any, handler: Callable[..., Any]) -> None: ...


def _object(
    properties: dict[str, Any], *, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_NULLABLE_TEXT = {"type": ["string", "null"]}
_NULLABLE_POSITIVE_INTEGER = {
    "type": ["integer", "null"],
    "minimum": 1,
}
_UUID = {
    "type": "string",
    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
}
_NULLABLE_UUID = {"oneOf": [_UUID, {"type": "null"}]}
_GIT_REVISION = {"type": "string", "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"}
_DIGEST = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_TIMESTAMP = {"type": "string", "minLength": 20}
_TAGS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "uniqueItems": True,
}
_STATUS = {"enum": ["candidate", "approved", "rejected", "published"]}
_KIND = {"enum": ["durable", "coordination"]}
_CATEGORY = {"enum": ["decision", "pattern", "lesson", "risk", "reference"]}
_REPO_ID = {"type": "string", "pattern": "^[A-Za-z0-9._-]+$"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_READ_LIMIT}

_CANDIDATE_PROPERTIES = {
    "candidate_id": _UUID,
    "repo_id": _REPO_ID,
    "local_candidate_id": {"type": "integer", "minimum": 1},
    "source_event_id": {"type": "integer", "minimum": 1},
    "source_sprint_id": {"type": "integer", "minimum": 1},
    "source_item_id": _NULLABLE_POSITIVE_INTEGER,
    "source_track": _NULLABLE_TEXT,
    "source_actor": _NULLABLE_TEXT,
    "source_type": _NULLABLE_TEXT,
    "source_created_at": _NULLABLE_TEXT,
    "source_payload": {},
    "event_type": {"type": "string", "minLength": 1},
    "candidate_kind": _KIND,
    "summary": {"type": "string", "minLength": 1},
    "detail": _NULLABLE_TEXT,
    "tags": _TAGS,
    "confidence": _NULLABLE_TEXT,
    "status": _STATUS,
    "content_digest": _DIGEST,
    "basis_git_revision": _GIT_REVISION,
    "extracted_at": _TIMESTAMP,
    "imported_at": _TIMESTAMP,
}
_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": _CANDIDATE_PROPERTIES,
    "required": list(_CANDIDATE_PROPERTIES),
    "additionalProperties": False,
}
_CANDIDATE_INTAKE_PROPERTIES = {
    key: value
    for key, value in _CANDIDATE_PROPERTIES.items()
    if key not in {"candidate_id", "status", "imported_at"}
}
_CANDIDATE_INTAKE_SCHEMA = {
    "type": "object",
    "properties": _CANDIDATE_INTAKE_PROPERTIES,
    "required": list(_CANDIDATE_INTAKE_PROPERTIES),
    "additionalProperties": False,
}

_REVIEW_PROPERTIES = {
    "review_id": {"type": "integer", "minimum": 1},
    "candidate_id": _UUID,
    "decision": {"enum": ["approved", "rejected"]},
    "reviewed_at": _TIMESTAMP,
    "reviewed_by": {"type": "string", "minLength": 1},
    "review_notes": _NULLABLE_TEXT,
    "content_digest": _DIGEST,
    "basis_git_revision": _GIT_REVISION,
    "imported_at": _TIMESTAMP,
}
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": _REVIEW_PROPERTIES,
    "required": list(_REVIEW_PROPERTIES),
    "additionalProperties": False,
}

_PUBLICATION_PROPERTIES = {
    "publication_id": _UUID,
    "repo_id": _REPO_ID,
    "local_entry_id": {"type": "integer", "minimum": 1},
    "candidate_id": _UUID,
    "document_id": {"type": "string", "minLength": 1},
    "content_path": {"type": "string", "minLength": 1},
    "content_anchor": {"type": "string", "minLength": 1},
    "git_revision": _GIT_REVISION,
    "content_digest": _DIGEST,
    "category": _CATEGORY,
    "source_kind": _KIND,
    "tags": _TAGS,
    "published_at": _TIMESTAMP,
    "superseded_by": _NULLABLE_UUID,
    "inline_supersedes": _NULLABLE_UUID,
    "imported_at": _TIMESTAMP,
}
_PUBLICATION_SCHEMA = {
    "type": "object",
    "properties": _PUBLICATION_PROPERTIES,
    "required": list(_PUBLICATION_PROPERTIES),
    "additionalProperties": False,
}
_PUBLICATION_INPUT_PROPERTIES = {
    key: value
    for key, value in _PUBLICATION_PROPERTIES.items()
    if key
    not in {
        "publication_id",
        "superseded_by",
        "inline_supersedes",
        "imported_at",
    }
}
_PUBLICATION_INPUT_PROPERTIES["supersedes_publication_id"] = _NULLABLE_UUID
_PUBLICATION_INPUT_SCHEMA = {
    "type": "object",
    "properties": _PUBLICATION_INPUT_PROPERTIES,
    "required": [
        key
        for key in _PUBLICATION_INPUT_PROPERTIES
        if key != "supersedes_publication_id"
    ],
    "additionalProperties": False,
}


def _mutation_result(
    properties: dict[str, Any], required: tuple[str, ...]
) -> dict[str, Any]:
    return _object(
        {
            **properties,
            "evidence_ref": {"type": "string", "pattern": "^knowledge:"},
            "replayed": {"type": "boolean"},
        },
        required=(*required, "evidence_ref", "replayed"),
    )


def _operation(
    *,
    name: str,
    input_schema: dict[str, Any],
    result_schema: dict[str, Any],
    authority: str,
    semantics: str,
    idempotency: str,
    handler_name: str,
    repo_scoped: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "owning_domain": DOMAIN_NAME,
        "input_schema": input_schema,
        "result_schema": result_schema,
        "required_authority": authority,
        "execution_semantics": semantics,
        "idempotency": idempotency,
        "repo_scoped": repo_scoped,
        "deprecation": {
            "deprecated": False,
            "replacement": None,
            "sunset_at": None,
        },
        "required_client_schema_features": SCHEMA_FEATURES,
        "_handler_name": handler_name,
    }


_OPERATIONS = (
    _operation(
        name="knowledge.candidate.intake",
        input_schema=_object(
            {"candidate": _CANDIDATE_INTAKE_SCHEMA}, required=("candidate",)
        ),
        result_schema=_mutation_result(
            {"candidate": _CANDIDATE_SCHEMA}, ("candidate",)
        ),
        authority=INTAKE_AUTHORITY,
        semantics="write",
        idempotency="required",
        handler_name="intake_candidate",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.candidate.list",
        input_schema=_object(
            {
                "repo_id": _REPO_ID,
                "status": {"oneOf": [_STATUS, {"type": "null"}]},
                "candidate_kind": {"oneOf": [_KIND, {"type": "null"}]},
                "limit": _LIMIT,
            },
            required=("repo_id",),
        ),
        result_schema=_object(
            {
                "candidates": {
                    "type": "array",
                    "items": _CANDIDATE_SCHEMA,
                    "maxItems": MAX_READ_LIMIT,
                },
                "count": {"type": "integer", "minimum": 0, "maximum": MAX_READ_LIMIT},
                "limit": _LIMIT,
            },
            required=("candidates", "count", "limit"),
        ),
        authority=READ_AUTHORITY,
        semantics="read",
        idempotency="not-allowed",
        handler_name="list_candidates",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.candidate.show",
        input_schema=_object(
            {"candidate_id": _UUID, "repo_id": _REPO_ID},
            required=("candidate_id", "repo_id"),
        ),
        result_schema=_object(
            {
                "candidate": {"oneOf": [_CANDIDATE_SCHEMA, {"type": "null"}]},
                "review": {"oneOf": [_REVIEW_SCHEMA, {"type": "null"}]},
            },
            required=("candidate", "review"),
        ),
        authority=READ_AUTHORITY,
        semantics="read",
        idempotency="not-allowed",
        handler_name="show_candidate",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.candidate.approve",
        input_schema=_object(
            {"candidate_id": _UUID, "repo_id": _REPO_ID, "notes": _NULLABLE_TEXT},
            required=("candidate_id", "repo_id"),
        ),
        result_schema=_mutation_result(
            {"candidate": _CANDIDATE_SCHEMA, "review": _REVIEW_SCHEMA},
            ("candidate", "review"),
        ),
        authority=REVIEW_AUTHORITY,
        semantics="write",
        idempotency="required",
        handler_name="approve_candidate",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.candidate.reject",
        input_schema=_object(
            {"candidate_id": _UUID, "repo_id": _REPO_ID, "reason": _NULLABLE_TEXT},
            required=("candidate_id", "repo_id"),
        ),
        result_schema=_mutation_result(
            {"candidate": _CANDIDATE_SCHEMA, "review": _REVIEW_SCHEMA},
            ("candidate", "review"),
        ),
        authority=REVIEW_AUTHORITY,
        semantics="write",
        idempotency="required",
        handler_name="reject_candidate",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.publication-reference.record",
        input_schema=_object(
            {"publication": _PUBLICATION_INPUT_SCHEMA}, required=("publication",)
        ),
        result_schema=_mutation_result(
            {
                "publication": _PUBLICATION_SCHEMA,
                "candidate": _CANDIDATE_SCHEMA,
            },
            ("publication", "candidate"),
        ),
        authority=PUBLISH_AUTHORITY,
        semantics="write",
        idempotency="required",
        handler_name="record_publication_reference",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.publication-reference.supersede",
        input_schema=_object(
            {"predecessor_id": _UUID, "successor_id": _UUID, "repo_id": _REPO_ID},
            required=("predecessor_id", "successor_id", "repo_id"),
        ),
        result_schema=_mutation_result(
            {
                "predecessor": _PUBLICATION_SCHEMA,
                "successor": _PUBLICATION_SCHEMA,
            },
            ("predecessor", "successor"),
        ),
        authority=PUBLISH_AUTHORITY,
        semantics="write",
        idempotency="required",
        handler_name="supersede_publication",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.publication-reference.list",
        input_schema=_object(
            {
                "repo_id": _REPO_ID,
                "category": {"oneOf": [_CATEGORY, {"type": "null"}]},
                "source_kind": {"oneOf": [_KIND, {"type": "null"}]},
                "limit": _LIMIT,
            },
            required=("repo_id",),
        ),
        result_schema=_object(
            {
                "publications": {
                    "type": "array",
                    "items": _PUBLICATION_SCHEMA,
                    "maxItems": MAX_READ_LIMIT,
                },
                "count": {"type": "integer", "minimum": 0, "maximum": MAX_READ_LIMIT},
                "limit": _LIMIT,
            },
            required=("publications", "count", "limit"),
        ),
        authority=READ_AUTHORITY,
        semantics="read",
        idempotency="not-allowed",
        handler_name="list_publications",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.publication-reference.show",
        input_schema=_object(
            {"publication_id": _UUID, "repo_id": _REPO_ID},
            required=("publication_id", "repo_id"),
        ),
        result_schema=_object(
            {"publication": {"oneOf": [_PUBLICATION_SCHEMA, {"type": "null"}]}},
            required=("publication",),
        ),
        authority=READ_AUTHORITY,
        semantics="read",
        idempotency="not-allowed",
        handler_name="show_publication",
        repo_scoped=True,
    ),
    _operation(
        name="knowledge.schema.compatibility",
        input_schema=_object({}),
        result_schema=_object(
            {
                "schema": {"type": "string", "minLength": 1},
                "domain_api_version": {"const": DOMAIN_API_VERSION},
                "installed_schema_version": {"type": ["integer", "null"]},
                "minimum_schema_version": {"type": "integer", "minimum": 1},
                "maximum_schema_version": {"type": "integer", "minimum": 1},
                "current_role": {"type": "string", "minLength": 1},
                "expected_role_kind": {"const": "runtime"},
                "configured_role": _NULLABLE_TEXT,
                "environment_name": _NULLABLE_TEXT,
                "environment_class": _NULLABLE_TEXT,
                "compatible": {"type": "boolean"},
                "reasons": {"type": "array", "items": {"type": "string"}},
            },
            required=(
                "schema",
                "domain_api_version",
                "installed_schema_version",
                "minimum_schema_version",
                "maximum_schema_version",
                "current_role",
                "expected_role_kind",
                "configured_role",
                "environment_name",
                "environment_class",
                "compatible",
                "reasons",
            ),
        ),
        authority=READ_AUTHORITY,
        semantics="read",
        idempotency="not-allowed",
        handler_name="schema_compatibility",
    ),
)


def catalog_operation_specs() -> tuple[dict[str, Any], ...]:
    """Return fresh data-only catalog definitions without importing Vuoro."""
    return tuple(deepcopy(operation) for operation in _OPERATIONS)


def _load_definition(**values: Any) -> Any:
    try:
        from vuoro_service.contracts import OperationDefinition
    except ImportError as error:  # pragma: no cover - composed runtime only
        raise RuntimeError(
            "registering knowledge operations requires vuoro-service"
        ) from error
    return OperationDefinition(**values)


def _load_rejection(code: str, message: str, http_status: int) -> BaseException:
    try:
        from vuoro_service.catalog import OperationRejectedError
    except ImportError as error:  # pragma: no cover - composed runtime only
        raise RuntimeError(
            "invoking knowledge operations requires vuoro-service"
        ) from error
    return OperationRejectedError(code, message, http_status=http_status)


@dataclass(frozen=True)
class VuoroKnowledgeAdapter:
    application: CentralKnowledgeApplication
    rejection_factory: RejectionFactory = _load_rejection

    def _reject(self, code: str, message: str, http_status: int) -> NoReturn:
        raise self.rejection_factory(code, message, http_status)

    def _mutation_evidence(self, context: Any) -> MutationEvidence:
        identity = getattr(context, "identity", None)
        return MutationEvidence(
            actor=getattr(identity, "actor", ""),
            environment=getattr(identity, "environment", ""),
            request_id=getattr(context, "request_id", ""),
            catalog_revision=getattr(context, "catalog_revision", ""),
            idempotency_key=getattr(context, "idempotency_key", "") or "",
            basis_revision=getattr(context, "basis_revision", None),
        )

    def _call(self, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return callback()
        except KnowledgeApplicationError as error:
            self._reject(error.code, str(error), error.http_status)
        except SchemaCompatibilityError as error:
            self._reject("knowledge-schema-incompatible", str(error), 503)

    def _require_repo(self, context: Any, repo_id: Any) -> str:
        """Bind a record argument to the already-authorized envelope scope."""
        if not isinstance(repo_id, str) or not repo_id:
            self._reject("knowledge-repo-mismatch", "knowledge record has no repository scope", 403)
        if getattr(context, "repo_id", None) != repo_id:
            self._reject(
                "knowledge-repo-mismatch",
                "knowledge record repository does not match the authorized invocation scope",
                403,
            )
        return repo_id

    def intake_candidate(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        self._require_repo(context, arguments["candidate"].get("repo_id"))
        return self._call(
            lambda: self.application.intake_candidate(
                arguments["candidate"], evidence=self._mutation_evidence(context)
            )
        )

    def list_candidates(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.list_candidates(
                repo_id=repo_id,
                status=arguments.get("status"),
                candidate_kind=arguments.get("candidate_kind"),
                limit=arguments.get("limit", 50),
            )
        )

    def show_candidate(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.show_candidate(
                candidate_id=arguments["candidate_id"], repo_id=repo_id
            )
        )

    def approve_candidate(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.approve_candidate(
                candidate_id=arguments["candidate_id"],
                notes=arguments.get("notes"),
                evidence=self._mutation_evidence(context),
                repo_id=repo_id,
            )
        )

    def reject_candidate(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.reject_candidate(
                candidate_id=arguments["candidate_id"],
                reason=arguments.get("reason"),
                evidence=self._mutation_evidence(context),
                repo_id=repo_id,
            )
        )

    def record_publication_reference(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        self._require_repo(context, arguments["publication"].get("repo_id"))
        return self._call(
            lambda: self.application.record_publication_reference(
                arguments["publication"], evidence=self._mutation_evidence(context)
            )
        )

    def supersede_publication(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.supersede_publication(
                predecessor_id=arguments["predecessor_id"],
                successor_id=arguments["successor_id"],
                evidence=self._mutation_evidence(context),
                repo_id=repo_id,
            )
        )

    def list_publications(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.list_publications(
                repo_id=repo_id,
                category=arguments.get("category"),
                source_kind=arguments.get("source_kind"),
                limit=arguments.get("limit", 50),
            )
        )

    def show_publication(
        self, arguments: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        repo_id = self._require_repo(context, arguments["repo_id"])
        return self._call(
            lambda: self.application.show_publication(
                publication_id=arguments["publication_id"], repo_id=repo_id
            )
        )

    def schema_compatibility(
        self, _arguments: dict[str, Any], _context: Any
    ) -> dict[str, Any]:
        return self.application.compatibility()

    def register(
        self,
        registry: CatalogRegistry,
        *,
        definition_factory: DefinitionFactory = _load_definition,
    ) -> None:
        for raw in catalog_operation_specs():
            handler_name = raw.pop("_handler_name")
            registry.register(
                definition_factory(**raw),
                getattr(self, handler_name),
            )


def register_operations(
    registry: CatalogRegistry,
    *,
    application: CentralKnowledgeApplication | None = None,
    definition_factory: DefinitionFactory = _load_definition,
) -> None:
    VuoroKnowledgeAdapter(application or CentralKnowledgeApplication()).register(
        registry, definition_factory=definition_factory
    )


__all__ = [
    "DOMAIN_API_VERSION",
    "INTAKE_AUTHORITY",
    "PUBLISH_AUTHORITY",
    "READ_AUTHORITY",
    "REVIEW_AUTHORITY",
    "SCHEMA_DIALECT",
    "VuoroKnowledgeAdapter",
    "catalog_operation_specs",
    "compatibility_record",
    "register_operations",
]
