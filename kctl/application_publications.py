"""Publication references and supersession services."""

from __future__ import annotations

from .application_core import *


class PublicationService:
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
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        predecessor_id = _uuid(predecessor_id, "predecessor_id")
        successor_id = _uuid(successor_id, "successor_id")
        expected_repo = _repo_id(repo_id) if repo_id is not None else None
        if predecessor_id == successor_id:
            raise KnowledgeInputError("a publication cannot supersede itself")
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                successor = self._publication(cur, successor_id)
                if successor is None:
                    raise KnowledgeNotFoundError("successor publication was not found")
                if expected_repo is not None:
                    self._require_repo(successor, expected_repo, kind="publication")
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

    def show_publication(
        self, *, publication_id: str, repo_id: str | None = None
    ) -> dict[str, Any]:
        publication_id = _uuid(publication_id, "publication_id")
        expected_repo = _repo_id(repo_id) if repo_id is not None else None
        with self.connection() as conn, conn.cursor() as cur:
            publication = self._publication(cur, publication_id)
            if expected_repo is not None:
                self._require_repo(publication, expected_repo, kind="publication")
        return {"publication": publication}



