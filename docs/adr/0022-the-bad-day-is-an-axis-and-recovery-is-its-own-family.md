# 0022 — the bad day is an axis, and recovery is its own family

Status: accepted
Date: 2026-09-03
Milestone: M6 (Phase 6½)

## Context

`bench/axes.py` has six axes and every one of them varies a *condition* held for the
whole run: how many hosts, how many defectors, how stale the telemetry, how hard the
probe limiter bites. M6 names ten scenarios — `steady`, `contention`, `diurnal`,
`regime-shift`, `churn`, `outage`, `cold-start`, `defectors`, `budget-squeeze`,
`slow-model` — and none of them is a condition. A scenario is an **event schedule**:
something happens at a time, and the question is what happens next.

The substrate has been able to schedule events since ADR 0007 and the sweep has never
told it to. Everything the registry scores, scores an episode *as a whole*: a model that
never recovers from an outage and a model that recovers in nine seconds land within noise
of each other, because the recovery is thirty samples out of six hundred and the mean
swallows it. That is the wrong answer to the question the product is for.

Two obstacles had to be resolved before an axis could carry a fault.

**A timeline event names a link by index, and an axis cannot know one.** `link_degrade`
takes `link: int` and `Substrate._validate` refuses an index past `topology.n_links` —
correctly, since an event naming a link that does not exist is a scenario that lies. But
`Tier.n_links` is documented as *a target, not a guarantee*: the generator hits it within
a few. An axis value written against the smoke tier's indices is therefore refused at the
realistic tier, and an axis whose meaning depends on the tier is not an axis.

**Recovery needs the perturbation instant, and `MetricInput` never carried it.** The
event timeline is plain data, it sits in the scenario the cell was built from, and no
metric could see it.

## Decision

**1. A `Disturbance` is declared against the topology's *shape*, and expanded against the
topology itself.** A new frozen `Disturbance` in `core/scenario.py` names a kind, a time
as a fraction of the run, and a *fraction or count* of links or ASes rather than their
indices. `Substrate._install_timeline` expands it — after `self.topology` exists, where
the true counts are known — into ordinary `TimelineEvent`s that go through the same
validation as a hand-written one. The expansion is seeded from the scenario seed, so the
same seed picks the same links, and both halves of a parity pair get the same bad day.

The expanded events are kept on `Substrate.timeline`. That is what a scenario *did*, as
against `Scenario.timeline`, which is what was written by hand.

**2. `scenario` is the seventh axis.** Its baseline value is `steady`, which is an empty
timeline and therefore exactly what every recorded cell already ran. The other values are
the disturbances the substrate implements: `outage` (a tenth of the links degraded hard,
then restored), `brownout` (a third of the links degraded mildly and never restored),
`filtered` (a fifth of the ASes stop answering with segments), `surge` (exogenous load on
every scope), and `regime-shift` (a surge that arrives and stays, with links degraded
under it).

**3. `recovery` is a fifth metric family, and nothing aggregates over it.** Four metrics:

| metric | what it answers |
|---|---|
| `recovered` | did it come back to the pre-event band at all, before the run ended |
| `time_to_recover_s` | how long that took, `None` if it never did |
| `cost_during_recovery` | mean fractional excess over the pre-event level while it was away |
| `recovered_to` | the new operating point over the old one — 1.0 is where it started |

`recovered` exists so the "never" case cannot hide behind a `None`. A single number would
have to choose between "it recovered in 9 s" and "it did not recover", and a metric that
returns `None` for the second is a metric a report renders as *not measured* — which is
how a model that never recovers scores like a model nobody watched.

A fifth family rather than four metrics filed under `stability` because the plan's
requirement is that steady-state and survival are **reported apart and never averaged**.
A family is the unit the report already summarises over, so putting recovery inside
`stability` would produce exactly the aggregate that must not exist.

## Consequences

- The six axes become seven and the baseline cell gains `scenario=steady`. Every suite
  digest changes, so every existing result file is stale under the new digest and will be
  re-run rather than silently mixed with cells that had no scenario axis. That is the
  correct behaviour and it is expensive once.
- Under one-at-a-time the scenario axis adds five cells per model per repeat.
- `MetricInput.events` is populated for every run, including `steady`, where it is empty
  and all four recovery metrics return `None`. A cell with no fault has nothing to recover
  from, and that is not the same as a cell that failed to recover.
- `Substrate.timeline` is new public surface on the substrate. It is read-only and it is
  read by `instrument`, which is the allowed direction.

## Alternatives rejected

**Name link indices in the axis and clamp them to the tier.** Two tiers would then apply
the same axis value to different fractions of their network — 6 links out of 60 and 6 out
of 10,000 — and the axis would mean "a serious outage" at smoke and "nothing" at
realistic while reading identically in both result files.

**Resolve the disturbance by building the world twice**: once to find which links carry
the driven scopes' traffic, once with the timeline that targets them. It aims the fault
better and it costs a realistic-tier build per cell, and the aiming is itself a confound —
a fault placed where the model is looking is a different experiment from a fault placed at
random, and only the second is a fair one across models.

**Recovery metrics inside `stability`.** Rejected above: the family is the aggregation
unit, and the requirement is that there be no aggregate.

**A single `recovery_score` combining all four.** It is the number a reader would quote,
it is the number that would let an excellent-in-the-ordinary-case model look deployable,
and there is no defensible weighting between "came back" and "came back cheaply".

**Extending `EVENT_KINDS` with a `fractional_link_degrade`.** The expansion belongs on the
scenario, not in the clock: the substrate should keep receiving events that name exactly
what they touch, so a trace still says which link went.
