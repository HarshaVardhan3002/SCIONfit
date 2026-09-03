# 0023 — three probes the review made possible, and a demand that says which it is

Status: accepted
Date: 2026-09-03
Milestone: M5 (Phase 6)

## Context

Four things arrive together because each of the three new probes needs the fourth.

**A model handed `Demand` is handed its own intent labelled as load.** `Demand.per_path`
is built from the published advisory, and `exposure/streams.py` is emphatic about why that
is not the same thing: *"`share` is the realised share, not the published weight… a model
that assumes its advice was followed exactly is wrong by exactly that much."* The harness
knows this in one file and forgets it in the other. R9 publishes an advisory, turns it
straight back into a `Demand`, and asks the model whether its advice survives its own
consequences — while feeding it the consequences it *intended* rather than the ones it
got. Sampling noise, defectors, dwell timers and hysteresis all live in that gap, and R9
has been blind to every one of them.

**R4 was aimed at a hazard Q1 removed.** It was written to catch a model that collapses
under `crypto_bound` identifiers. Q1 is resolved: `snet.Fingerprint` hashes the interface
sequence alone, so a fingerprint is stable across re-signing and a model that fails under
`crypto_bound` is not wrong about anything the deployed stack does. Failing it there is a
false positive against exactly the thing conformance claims to measure.

**Nothing tests which layer a model ranks on.** v1.2 §20.6 says a latency-class model
ranks on the static layer plus liveness and lets the dynamic layer widen intervals, never
reorder. The reasoning is a timescale argument: ranking on a fifteen-second-old congestion
estimate is routing on lagged load, which is the documented oscillation mechanism. Today
that defect reaches the closed loop undetected and is attributed to the model's advisory
*shape* rather than to its metric *choice* — the harness reports a model that oscillates
and says nothing about why.

**Nothing tests calibration under shift.** A model with fixed split-conformal intervals and
one with an adaptive level are indistinguishable on a stationary world and behave
completely differently after a degrade. The spec has an opinion about this and the suite
had no probe for it.

## Decision

**1. `Demand` splits into intended and realised.** `per_path` keeps its name and its
meaning — it is the intended share, and a caller that passes only it is unaffected. A new
optional `realised: Mapping[str, float] | None` carries what telemetry actually saw, and
`Demand.gap()` returns the per-path difference. `None`, and a path missing from a non-`None`
mapping, both mean *no telemetry covers this*, which is not the same as *no traffic*: the
first is ignorance and the second is a measurement, and a probe that conflated them would
score a model for a path nobody was using.

`Session.realised_shares()` exposes what `_harvest_telemetry` already computes, and
`demand_from_advisory` takes it. R9 now feeds back the realised load and grades the model
against the gap it was actually handed.

**2. R4 is re-aimed and R12 takes the hygiene question.** R4 keeps its implementation — it
tests that a model scores a path built from interfaces that did not exist at reset, which
is the real generalisation requirement — and its documentation now says that is what it is
for. The identity question moves to a new **R12**, which re-signs a fraction of the paths
(new identifiers, identical interface sequences) and measures how much of the model's
prediction survives. It reports a **grade and never a blocking failure**: `crypto_bound`
has no correspondent in the deployed stack, so a model that loses memory across it is
carrying a hygiene defect rather than failing a deployment requirement.

**3. R11 — layer discipline.** One variable: hold the static layer exactly fixed, change
only the dynamic layer (background congestion; no declared attribute moves, no interface
appears or vanishes), and ask a latency-class model for a ranking. A reordering fails. It
pairs with R7, which establishes the model can *see* the dynamic layer; R11 establishes it
uses it in the right place.

Applicability is by a new `Capabilities.requirement_class` — `latency`, `bandwidth`,
`loss`, or empty for a model that did not say. Empty is `NOT_APPLICABLE`, not a failure: a
model that makes no claim about its requirement class has not claimed anything R11 can
contradict.

**4. R13 — calibration under shift.** Measure empirical coverage of the model's own
interval before a scheduled degrade, apply the degrade, and measure how long coverage takes
to come back. A fixed-width interval will not recover; an adaptive level will. Distributional
models only.

**5. `reference/layered.py` ships the model that fails R11.** Invariant 6: a probe nothing
fails measures nothing. `LayeredRanker` declares `requirement_class="latency"` and takes one
flag. With `discipline=True` it ranks on the declared latency and widens with observed
congestion; with `discipline=False` it ranks on the freshest observed latency, which is the
defect R11 exists to catch. Two variants of one architecture, which is also the shape the
report's architecture section groups for.

## Consequences

- `Capabilities` gains a field. Additive and defaulted, so no existing model changes
  behaviour, and it is in before M6 freezes the contract.
- The probe suite goes from ten to thirteen. `ALL_PROBES` is ordered, and the three new ones
  are appended rather than inserted, so an existing report card's column order is unchanged.
- R12 can never block a deployment verdict. That is deliberate and it is a deliberate
  weakening of what R4 used to claim.
- `LayeredRanker(discipline=False)` fails R11 in CI and is asserted to.

## Alternatives rejected

**Rename `Demand.per_path` to `intended`.** Clearer, and it breaks every caller including
the one in the worked adaptor template that model authors copy. The field's meaning was
always "intended"; what was missing was the other one.

**Populate `realised` in the closed loop as well, and pass a `Demand` into `predict`.** The
driver does not pass demand today, so every accuracy number ever recorded was made without
one. Wiring it in would change all of them for a reason unrelated to this ADR, and the
plan's stated targets are the probes.

**Make R11 applicable to every model and infer the requirement class.** Inferring it from
behaviour is circular — the behaviour is what the probe is measuring — and inferring it
from the architecture tag would fail every gradient-boosted model for being gradient
boosted.

**Delete R4 and let R12 replace it.** R4's implementation tests generalisation to unseen
interfaces, which is a real requirement and independent of the identity question. Only its
justification needed correcting.
