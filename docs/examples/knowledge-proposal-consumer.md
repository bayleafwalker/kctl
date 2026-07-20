# Consuming knowledge-proposal/v1: a worked example

This walks through the intended loop for `kctl export-proposal`: kctl reads
its own approved-but-unpublished candidates and writes a proposal artifact;
a human or a separately authorized agent reads that artifact and decides
whether to act, and if so runs the sprintctl command themselves. kctl never
performs the last step. See `docs/protocols/knowledge-proposal-v1.md` for the
full record contract and `docs/protocols/knowledge-lifecycle.md` for how
`approved` fits into kctl's candidate lifecycle.

## 1. kctl writes the proposal artifact (read-only)

```sh
kctl export-proposal --artifacts-root /projects/dev/_artifacts --repo-id kctl
```

```text
Wrote 1 approved knowledge proposal(s) to /projects/dev/_artifacts/kctl/knowledge/knowledge-proposal-v1.ndjson
This is a proposal only — run the suggested sprintctl command yourself to act on it. kctl does not create or modify sprintctl items.
```

This command only reads `knowledge_candidate` rows with `status = 'approved'`
from kctl's local SQLite store and atomically rewrites one NDJSON file. It
never opens a connection to sprintctl and never runs a sprintctl command.

## 2. The artifact: one line per approved candidate

A representative fixture record (see
`verification/examples/knowledge-proposal-v1.ndjson`):

```json
{"candidate_id":17,"content_digest":"sha256:e8a60524b505996f068a34d7b38a725b143037ae7fb9152dc8835ed32691a884","detail":"Prefer asymmetric verification so services can validate without sharing a signing secret.","extracted_at":"2026-07-17T18:00:00Z","provenance":{"event_id":781,"event_ref":"sprintctl:event:781","item_id":942,"reviewed_at":"2026-07-17T18:05:00Z","reviewed_by":"human-reviewer","sprint_id":379,"track":"remote-mode"},"rendered_at":"2026-07-17T18:20:00Z","repo_id":"homelab-analytics","schema_version":"knowledge-proposal/v1","status":"approved","stream":"durable","suggested_next_action":"propose sprintctl item add in repo homelab-analytics","suggested_owner_repo":"homelab-analytics","summary":"Use RS256 for cross-service verification","tags":["architecture","auth"]}
```

Everything a reviewer needs is here: the reviewed content (`summary`,
`detail`, `tags`), where it came from (`provenance`, tracing back to
`sprintctl:event:781`), and a suggestion of what to do next
(`suggested_owner_repo`, `suggested_next_action`). Nothing here claims a
sprintctl item already exists.

## 3. A human or dispatched agent evaluates the proposal

This is the step kctl cannot take on your behalf. A reviewer (human, or an
agent that holds its own sprintctl claim/authority — never kctl acting as
that agent) reads the record above and decides:

- Is this still relevant?
- Is `homelab-analytics` really the right owner repo, or should it be filed
  elsewhere?
- What sprint and track should hold the new item?

## 4. Acting on the proposal: an explicit sprintctl command

If the reviewer agrees, *they* — not kctl — run the sprintctl mutation
command directly, using `provenance` to make the new item traceable back to
the source event:

```sh
sprintctl item add \
  --sprint-id 379 \
  --track remote-mode \
  --title "Use RS256 for cross-service verification" \
  --description "Prefer asymmetric verification so services can validate without sharing a signing secret. Proposed from kctl candidate #17 (sprintctl:event:781), reviewed by human-reviewer on 2026-07-17T18:05:00Z."
```

kctl's proposal artifact is never updated to say "accepted" — kctl has no
such state. If the reviewer instead runs `kctl publish --id 17 ...`, the
candidate becomes a published knowledge-base entry and simply stops
appearing in the next `kctl export-proposal` run; if the reviewer takes no
action, the candidate keeps appearing in each proposal snapshot until it is
published or the underlying candidate state changes through kctl's normal
review commands.

## What this preserves

- kctl only ever reads its own SQLite store and sprintctl's event stream —
  see `kctl/source.py` and `AGENTS.md` for the read-only boundary.
- `kctl export-proposal` calls no sprintctl command, mutation or otherwise.
- The proposal artifact carries a suggestion, not a decision. The decision
  and the `sprintctl item add` invocation both belong to a human or a
  separately authorized agent, never to kctl.
