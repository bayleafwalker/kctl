"""Immutable PostgreSQL migration assets for the served knowledge authority."""

MIGRATION_ASSETS: tuple[tuple[str, str], ...] = (
    (
        "candidates_and_reviews",
        r"""
        CREATE TABLE __SCHEMA__.knowledge_candidate (
            candidate_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id            text        NOT NULL CHECK (repo_id ~ '^[A-Za-z0-9._-]+$'),
            local_candidate_id bigint      NOT NULL CHECK (local_candidate_id > 0),
            source_event_id    bigint      NOT NULL CHECK (source_event_id > 0),
            source_sprint_id   bigint      NOT NULL CHECK (source_sprint_id > 0),
            source_item_id     bigint      CHECK (source_item_id > 0),
            source_track       text,
            source_actor       text,
            source_type        text,
            source_created_at  timestamptz,
            source_payload     jsonb,
            event_type         text        NOT NULL CHECK (event_type <> ''),
            candidate_kind     text        NOT NULL
                                        CHECK (candidate_kind IN ('durable', 'coordination')),
            summary            text        NOT NULL CHECK (summary <> ''),
            detail             text,
            tags               jsonb       NOT NULL DEFAULT '[]'::jsonb
                                        CHECK (jsonb_typeof(tags) = 'array'),
            confidence         text,
            status             text        NOT NULL
                                        CHECK (status IN ('candidate', 'approved', 'rejected', 'published')),
            content_digest     text        NOT NULL
                                        CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
            basis_git_revision text        NOT NULL
                                        CHECK (basis_git_revision ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
            extracted_at       timestamptz NOT NULL,
            imported_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT knowledge_candidate_local_key
                UNIQUE (repo_id, local_candidate_id),
            CONSTRAINT knowledge_candidate_source_event_key
                UNIQUE (repo_id, source_event_id)
        );

        CREATE TABLE __SCHEMA__.knowledge_review (
            review_id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            candidate_id       uuid        NOT NULL,
            decision           text        NOT NULL CHECK (decision IN ('approved', 'rejected')),
            reviewed_at        timestamptz NOT NULL,
            reviewed_by        text        NOT NULL CHECK (reviewed_by <> ''),
            review_notes       text,
            content_digest     text        NOT NULL
                                        CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
            basis_git_revision text        NOT NULL
                                        CHECK (basis_git_revision ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
            imported_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT knowledge_review_candidate_fk
                FOREIGN KEY (candidate_id)
                REFERENCES __SCHEMA__.knowledge_candidate(candidate_id),
            CONSTRAINT knowledge_review_candidate_key UNIQUE (candidate_id)
        );

        CREATE INDEX knowledge_candidate_status_idx
            ON __SCHEMA__.knowledge_candidate
               (repo_id, candidate_kind, status, extracted_at DESC);
        """,
    ),
    (
        "publication_references",
        r"""
        CREATE TABLE __SCHEMA__.knowledge_publication_reference (
            publication_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id            text        NOT NULL CHECK (repo_id ~ '^[A-Za-z0-9._-]+$'),
            local_entry_id     bigint      NOT NULL CHECK (local_entry_id > 0),
            candidate_id       uuid        NOT NULL UNIQUE,
            document_id        text        NOT NULL CHECK (document_id <> ''),
            content_path       text        NOT NULL
                                        CHECK (
                                            content_path <> ''
                                            AND content_path !~ '(^/|(^|/)\.\.(/|$)|\\)'
                                        ),
            content_anchor     text        NOT NULL CHECK (content_anchor <> ''),
            git_revision       text        NOT NULL
                                        CHECK (git_revision ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
            content_digest     text        NOT NULL
                                        CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
            category           text        NOT NULL
                                        CHECK (category IN ('decision', 'pattern', 'lesson', 'risk', 'reference')),
            source_kind        text        NOT NULL
                                        CHECK (source_kind IN ('durable', 'coordination')),
            tags               jsonb       NOT NULL DEFAULT '[]'::jsonb
                                        CHECK (jsonb_typeof(tags) = 'array'),
            published_at       timestamptz NOT NULL,
            superseded_by      uuid,
            imported_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT knowledge_publication_candidate_fk
                FOREIGN KEY (candidate_id)
                REFERENCES __SCHEMA__.knowledge_candidate(candidate_id),
            CONSTRAINT knowledge_publication_local_key
                UNIQUE (repo_id, local_entry_id),
            CONSTRAINT knowledge_publication_repo_identity_key
                UNIQUE (repo_id, publication_id),
            CONSTRAINT knowledge_publication_supersession_fk
                FOREIGN KEY (repo_id, superseded_by)
                REFERENCES __SCHEMA__.knowledge_publication_reference(repo_id, publication_id)
        );

        CREATE INDEX knowledge_publication_document_idx
            ON __SCHEMA__.knowledge_publication_reference
               (repo_id, document_id, published_at DESC);

        CREATE TABLE __SCHEMA__.schema_principal (
            role_kind         text        PRIMARY KEY
                                          CHECK (role_kind IN ('migration', 'runtime')),
            role_name         name        NOT NULL UNIQUE,
            environment_name  text        NOT NULL CHECK (environment_name <> ''),
            environment_class text        NOT NULL
                                          CHECK (environment_class IN ('development', 'production', 'recovery')),
            recorded_at       timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        """,
    ),
    (
        "publication_inline_supersession_evidence",
        r"""
        ALTER TABLE __SCHEMA__.knowledge_publication_reference
            ADD COLUMN inline_supersedes uuid;

        ALTER TABLE __SCHEMA__.knowledge_publication_reference
            ADD CONSTRAINT knowledge_publication_inline_supersession_fk
            FOREIGN KEY (repo_id, inline_supersedes)
            REFERENCES __SCHEMA__.knowledge_publication_reference(repo_id, publication_id);
        """,
    ),
)
