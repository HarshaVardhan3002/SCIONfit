# What each probe actually does

Ten probes, one per requirement. Each changes exactly one thing.

Statuses: `PASS`, `WEAK`, `FAIL`, `DECLARED_ABSENT`, `FALSE_CLAIM`,
`NOT_APPLICABLE`, `ERROR`.

The gating rule is worth stating plainly, because it is the part people get
wrong:

| you declared | probe says | result |
|---|---|---|
| capability absent | fails | `DECLARED_ABSENT` — honest, recorded, not punished |
| capability absent | passes | `PASS`, with a note to update your declaration |
| capability present | fails | `FALSE_CLAIM` — the only outcome worse than failing |

A wrong declaration is treated as worse than a missing capability because it
silently corrupts every downstream comparison that trusts it.

---

**R1 · Shared-link coupling.** Degrades one interface that lies on some paths
but not all. Measures the mean relative change in predicted cost on the paths
that use it against those that do not. Failing means the model scores paths
independently and cannot represent a shared bottleneck.

**R2 · Composition onto an unseen path.** Assembles a path from interfaces the
model has observed, but that was never measured as a unit, and asks for a
prediction. Compares against the world's ground truth.

**R3 · Missing is not zero.** Runs two identical episodes. In one, a metric is
`None`; in the other it is `0.0`. Identical predictions is a failure: the model
is imputing missingness away, which is the single most common silent defect in
models trained on sparse telemetry.

**R4 · Survives topology churn.** Adds interfaces that did not exist at reset
and a path using them. Checks the model neither raises nor silently drops the
path, and that it is not *more* confident about a never-observed path than
about a measured one.

**R5 · Distributional output.** Requires at least three quantiles per metric,
correctly ordered. A point estimate cannot distinguish a well-measured path
from an unobserved one, and everything downstream needs that distinction.

**R6 · Demand sensitivity.** Same state, two demand vectors: uniform, and 95%
concentrated on one path. Identical predictions mean the model cannot see the
effect of its own advice. This is the probe the whole project turns on.

**R7 · Monotone in load.** Sweeps demand on one path from 5% to 95% and checks
predicted cost never falls. Non-monotonicity permits multiple self-consistent
equilibria, which is what lets a solver cycle.

**R8 · Assignment, not ranking.** Checks `advise()` returns a distribution.
A one-hot advisory sends every host to the same path, which is the herding
failure mode stated directly.

**R9 · Advice survives its own consequences.** Takes the advisory, computes the
load it induces, re-predicts under that load, and checks whether the
recommended path is still the best one. Returns `NOT_APPLICABLE` if the model
ignores demand, because returning the same answer twice is repeatability, not
self-consistency, and passing a model for that would be misleading.

**R10 · Confidence falls with information age.** Withholds observations for 600
simulated seconds and checks that the advisory relaxes and reported confidence
drops. Stale advice stated confidently is worse than no advice, because every
host acts on the same stale reading at once.

**R11 · Layer discipline.** Holds the static layer exactly fixed, congests only
the dynamic one, and checks the model's *published ranking* rather than the order
of its point estimates. That distinction is the design: a latency-class model is
entitled to nowcast congestion and is not entitled to let the nowcast decide who
goes first, because ranking on a fifteen-second-old congestion estimate is
routing on lagged load — everyone moves to the path that was cheapest fifteen
seconds ago, which makes it the most expensive, and the population swaps again
next round. Applies only to a model that declares
`requirement_class="latency"`; empty is `NOT_APPLICABLE`, because a model that
claims nothing has claimed nothing this can contradict. The probe stages its own
experiment before grading it — it escalates the congestion until the world's own
cheapest path has genuinely changed, and returns `NOT_APPLICABLE` rather than a
pass when nothing short of saturation does.

**R12 · Identity churn hygiene.** Re-signs every path — new identifiers,
identical interface sequences, identical network — and measures how much of the
model's prediction survives. **Grades, never blocks.** Q1 resolved that the
deployed fingerprint hashes the interface sequence alone, so a model that loses
its memory across a rename carries a hygiene defect rather than failing a
deployment requirement. It is still worth knowing: such a model discards its
whole history every refresh cycle while every one of its outputs still looks
plausible.

**R13 · Calibration under shift.** Measures the empirical coverage of the
model's own interval, degrades a link, and measures how long coverage takes to
come back. A fixed-width interval never recovers; an adaptive level does.
Deliberately *not* tied to the `distributional` flag: a model whose intervals do
not recover has not lied about being distributional — it is distributional and
badly calibrated, which is a different and more interesting finding.
