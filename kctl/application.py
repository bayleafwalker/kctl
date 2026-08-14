"""Compatibility surface composed from focused central-knowledge services."""

from __future__ import annotations

from .application_core import *
from .application_candidates import CandidateService
from .application_publications import PublicationService


class CentralKnowledgeApplication(
    PublicationService, CandidateService, KnowledgeApplicationCore,
):
    """Stable application boundary over focused candidate/publication services."""

    pass


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
