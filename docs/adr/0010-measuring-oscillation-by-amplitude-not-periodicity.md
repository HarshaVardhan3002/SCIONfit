# 0010 — Measuring oscillation by amplitude, not periodicity

Status: accepted
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

**5. The M3 criterion is restated, and the original is recorded as failed.**
The criterion becomes a ratio between models on the same scenario and seed —
greedy's fast-band amplitude at least four times the stochastic model's, on both
the advised-load and realised-split series, plus a mean path cost at least twice
as high. Ratios rather than absolute thresholds, because amplitude is in the
series' units and a threshold in those units would be a number tuned to the
smoke tier. The original ">0.5 / <0.15" is reported in `docs/milestones/M3.md`
as not met, with these figures.

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
- The negative finding is itself a result, and arguably the most useful one in
  M3: a published oscillation metric that inverts under multi-scope contention
  is a trap that any group reusing the proposal's methodology would fall into.

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
