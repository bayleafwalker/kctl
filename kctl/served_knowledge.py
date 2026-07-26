"""Authenticated client facade for repository-scoped Vuoro knowledge reads.

This stays separate from the Sprintctl event source because knowledge results
do not carry a redundant ``repo_id`` envelope; the transport scope is the
authorization boundary.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .source import ServedProfile, SprintctlSourceError, _resolve_file_credential
from .proposal import proposal_digest

_GIT_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class ServedKnowledgeClient:
    profile: ServedProfile
    repo_id: str

    def _invoke(self, operation: str, arguments: dict[str, Any], *, basis_revision: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        try:
            from vuoro_client import AsyncVuoroClient, Profile  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SprintctlSourceError(
                "Served knowledge requires vuoro-client. Install kctl with its served extra: kctl[served]."
            ) from exc

        async def invoke() -> dict[str, Any]:
            profile = Profile(
                name=self.profile.name,
                endpoint=self.profile.endpoint,
                credential_ref=self.profile.credential_ref,
                expected_environment=self.profile.expected_environment,
            )
            async with AsyncVuoroClient(profile, _resolve_file_credential) as client:
                kwargs: dict[str, Any] = {"repo_id": self.repo_id}
                if basis_revision is not None:
                    kwargs["basis_revision"] = basis_revision
                if idempotency_key is not None:
                    kwargs["idempotency_key"] = idempotency_key
                return await client.invoke(operation, arguments, **kwargs)

        try:
            result = asyncio.run(invoke())
        except Exception as exc:  # noqa: BLE001
            raise SprintctlSourceError(
                f"could not invoke served knowledge operation {operation}: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise SprintctlSourceError(
                f"served knowledge operation {operation} returned a non-object result"
            )
        return result

    def list_candidates(
        self, *, status: str | None = None, candidate_kind: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return self._invoke(
            "knowledge.candidate.list",
            {"repo_id": self.repo_id, "status": status, "candidate_kind": candidate_kind, "limit": limit},
        )

    def show_candidate(self, candidate_id: str) -> dict[str, Any]:
        return self._invoke(
            "knowledge.candidate.show", {"repo_id": self.repo_id, "candidate_id": candidate_id}
        )

    def review_candidate(self, candidate_id: str, *, decision: str, note: str | None, basis_revision: str) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        return self._invoke(
            f"knowledge.candidate.{decision}",
            {"repo_id": self.repo_id, "candidate_id": candidate_id, "notes" if decision == "approve" else "reason": note},
            basis_revision=basis_revision,
            idempotency_key=str(uuid4()),
        )

    def intake_candidate(self, candidate: dict[str, Any], *, basis_git_revision: str) -> dict[str, Any]:
        if not _GIT_REVISION.fullmatch(basis_git_revision):
            raise ValueError("--basis-git-revision must be a full 40- or 64-hex commit")
        tags = candidate.get("tags", [])
        if isinstance(tags, str):
            import json
            tags = json.loads(tags)
        if not isinstance(tags, list):
            raise ValueError("candidate tags must be an array")
        summary = candidate["summary"]
        detail = candidate.get("detail")
        event_id = candidate["source_event_id"]
        payload = {
            "repo_id": self.repo_id,
            "local_candidate_id": event_id,
            "source_event_id": event_id,
            "source_sprint_id": candidate["source_sprint_id"],
            "source_item_id": candidate.get("source_item_id"),
            "source_track": candidate.get("track_name"),
            "source_actor": candidate.get("source_actor"),
            "source_type": candidate.get("source_type"),
            "source_created_at": candidate.get("source_created_at"),
            "source_payload": candidate.get("source_payload"),
            "event_type": candidate["event_type"],
            "candidate_kind": candidate["candidate_kind"],
            "summary": summary,
            "detail": detail,
            "tags": tags,
            "confidence": candidate.get("confidence"),
            "content_digest": proposal_digest(summary, detail),
            "basis_git_revision": basis_git_revision,
            "extracted_at": candidate["extracted_at"],
        }
        key = f"kctl-intake:{self.repo_id}:{event_id}:{basis_git_revision}:{payload['content_digest']}"
        return self._invoke("knowledge.candidate.intake", {"candidate": payload}, basis_revision=basis_git_revision, idempotency_key=key)

    def list_publications(
        self, *, category: str | None = None, source_kind: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return self._invoke(
            "knowledge.publication-reference.list",
            {"repo_id": self.repo_id, "category": category, "source_kind": source_kind, "limit": limit},
        )
