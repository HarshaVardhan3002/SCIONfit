# 0026 — An incident is not an event, and a shipped failing model is not a regression

Status: accepted
Date: 2026-09-03

## Context

An adversarial review of the phase 5–7 code produced six confirmed defects. Three
were plain bugs and are fixed without needing a decision recorded here: a scripted
policy that read a zero estimate as free, a decision-family metric that forgot to
trim the warmup, and a pre-run estimate priced for one core while the run took the
whole machine.

The other three each had two defensible answers.

**What counts as one fault.** A declared `Disturbance` is expanded at build time
into one `TimelineEvent` per link it drew (ADR 0022). `n_faults` counted those
events, so the identical `outage` axis value reported 7 at the smoke tier, 81 at
dev and 1,001 at realistic. The support family exists to be divided by — its own
docstring says a report divides by these to decide how many digits a value has
earned — so the same declared bad day carried three different confidences, and
CLAUDE.md's rule that an axis whose meaning depends on the tier is not an axis was
broken by the metric rather than by the axis.

**Where the recovery clock starts.** `_recovery_window` anchored on the earliest
scheduled fault. A scenario with two disturbances, the first drawing links no
driven scope was using, therefore charged the model for the quiet interval between
them: 13.0 s reported against a true 3.0 s on a two-fault series. The shipped
`regime-shift` axis value schedules two disturbances.

**Where a model that exists to fail a probe lives.** Invariant 6 says every probe
ships with a reference model that fails it. R13's only failing model was a
subclass defined inside its own test, overriding a class constant no constructor
exposes — satisfying the invariant by the letter while leaving a reviewer nothing
in the catalogue to point at. But `REFERENCE_MODELS` is the registry
`tests/test_trace.py` folds into the pinned conformance trace, and a deliberate
FAIL pinned there reads as a regression on every future diff.

## Decision

**An incident is one `(instant, kind)`, not one event.** `n_faults` deduplicates.
Two disturbances of the same kind at the same instant differing only in parameters
therefore count as one incident, which is the right reading: it is one thing that
happened to the network at one moment.

**The recovery clock starts at the fault the disturbance is attributable to** —
the last fault scheduled at or before the sample where cost left its band — not at
the earliest fault in the run. The *baseline* deliberately still comes from before
the first fault. That asymmetry is intentional: recomputing the baseline from
before the anchor would move the band, which moves the onset, which moves the
anchor.

**A model that ships to fail a probe goes in `exposure.loading.BUILTIN_MODELS`,
not in `REFERENCE_MODELS`.** That is the slot `gbdt`, `llm` and `prober` already
occupy: loadable by name, absent from the pinned trace. `FrozenConformal` —
`ReferenceStochastic` with its adaptive-conformal step at zero — is registered
there and fails R13 on 6 of 12 seeds while `reference` fails it on none.

## Consequences

- `n_faults` is comparable across tiers, which is what a support count has to be.
  It no longer answers "how many links went down"; nothing asked it to.
- `time_to_recover_s`, `recovered`, `cost_during_recovery` and `recovered_to` all
  move for any multi-disturbance scenario. Recovery figures recorded before this
  are not comparable with figures recorded after it.
- `mean_deviation` moves for every cell, by roughly the size of its opening
  transient — about 20× on a synthetic series that converges at the warmup.
- The pinned conformance trace hash does **not** move, because the new model is
  outside the registry the trace folds. That was the point of putting it there.
- Invariant 6 now holds for R11 (a constructor flag on a registered model), R12
  (graded, never blocking, by design — ADR 0023) and R13 (a catalogue entry) by
  three different mechanisms. If a fourth probe needs a fourth, that is a smell.

## Alternatives rejected

- **Leaving `n_faults` as an event count and adding `n_incidents` beside it.**
  Two support counts for one family, and the wrong one is the one a report already
  divides by.
- **Deduplicating on `(instant, kind, params)`.** Distinguishes "a tenth mildly"
  from "a tenth hard" scheduled together, which the seed fix already separates in
  the draw. As a count of incidents it is worse: two simultaneous degrades of the
  same links is one thing the network experienced.
- **Recomputing the baseline from before the anchor fault.** Circular, as above.
  Provably-quiet ground beats slightly-fresher ground.
- **Anchoring on the *nearest* fault to the onset in either direction.** Would let
  a fault scheduled after the disturbance began explain it. Causality is not
  negotiable, even in a simulation.
- **Making `ETA_ACI` a constructor parameter, mirroring `layered(discipline=…)`.**
  Defensible and nearly chosen. Rejected because the failing configuration is not
  a tuning of the model, it is a different model — one that calibrates once and
  trusts it forever — and a named class says that where a keyword argument does
  not.
- **Adding `frozen` to `REFERENCE_MODELS` and re-pinning the trace hash.** Puts a
  permanent expected-FAIL into the artifact whose whole job is to make an
  unexpected change visible.
