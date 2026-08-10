# 0010 — Measuring oscillation by amplitude, not periodicity

Status: accepted, revised in M3 (see "Revision", which corrects the evidence below),
and the revision's own mechanism superseded in M4 by
[0011](0011-sampling-off-the-worlds-clock.md) — read the note at the end before
quoting the revision table
Date: M3
Supersedes nothing. Amends the measurement half of [0009](0009-closing-the-loop-hosts-delay-and-realised-load.md).

## Context

M3 promised a headline result: a greedy ranked-advice model oscillates at scale
and a stochastic one does not, separated by an oscillation index above 0.5
against below 0.15. That index is spectral peak dominance — the share of
fast-band power sitting in its single largest bin — ported unchanged from the
proposal's toy simulation.

Running the real closed loop, the separation does not appear. Eight scopes,
smoke tier, 240 decision rounds, same seed and same scenario for both models:

| model | peak dominance (link) | fast-band RMS (advised load) | RMS (realised split) | mean path cost |
|---|---|---|---|---|
| `minrtt` (greedy) | 0.147 | 3.49 | 0.474 | 1360 ms |
| `reference` (stochastic) | 0.200 | 0.54 | 0.114 | 451 ms |

Peak dominance does not merely fail to clear 0.5, it **ranks the two models the
wrong way round**. Everything else separates them by between four and twenty-six
times, in the expected direction, and the underlying series are not subtle: the
greedy model throws its entire population from one path to another and back,
round after round. The pathology is present and severe. The detector is blind
to it.

The reason is in the detector's own docstring, which assumed three generators of
movement: slow environment tracking, flat sampling noise, and "a control loop
stampeding — one sharp peak". The third assumption is the toy's, and the toy had
one scope and one decision maker. At `n = 1` we do see a periodic signature
(`dominant_period` 7.4); by `n = 8` each scope is chasing a bottleneck that
seven others are also moving, decisions land at latencies that differ per turn,
and the switching becomes violent but aperiodic. Broadband movement spreads its
power over the whole fast band, which is exactly the shape peak dominance was
designed to score as *calm*.

A second, smaller confound: total link utilisation includes exogenous
background traffic, which resamples on a 60 s bucket against a 30 s decision
cadence and therefore aliases straight into the fast band. That noise belongs to
the environment, not to the model, and it is what lifted the stochastic model's
dominance score above the greedy one's.

## Decision

**1. The headline detector is fast-band amplitude, `fast_swing`.** Band-pass the
series above `FAST_BAND_MIN` and report the RMS of the residue, in the series'
own units. It answers "how much load does this model move quickly", which is
the question the milestone was always asking; peak dominance answered "and does
it do so on a metronome", which is a property of the toy and not of the
pathology.

**2. Peak dominance is kept and still reported.** It is the right measure for a
single-scope run and it is the number the proposal published, so removing it
would delete the evidence for this ADR. `oscillation_index` and
`dominant_period` stay in the report card. What changes is which number the
milestone criterion is written against.

**3. Detectors run over two link series, not one.** `utilisation` — total,
including background — is what an operator sees and what the figure plots.
`advised_load` — demand attributable to the advised host populations, over
capacity — is what the model caused, and is what the detectors read. Separating
them is not cosmetic: measuring the model on a series containing exogenous noise
is what inverted the ranking above.

**4. The realised split is a first-class series.** `path_share` records each
scope's realised share of a *fixed* reference path, its first. Not the busiest
path: for a winner-take-all model the busiest path's share is 1.0 every round
whichever path is winning, which hides precisely the swapping being measured.

**5. The M3 criterion is restated.**
The criterion becomes a ratio between models on the same scenario and seed —
greedy's fast-band amplitude at least four times the stochastic model's, on both
the advised-load and realised-split series, plus a mean path cost at least twice
as high. Ratios rather than absolute thresholds, because amplitude is in the
series' units and a threshold in those units would be a number tuned to the
smoke tier. The original ">0.5 / <0.15" is reported in `docs/milestones/M3.md`
alongside it at every scale measured. (Revised: at the time this was written it
was not met at any scale; see below.)

## Consequences

- The report card carries five oscillation numbers where it carried three. That
  is the honest width of the phenomenon; a single scalar was what got this
  wrong in the first place.
- `fast_swing` is not bounded by one and not dimensionless. Comparing it across
  series with different units is meaningless, and the docstring says so. This is
  a real ergonomic cost of the change.
- Cross-model comparison now needs a *pair* of runs on one scenario and seed to
  mean anything. `compare()` and the bench front-end in M6 must never present a
  single model's amplitude as a standalone score.
- The M4 detectors — herding, context rot, identity amnesia, staleness misuse —
  inherit the warning. Any of them ported from the proposal's toy should be
  validated at `n = 1` *and* at scale before its threshold is believed.
- The negative finding is itself a result: a published oscillation metric whose
  value depends this strongly on the number of scopes and the length of the run
  is a trap for any group reusing the proposal's methodology. (Revised: the
  *inversion* originally reported here was an artefact of a second bug and is
  withdrawn; the scale-dependence is not.)

## Alternatives rejected

**Tune the fast band or the warmup until dominance clears 0.5.** This is
fitting the detector to the desired answer. The milestone spec explicitly
forbids it, and the failure is structural rather than a matter of band edges:
aperiodic movement has no peak to find at any band.

**Average dominance over links instead of taking the max.** Changes nothing.
The greedy model's power is spread across the band on every link, not
concentrated on links that the maximum happens to miss.

**Report only cost and drop the oscillation measures.** Cost separates the
models cleanly (3× here, 50× at one scope), and it is what an operator cares
about. But cost cannot distinguish a model that is uniformly mediocre from one
that is excellent half the time and catastrophic the other half, and that
distinction is the entire subject of the project.

**Subtract background from the utilisation figure as well as from the detector
series.** Rejected because the figure is meant to show what an operator would
see on their own graphs, background included. The detector needs the clean
series; the reader needs the dirty one.

**Sample faster than the background bucket so the aliasing goes away.** Does
not help, and would hurt: the decision cadence defines what "fast" means for a
control loop, so sampling off-cadence makes the fast band mean something other
than "per decision round". The aliasing is handled by measuring the model's own
footprint instead.

---

## Revision — after the cadence fix

The measurements in **Context** were taken on a series that was not uniformly
sampled, and one of the two claims made from them does not survive being
measured again.

`run_loop` assumed a 30 s decision round would always fit the model's turn. At
eight scopes it mostly does; at forty it never does, because probes are charged
per scope. Every round then ended whenever the turns happened to finish, so the
world advanced by a variable amount between samples while the FFT was told the
grid was uniform. The cadence is now calibrated from a few unsampled rounds and
held (see the loop's `_calibrate`), and with a real grid the numbers change.

Smoke tier, 240 rounds, seed 7, both models on one scenario, sweeping the number
of concurrent scopes:

| scopes | cadence | amplitude, greedy | amplitude, stochastic | ratio | dominance, greedy | dominance, stochastic |
|---|---|---|---|---|---|---|
| 4 | 30 s | 3.33 | 0.32 | 10.5x | 0.330 | 0.076 |
| 8 | 30 s | 3.25 | 0.54 | 6.0x | 0.507 | 0.148 |
| 16 | 71 s | 3.00 | 0.30 | 10.2x | 0.712 | 0.157 |
| 24 | 121 s | 4.46 | 0.24 | 18.7x | 0.848 | 0.279 |

**What is withdrawn.** Peak dominance does not rank the two models backwards.
The 0.147-against-0.200 inversion in the table above came from an unevenly
sampled series read off total utilisation, and both of those were bugs. With the
grid fixed and the detectors reading `advised_load`, dominance ranks the models
correctly at every scale measured. The sentence "the detector is blind to it"
was wrong.

**What stands, and is now the actual argument.** Amplitude is stable and
dominance is not. Across a 6x change in the number of scopes the greedy model's
amplitude moves between 3.0 and 4.5 and the ratio stays between 6x and 19x,
while its dominance climbs from 0.33 to 0.85 — through the milestone's 0.5
threshold, which is therefore a number that means different things at different
scales. Series length has the same effect: at 4 scopes, 100 rounds gives 0.375
against 0.249 (1.5x, no separation worth having) and 240 rounds gives 0.330
against 0.076 (4.3x). A criterion written against dominance is a criterion
against the run's shape as much as against the model.

That is a weaker finding than the one first recorded, and it is the one the
evidence supports. Decisions 1 to 4 are unaffected — they were the reason the
second series and the fixed reference path exist, and both of those are what
made the corrected measurement possible. Decision 5 stands with its own
justification rather than the failed threshold: ratios travel across scales,
absolute thresholds on either detector do not.

**Consequence for M4.** "Validate at n = 1 and at scale" was already the rule
here. Add to it: validate on a series you have checked is uniformly sampled. The
detectors cannot see that they are being lied to about their own x axis, and
neither could the report card — the output looked entirely plausible for as long
as the bug existed.

---

## Note — M4, after ADR 0011

The revision above fixed the grid by widening `decision_s` per row until every
round fit it. M4 fixes it by taking samples off the world's clock instead, so the
cadence is held at 30 s and rounds are allowed to overrun. Re-measuring the sweep
that way (table in [0011](0011-sampling-off-the-worlds-clock.md), and in
`docs/milestones/M3.md` beside the original) reproduces the amplitude column to
within 0.1 and the ratios to within 0.5x, and does **not** reproduce the dominance
column: dominance falls from 0.257 to 0.079 across the same sweep instead of
climbing from 0.33 to 0.85.

The climb was mostly an artefact of the fix. Widening the cadence widened the
sample interval with it — to 121 s at 24 scopes — and coarser sampling folds the
same power into fewer bins, which raises dominance for any series regardless of
the model. So the revision's sentence "its dominance climbs from 0.33 to 0.85"
should be read as a statement about sample rate, not about scale.

Decision 5 is what survives, with more support than it had: the ratio between two
models on one scenario reproduced across both mechanisms, and absolute dominance
has now been seen to move in *either* direction depending on a sampling choice the
model has no part in. The 0.5 threshold is not reached at any scale on a held
cadence.
