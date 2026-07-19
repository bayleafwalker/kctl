---
decision_id: kctl.ecosystem-public-label
status: decided
date: 2026-07-19
implementation: deferred
---

# Rename the public ecosystem label to Vuoro

## Decision

Rename the public label **AgentOps substrate** to **Vuoro**, but do not rename
the `agentops`, `sprintctl`, `kctl`, `actionq`, or `auditctl` repositories and
packages. This is a public-label-only decision. Page edits and any compatible
navigation or metadata changes are a separately scoped follow-up.

`Vuoro` is Finnish for a turn, shift, or turn of duty. It follows the existing
activity-oriented naming register without promising a multi-operator product:
the system coordinates whose turn owns work and what happens around that turn.
`Talkoot` describes communal work well, but overstates collaboration that the
current single-operator scope deliberately does not provide. `Askel` (step) is
too generic. `Työvuoro` is more namespace-distinct but less concise and harder
to carry internationally.

## Collision and namespace evidence

AgentOps.ai has an established naming footprint: the `agentops` PyPI project,
the `AgentOps-AI/agentops` GitHub repository, the AgentOps documentation and
dashboard, and the `agentops.ai` product site all identify an agent
observability and monitoring product. The local public label occupies adjacent
subject matter and would require repeated qualification in every survey, so it
should not remain the ecosystem name.

Namespace checks on 2026-07-19:

| Candidate | GitHub | PyPI | Domain signal |
| --- | --- | --- | --- |
| `vuoro` | account exists; `bayleafwalker/vuoro` absent | project absent | `vuoro.dev` resolves; `vuoro.app` had no DNS, which is not proof of availability |
| `talkoot` | account exists; `bayleafwalker/talkoot` absent | project absent | `talkoot.app` resolves; `talkoot.dev` had no DNS |
| `askel` | account exists; `bayleafwalker/askel` absent | project absent | checked `.app`/`.dev` names had no DNS |
| `tyovuoro` | account absent; `bayleafwalker/tyovuoro` absent | project absent | checked `.app`/`.dev` names had no DNS |

GitHub account names are already occupied for the three short candidates, but
repository names remain available under the current owner. PyPI returned no
project for all four. Domain checks are discovery signals only; registrar
availability must be confirmed immediately before purchase. Because this
decision changes a label rather than package or repository identity, none of
those namespace results blocks `Vuoro`.

## Why not a full rename

The collision is in public positioning, not in the stable interfaces. A full
rename would churn package imports, artifact paths, deployment names, links,
and repository automation without improving disambiguation beyond the public
label. Keeping implementation identities stable also avoids implying that the
single-operator tools have become a new multi-operator platform.

Primary footprint references: [AgentOps core concepts](https://docs.agentops.ai/v2/concepts/core-concepts),
[AgentOps-AI/agentops](https://github.com/AgentOps-AI/agentops), and
[agentops on PyPI](https://pypi.org/project/agentops/).
