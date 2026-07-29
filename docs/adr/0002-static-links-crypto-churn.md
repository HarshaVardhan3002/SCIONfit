# ADR 0002 — Physical links are static; cryptographic material churns

**Status.** Accepted. Supersedes the churn model in `scionfit` v0.1.

## Context

`scionfit` v0.1 modelled inter-AS links as appearing and disappearing roughly hourly, and
probe R4 tested whether a model survived encountering interfaces that did not exist at
reset. Domain review established that this is **wrong**.

In SCION the physical inter-AS topology is stable. What refreshes constantly is the
beaconed path segment: it is signed, carries an expiry, and is periodically re-beaconed. A
refreshed segment can describe an identical interface sequence while carrying new
cryptographic material.

## Decision

The substrate treats physical topology as **immutable** for the duration of a run. Real
topology change is an explicit scheduled scenario event, never background noise.

Path segments carry an expiry and are re-signed on a configurable interval. Every path
exposes two identifiers: `structural_id`, a stable hash of the ordered interface sequence,
and `segment_id`, tied to current cryptographic material. `identity_policy` selects which
one `path_id` aliases at the exposure boundary.

Probe R4 is rewritten: run the same episode under both policies and compare. A
structure-keyed model is unaffected; an identity-keyed model collapses under
`crypto_bound`.

## Consequences

R4 becomes one of the more valuable probes rather than one of the weaker ones. It now
detects **identity amnesia**: a model that silently discards its entire per-path history on
every beacon refresh while its outputs still look plausible. That failure is invisible
without this test.

`env/world.py` from v0.1 is discarded. Scenario files gain a scheduled-topology-event type
so genuine changes remain expressible.

Whether the real fingerprint is stable is open question Q1. Both policies are implemented
and neither is the blessed default until it is answered. Nothing may hardcode an
assumption about it.

## Alternatives rejected

*Keep modelling link churn.* Contradicts the domain.
*Model only structural identity.* Would make identity amnesia untestable, which is exactly
the failure mode the correction revealed.
