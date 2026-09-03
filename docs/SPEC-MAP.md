# SPEC-MAP — every requirement against the thing that covers it

One table. Each row is a requirement the Master Spec states; each row names the probe,
metric, axis or knob that covers it, or says plainly that nothing does.

**Why this file exists.** Bug B1 — the beacon interval wrong by sixty-fold — survived four
milestones because nothing connected a spec sentence to the code that was supposed to
implement it. Q1 and Q6 were found the same way, by reading the source rather than by
reading a map. A table that says *not covered — gap* in twelve places is worth more than a
suite that quietly covers eleven of them and never says which.

**How to read the coverage column.**

| marker | meaning |
|---|---|
| **probe** | a conformance probe fails a model that gets this wrong |
| **metric** | a registered metric scores it; a report prints it |
| **axis** | the sweep varies it, so its effect is measured rather than assumed |
| **substrate** | the world implements it; nothing scores the model on it |
| **gap** | nothing covers it, and it should |
| **deliberate** | nothing covers it, and that is a decision with a reason |

Keep this file honest by moving rows *out* of **gap**, never by deleting them.

---

## §7 — the unit and its composition

| requirement | coverage | where |
|---|---|---|
| A path's state composes from its links | **probe** | R1 shared-link coupling; R2 composition onto an unseen path |
| Latency composes additively, bandwidth as a min, loss multiplicatively | **substrate** | `backends/analytical.World.path_metrics`, `core/linkstate.path_metrics_batch` |
| The *unit* is the identifiable partition of the incidence matrix | **gap** | `exposure/contracts.py` has no `unit`. The largest structural gap, deferred with a written reason in `docs/PLAN.md`, "Deferred, and named" |
| A model predicts unit states and composes them to paths | **gap** | same row; would upgrade R2 from "produces a number" to "its own units compose to its own paths" |
| Forward and reverse availability differ | **substrate** | per-direction link state; `LinkState` carries both |
| Return-path composition is the forward path reversed | **gap** | F2, carried: `rtt_s` is hardcoded at 2x forward. Known divergence, known correct answer, blocks G3 |

## §12–14 — beaconing, segments, identity

| requirement | coverage | where |
|---|---|---|
| Physical inter-AS links are stable for a run | **substrate** | `TopologySpec` builds once; `topology_change` is a scheduled event, not background noise |
| Segments are re-beaconed and re-signed on an interval | **substrate** | `core/segments.BeaconPolicy`; corrected by B1 from 60x too slow |
| A re-signed segment may describe the same interfaces under a new identifier | **probe** | R12 identity churn hygiene (ADR 0023) |
| The deployed fingerprint hashes the interface sequence alone | **substrate** | Q1, resolved. `identity_policy="structural"` is the default; `crypto_bound` stays for R12 |
| A model must not key memory on the identifier | **probe** | R12, graded and non-blocking — `crypto_bound` has no correspondent in the deployed stack |
| A model generalises to interfaces that did not exist at reset | **probe** | R4, re-aimed by ADR 0023 to what it actually tests |
| Segments vanish from path-server answers without the graph moving | **axis** | `scenario=filtered`; substrate `as_policy_filter` |
| Hop-field expiry | **substrate** | `PathRef.expiry_s`; nothing scores a model on respecting it — **gap** |

## §19 — prediction and calibration

| requirement | coverage | where |
|---|---|---|
| Emit a distribution, not a point estimate | **probe** | R5 |
| Quantiles ordered | **metric** | `Dist.quantiles_monotone`; asserted per model in tests |
| Nominal 80% coverage | **metric** | `coverage`, `coverage_gap`, stratified by horizon |
| Sharpness against calibration | **metric** | `interval_width` beside `coverage` — neither is readable alone |
| Adaptive conformal level (§19.5) | **probe** | R13 calibration under shift (ADR 0023); `ReferenceStochastic` implements the mechanism |
| Pinball loss and CRPS | **metric** | `pinball`, `crps` |
| Missing is not zero | **probe** | R3 |
| Confidence falls with information age | **probe** | R10 |
| Horizons +60 s and +300 s beat persistence | **metric** + baseline | `horizons_s=(0, 60, 300)`; `Persistence` is a mandatory baseline so the comparison cannot be skipped |

## §20 — requirement classes and layer discipline

| requirement | coverage | where |
|---|---|---|
| A model declares its requirement class | **substrate** | `Capabilities.requirement_class` (ADR 0023) |
| §20.6: a latency-class model ranks on static + liveness | **probe** | R11 layer discipline |
| The dynamic layer widens intervals, never reorders | **probe** | R11; `LayeredRanker(discipline=False)` is the model that fails it |
| Bandwidth-class and loss-class layer rules | **gap** | R11 grades only `latency`; the other two classes are declared and unchecked |

## §22–24 — demand, assignment, the closed loop

| requirement | coverage | where |
|---|---|---|
| Condition the prediction on offered demand | **probe** | R6 |
| Cost is non-decreasing in demand | **probe** | R7 |
| Output an assignment, not a ranking | **probe** | R8 |
| The published distribution survives its own consequences | **probe** | R9 |
| Intended and realised load are different things | **substrate** | `Demand.realised`, `Demand.gap()`, `Session.realised_shares()` (ADR 0023) |
| A performativity probe over that gap | **gap** | the hook exists; the probe does not |
| The mechanism ladder, rungs 1–2 (model side) | **substrate** | `Capabilities.distributional`, `emits_assignment` |
| The mechanism ladder, rungs 3–4 (population side) | **axis** | `discipline`: epsilon, hysteresis, dwell, synchronised, mirror |

## §28 and M4½ — the campaign

| requirement | coverage | where |
|---|---|---|
| Population size | **axis** | `population` |
| Defector fraction | **axis** | `defectors` |
| Mechanism ladder | **axis** | `discipline` |
| Paths per selector | **axis** | `paths` |
| Staleness | **axis** | `staleness` |
| Probe-limit regime | **axis** | `probes` (Q6: there is no protocol answer, so it is a deployment choice) |
| Ten named scenarios | **axis**, partly | `scenario` covers steady / outage / brownout / filtered / surge / regime-shift. `diurnal`, `churn`, `cold-start` and `slow-model` are **gap**; `defectors` and `budget-squeeze` are the `defectors` and `probes` axes under other names |
| Five mandatory baselines | **substrate** | `MANDATORY_BASELINES`, appended rather than offered |
| Four metric families | **metric** | plus `recovery`, which is not one of the four and is deliberately apart (ADR 0022) |
| Recovery after a fault | **metric** | `recovered`, `time_to_recover_s`, `cost_during_recovery`, `recovered_to` |
| Cost per decision, decision latency | **metric** | `calls_per_decision`, `decision_p50_s`, `decision_p95_s`, `overruns`, `late_calls` |
| A model's own compute is charged | **substrate** | `think` (ADR 0021); `free` by default, and the report says so |

## Probe limits and SCMP

| requirement | coverage | where |
|---|---|---|
| SCMP *may* be rate limited | **axis** | Q6: the audited router implements no limiter, so the regime is a deployment choice. `probes` axis carries audited / unlimited / strict |
| A probe consumes bandwidth while it runs | **substrate** | `Substrate.hold_probe_load` |
| Every tool call is charged | **substrate** | `exposure/budget.py`, `Session.call` |

---

## The gaps, gathered

Twelve rows above say **gap**. In rough order of what they would buy:

1. **The unit as a computable object.** Contract change; decide before M6 freezes the API.
2. **A performativity probe** over `Demand.gap()`. The hook landed with ADR 0023 and nothing uses it yet.
3. **Layer discipline for the bandwidth and loss classes.** R11 generalises; only the
   latency rule is written.
4. **Hop-field expiry.** A model that recommends an expired path should be caught, and is not.
5. **Return-path composition (F2).** `rtt_s = 2x forward` is wrong and the right answer is known.
6. **Four of the ten scenarios**: `diurnal`, `churn`, `cold-start`, `slow-model`. The first
   three are event schedules the substrate can already run; `slow-model` is `think="fixed"`
   with a large `think_s` and wants an axis rather than a suite-wide setting.
