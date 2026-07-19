# Knowledge Base — kctl

Register capture: 2026-07-19. These entries record design conclusions only;
they carry **no build intent**. The implemented scope remains the
single-operator claim protocol in
[`sprintctl.claim-ownership`](https://github.com/bayleafwalker/sprintctl/blob/main/docs/protocols/claim-ownership.md).
Source: the 2026-07-19 design conversation and its two supplied brainstorm
documents; those discovery documents are not treated as external evidence.

## Design knowledge

### Keep assignment, claim, attempt, submission, and acceptance separate

Tags: `assignment`, `claim`, `attempt`, `submission`, `acceptance`, `sprintctl`

The durable coordination model has five distinct records. Assignment says who
is accountable for work; a claim grants temporary execution authority; an
attempt records one concrete execution; submission ends that attempt by
offering evidence and a result; acceptance is a separate judgment that the
result is sufficient. Collapsing any adjacent pair loses either authority,
history, or review meaning. No build intent.

---

### Claims end at submission; review holds no lease

Tags: `claim`, `submission`, `review`, `lease`, `sprintctl`

A worker releases or expires its execution claim when it submits. Review is
not execution and must not hold the lease, because a long human review would
block useful new attempts while providing no fencing value. Acceptance remains
possible after the claim has ended. No build intent.

---

### Treat the review-gap with a preferred_principal advisory window

Tags: `review-gap`, `preferred_principal`, `submission`, `reacquire`, `sprintctl`

Ending the claim at submission creates a review gap: requested fixes may need
the prior worker, but another worker can claim first. The candidate remedy is a
short `preferred_principal` advisory window attached to the submission. It
nudges reacquisition toward the prior principal without extending the lease or
creating hidden exclusivity. No build intent.

---

### Use explicit claim modes for future public contribution

Tags: `claim-modes`, `none`, `advisory`, `exclusive`, `maintainer-approved`, `sprintctl`

Any future public-contribution surface should name its claim posture as one of
`none`, `advisory`, `exclusive`, or `maintainer-approved`. The mode determines
whether parallel attempts are merely visible, discouraged, prohibited, or
gated by a maintainer. It is not a phased plan and does not change current
single-operator exclusivity. No build intent.

---

### lease_epoch is the fencing mechanism that TTL cannot provide

Tags: `lease_epoch`, `fencing`, `TTL`, `expiry`, `sprintctl`

TTL answers whether a lease is considered live at backend time; it cannot stop
a partitioned former holder from performing a stale external side effect.
`lease_epoch` is the monotonic fencing value a future downstream mutation can
compare. The schema carries it now, while expected-epoch enforcement remains
explicitly deferred. No build intent.

---
