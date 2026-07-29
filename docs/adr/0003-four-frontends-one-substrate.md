# ADR 0003 — Four front-ends over one substrate

**Status.** Accepted.

## Context

The project must serve four purposes: conformance checking, benchmarking, RL training, and
eventually driving a real deployment. The obvious path is four tools that share some code.

## Decision

One substrate, one exposure layer, four thin front-ends. Dependency direction is
`core <- exposure <- front-ends`, enforced in CI by import-linter rather than by
convention. No front-end may import `core`. No model may import either.

## Consequences

A model trained in `gym`, checked by `conformance` and scored by `bench` runs in `deploy`
with no code change. That property is the main architectural argument and is worth real
cost to preserve.

The cost is real: the flat RL interface would be simpler if it could reach into substrate
state, and it may not. `gym/flat` is the single permitted exception to the
no-summarisation invariant and its flattening code is confined to `gym/`.

## Alternatives rejected

*Separate tools sharing a library.* Drifts. The RL env grows a convenience that conformance
does not have, and after a while a model that trains cannot be checked.
*One monolithic tool with modes.* Every mode pays for every other mode's complexity.
