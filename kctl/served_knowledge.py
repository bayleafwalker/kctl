"""Authenticated client facade for repository-scoped Vuoro knowledge reads.

This stays separate from the Sprintctl event source because knowledge results
do not carry a redundant ``repo_id`` envelope; the transport scope is the
authorization boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .source import ServedProfile, SprintctlSourceError, _resolve_file_credential


@dataclass(frozen=True)
class ServedKnowledgeClient:
    profile: ServedProfile
    repo_id: str

    def _invoke(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
                return await client.invoke(operation, arguments, repo_id=self.repo_id)

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

    def list_publications(
        self, *, category: str | None = None, source_kind: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return self._invoke(
            "knowledge.publication-reference.list",
            {"repo_id": self.repo_id, "category": category, "source_kind": source_kind, "limit": limit},
        )

