"""Candidate intake, review, and repository-scoped inspection services."""

from __future__ import annotations

from .application_core import *


class CandidateService:
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

    @staticmethod
    def _require_repo(record: dict[str, Any] | None, repo_id: str, *, kind: str) -> None:
        """Fail closed before exposing or mutating a cross-repository record."""
        if record is not None and record["repo_id"] != repo_id:
            # Deliberately use the normal not-found shape: a caller authorized
            # for one repository must not learn that an unguessable central ID
            # belongs to another repository.
            raise KnowledgeNotFoundError(f"{kind} was not found")

    def show_candidate(self, *, candidate_id: str, repo_id: str | None = None) -> dict[str, Any]:
        candidate_id = _uuid(candidate_id, "candidate_id")
        expected_repo = _repo_id(repo_id) if repo_id is not None else None
        with self.connection() as conn, conn.cursor() as cur:
            candidate = self._candidate(cur, candidate_id)
            if expected_repo is not None:
                self._require_repo(candidate, expected_repo, kind="candidate")
            review = self._review(cur, candidate_id) if candidate is not None else None
        return {"candidate": candidate, "review": review}

    def _review_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        notes: str | None,
        evidence: MutationEvidence,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        candidate_id = _uuid(candidate_id, "candidate_id")
        expected_repo = _repo_id(repo_id) if repo_id is not None else None
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
                if expected_repo is not None:
                    self._require_repo(candidate, expected_repo, kind="candidate")
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
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        return self._review_candidate(
            candidate_id=candidate_id,
            decision="approved",
            notes=notes,
            evidence=evidence,
            repo_id=repo_id,
        )

    def reject_candidate(
        self,
        *,
        candidate_id: str,
        reason: str | None = None,
        evidence: MutationEvidence,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        return self._review_candidate(
            candidate_id=candidate_id,
            decision="rejected",
            notes=reason,
            evidence=evidence,
            repo_id=repo_id,
        )



