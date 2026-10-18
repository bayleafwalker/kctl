# Legacy knowledge owner reconciliation: kctl#2103

Date: 2026-10-18. Disposition: withdraw the proposed legacy candidate-intake
extension under kctl#2103. The current work belongs to the Workspace owner;
this is not acceptance of a new kctl operation or a claim that caller authority
was fixed or revoked. The coordinating session owns the tracker transition.

## Governing source and owner boundary

The source revision is Vuoro
`aa5af4de351f011c1d4f795eaa72b692c5dfcb47`:

- [Phase 0 baseline and contract](https://github.com/bayleafwalker/vuoro/blob/aa5af4de351f011c1d4f795eaa72b692c5dfcb47/docs/plans/2026-10-14-knowledge-workspace-phase0.md)
  records the October 14 implementation approval, keeps kctl retiring with no
  new operations, and assigns Workspace its own owner. The note's proposed
  metadata is unchanged; this reconciliation does not ratify it.
- [Workspace composition](https://github.com/bayleafwalker/vuoro/blob/aa5af4de351f011c1d4f795eaa72b692c5dfcb47/docs/architecture/knowledge-workspace-composition.md)
  binds `vuoro-workspace/v1`, describes contributor/reviewer separation and
  credential-plus-membership authorization, and preserves legacy data through
  separate schema configuration rather than an import.

The coordinator observed deployed catalog revision
`37292f51f8cbc643e34c57cd122dcb584e97bca3ca001adaa1fc430b14d02ebc`.
Its knowledge operations expose Workspace's `knowledge:*` authorities, not the
legacy kctl candidate/review catalog. This observation applies to that catalog;
it does not establish absence of legacy code or state elsewhere. The deployment
owner confirmed Workspace's separate `knowledge_workspace` schema, retention
of legacy tables unchanged, no bridge, and unchanged caller grants.

Workspace contributors prepare content and proposals. Reviewers approve or
reject them under the current role and credential checks. Agents remain capped
at contributor, and a proposal's author cannot review it. These boundaries do
not establish that an ordinary principal has contribution authority or
membership, or that any real intake request would be accepted.

## Parent-run authority verification

The coordinator ran the following existing tests from the Vuoro source revision
above in an isolated environment with temporary PostgreSQL 18.6 fixtures:

```bash
cd /projects/dev/vuoro
UV_PROJECT_ENVIRONMENT=/tmp/kctl-workspace-authority-venv \
VUORO_WORKSPACE_TEST_PG_BIN=/nix/store/4nz2y9q8kdn78axpm4jd7c1phmiygg58-postgresql-18.6/bin \
uv run --frozen --package vuoro-workspace --extra test pytest -q \
  packages/vuoro-workspace/tests/test_vuoro_adapter_served.py::test_catalog_registers_every_knowledge_operation_with_its_authority \
  packages/vuoro-workspace/tests/test_vuoro_adapter_served.py::test_missing_knowledge_authority_is_refused_by_the_shell \
  packages/vuoro-workspace/tests/test_vuoro_adapter_served.py::test_membership_is_intersected_on_every_call \
  packages/vuoro-workspace/tests/test_workspace_failure_scenarios.py::test_s5_contributor_agent_and_author_cannot_approve
```

Reported result: **4 passed, 1 deprecation warning in 2.13 s** (coordinator tool
session 54038). This is bounded example evidence of catalog authorities, shell
refusal, membership intersection, and contributor/agent/author review refusal.
The documentation session inspected the selectors and source, but did not
rerun those owner tests. It adds no live service acceptance, new grants,
membership, import, migration, or cross-host deployment evidence.

## Documentation reconciliation and limits

README and AGENTS routing now distinguish the deployed Workspace owner from
the retained kctl adapter. The July alignment plan retains its original
ratification metadata with a historical-scope note. The central workflow
contract remains a legacy implementation/recovery reference; its semantics and
historical verification results are preserved. Current context hashes are
rebound to the clarified protocol text, without rewriting prior result packets.

This change is documentation and verification metadata only. The runtime,
legacy tables, grants, package pins and versions remain unchanged. This file
becomes cross-host-replicated Git evidence when merged; the temporary test
environment is session-local. No ordinary-principal contribution acceptance or
S4 import/cutover is established.
