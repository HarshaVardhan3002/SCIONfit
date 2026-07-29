# ADR 0001 — Record architecture decisions

**Status.** Accepted.

## Context

This project will be built by several agents and people over a long period, with a large
gap between the ambition and any single milestone. Decisions made early for good reasons
get reversed later for bad ones when the reason is not written down. Two assumptions have
already had to be unwound: the churn model, and the topology-degeneracy bug that made a
correct model look demand-blind.

## Decision

Every architectural decision gets a short ADR in `docs/adr/`, written **before** the code
that implements it. Format: context, decision, consequences, alternatives rejected. Short.
If a decision needs more than a page, it is probably two decisions.

An ADR is required for: any dependency added to `core/`, any change to a seam, any change
to the domain model, any deviation from an upstream we said we would reuse, and any
performance-motivated design compromise.

## Consequences

Slightly slower to start each piece of work. Much faster to revisit it. A future
contributor can tell the difference between a decision and an accident, which is the whole
point.

## Alternatives rejected

*Code comments.* They explain what, rarely why, and never what was rejected.
*A single design document.* Goes stale, and nobody notices which parts are stale.
