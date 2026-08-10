# 0011 — Sampling off the world's clock, and a fast band measured in rounds

Status: accepted
Date: M4
Supersedes the cadence-calibration mechanism introduced in the revision to
[0010](0010-measuring-oscillation-by-amplitude-not-periodicity.md). Decisions 1–5
of 0010 are unaffected.

## Context

M3 sampled every series once per decision round: the round finished, the loop
appended a point, the next round began. That makes the sample index and the round
index the same thing, which is convenient, and it makes the sample *interval* a
property of how long the model took, which is not.

The consequence was recorded as M3's known limitation. A round that overruns its
30 s slot does not merely land its advice late; under per-round sampling it also
stretches the gap to the next sample. The world then advances by a variable
amount between points while every detector in `instrument/` is told the grid is
uniform — and none of them can tell otherwise. A series is the right length and
the numbers are the right order of magnitude either way. ADR 0010's revision
exists because that happened once already and the report card looked fine
throughout.

M3's answer was to calibrate: run a few throwaway rounds, measure what a round
actually costs, widen `decision_s` to fit, and hold it. That does produce a
uniform grid. It produces it by letting the model's speed choose the scenario's
cadence, which is the wrong direction of causation for this harness. Invariant 3
says the network does not wait for the model; a harness that slows the world's
decision clock until the model can keep up is the same concession with an extra
step. It also silently converts a finding — *this model cannot decide inside
30 s at 24 scopes* — into a configuration change, so the finding disappears from
the output.

The two problems are separable, and M3 conflated them because per-round sampling
made them one problem.

## Decision

**1. Samples are taken by the world, not by the loop.** `instrument/sampler.py`
adds a `Sampler` that attaches to `Substrate.taps` and records a point whenever
simulated time crosses the next multiple of `interval_s`. The model's turn cannot
move a grid point, because the model is not what puts them there.

**2. The grid is anchored to absolute simulated time, not to attach time.** Grid
points are at `k * interval_s` from the start of the world. Anchoring to the
moment the sampler attached was implemented first and was wrong in a way worth
recording: `session.call("subscribe", ...)` charges time, so the sampler attached
at t ≈ 0.13 s, every grid point landed at `0.13 + 30k`, and — because
`interval_s` was a whole multiple of `step_s` but the *offset* was not — no grid
point ever coincided with a tick. The samples came out at 30.13, 60.90, 91.00,
with 0.98 s of jitter. A fix aimed at grid uniformity had produced a
non-uniform grid, and only an explicit `uniform()` assertion caught it.

**3. A grid point crossed without a tick is counted, never interpolated.**
`Series.skipped` records it and `Series.uniform()` then returns false. Anything
can advance the clock by more than one interval — `clock.advance_to`, a large
scheduled event — and inventing a value for the missing point would restore
uniformity by fabricating the measurement. The alternative, refusing to run, is
worse: the run is still valid, it is only the spectra that are not.

**4. `sample_s` defaults to the decision cadence.** With no configuration, a run
is sampled once per round exactly as in M3, so every threshold recorded in
`docs/milestones/` is measured against the same axis it was written for. What
changes is that the property now holds *by construction* rather than by the model
happening to be fast enough.

**5. Sampling slower than the cadence is refused.** The model can change its
advice once per round; a grid coarser than that is below Nyquist for the fastest
thing being measured, and the resulting number is not a weaker measurement of
oscillation but a different quantity. `run_loop` raises rather than reporting it.

**6. The fast band is defined in rounds and converted per run.** `band_min` and
`warmup_samples` in `instrument/detectors.py` take `(sample_s, decision_s)` and
return the band edge in cycles-per-sample and the warmup in samples.
`FAST_BAND_MAX_ROUNDS = 12.5` is the reciprocal of M3's `FAST_BAND_MIN = 0.08`,
so a per-round series is measured identically; an off-cadence series is measured
over the same span of *world*, rather than over a fraction of it. Oscillation is
a property of a control loop relative to its own cadence — the pathology is
"flaps every other decision", not "flaps every 60 seconds" — so rounds are the
units the constant belonged in all along.

**7. `adaptive_cadence` is off by default and kept as a comparison.** It stays
because "the same model at a cadence it can keep" is a legitimate controlled
experiment, and because deleting it would delete the mechanism behind ADR 0010's
revision table. It is no longer the default, and it is no longer described as
protecting the grid.

## Evidence

All three tables below are produced by `docs/evidence/m4-sampling/sweep.py` and
committed as `docs/evidence/m4-sampling/sweep.log`; the demo run is
`docs/evidence/m4-smoke/`.

**The failure case, fixed.** Smoke tier, seed 7, 24 concurrent scopes, 12 rounds
at a held 30 s cadence — the configuration in which M3 could not keep a grid.
Every one of the 12 rounds overruns; the run takes 870 s of world time for
12 rounds, a mean of 72.5 s per round against a nominal 30 s. The sampler
produces 29 samples, first at 30 s, last at 870 s, every gap exactly 30 s,
jitter 0.0, 0 skipped. Under M3's axis the same run would have yielded 12 points
whose mean spacing was 72.5 s, labelled as 30 s, with unequal gaps.
`tests/test_loop.py::test_the_grid_survives_rounds_that_do_not_fit_the_cadence`
pins it.

**Sample rate does not change what is measured.** Smoke tier, seed 7, 8 scopes,
240 rounds at 30 s, both reference models on one scenario:

| `sample_s` | samples | grid uniform | amplitude, greedy | amplitude, stochastic | ratio | dominance, greedy | dominance, stochastic |
|---|---|---|---|---|---|---|---|
| default (per round) | 240 | yes | 3.300 | 0.546 | 6.05x | 0.297 | 0.206 |
| 3 s | 2400 | yes | 3.467 | 0.546 | 6.35x | 0.217 | 0.137 |
| 6 s | 1200 | yes | 3.435 | 0.553 | 6.22x | 0.216 | 0.141 |
| 10 s | 720 | yes | 3.337 | 0.548 | 6.09x | 0.215 | 0.122 |
| 15 s | 480 | yes | 3.312 | 0.542 | 6.11x | 0.222 | 0.152 |
| 30 s | 240 | yes | 3.300 | 0.546 | 6.05x | 0.297 | 0.206 |

A 10x change in sample rate moves the headline amplitude by 5% and the ratio
between the models by 5%. That invariance is the point of decision 6: without
the conversion, the same runs would report amplitudes differing by an order of
magnitude, because the band edge in cycles-per-sample would have meant a
different number of seconds in every row. The 30 s row reproducing the default
row exactly is the check that decision 4 is true.

Dominance is not invariant even here — it drops from 0.297 to about 0.22 as soon
as the series is oversampled, because the extra bins hold what the world was
doing between the model's turns and dilute the peak. Consistent with ADR 0010:
amplitude travels, dominance does not.

**Undersampling, which is why decision 5 refuses it.** Taking every *n*th point
of the uniform 30 s grid is exactly a uniform coarser grid, so the refused
configuration can still be measured, by decimating a run the loop did perform:

| effective rate | samples | amplitude, greedy | amplitude, stochastic | ratio | dominance, greedy | dominance, stochastic |
|---|---|---|---|---|---|---|
| 30 s (kept) | 240 | 3.300 | 0.546 | 6.05x | 0.297 | 0.206 |
| 60 s (decimated) | 120 | 2.791 | 0.509 | 5.48x | 0.357 | 0.227 |
| 120 s (decimated) | 60 | 1.613 | 0.341 | 4.72x | 0.554 | 0.459 |

Undersampling by 4x loses half the amplitude (3.300 to 1.613) and erodes the
ratio the M3 criterion is written against from 6.05x to 4.72x, toward its 4.0x
threshold.
Dominance moves the other way and rises for *both* models, to 0.554 against
0.459 — a calm model looks periodic once its power is folded into a quarter as
many bins. A criterion read off that row would pass the greedy model on a
threshold of 0.5 for the wrong reason.

**ADR 0010's scope sweep, re-measured on a held cadence.** Same scopes, seed,
rounds and models as the revision table in 0010; the difference is that
`decision_s` stays at 30 s instead of being widened per row, and the samples come
off the clock:

| scopes | cadence | overruns | amplitude, greedy | amplitude, stochastic | ratio | dominance, greedy | dominance, stochastic |
|---|---|---|---|---|---|---|---|
| 4 | 30 s | 0/240 | 3.37 | 0.33 | 10.3x | 0.257 | 0.081 |
| 8 | 30 s | 2/240 | 3.30 | 0.55 | 6.0x | 0.297 | 0.206 |
| 16 | 30 s | 240/240 | 2.82 | 0.26 | 10.8x | 0.103 | 0.067 |
| 24 | 30 s | 240/240 | 4.30 | 0.22 | 19.2x | 0.079 | 0.067 |

Every grid is uniform, including the two rows where every single round overran.
The amplitude column reproduces 0010's revision table to within 0.1 and the
ratios to within 0.5x (10.5/6.0/10.2/18.7 there, 10.3/6.0/10.8/19.2 here), which
is the check that widening the cadence was never what made the amplitude
measurement work.

The dominance column does not reproduce, and it fails in the opposite direction
to the one recorded. 0010's revision reported dominance *climbing* from 0.33 to
0.85 as scopes rose, through the milestone's 0.5 threshold. On a held cadence it
*falls*, from 0.257 to 0.079, and at 16 and 24 scopes it barely separates the
models at all (0.103 against 0.067; 0.079 against 0.067). The mechanism is the
undersampling table above: with `adaptive_cadence` on, the 24-scope row was
sampled once per 121 s rather than once per 30 s, and folding the same power into
a quarter as many bins raises dominance for any series. What 0010 read as
scale-dependence was substantially rate-dependence.

This makes 0010's Decision 5 stronger rather than weaker — its conclusion was
that ratios travel across conditions and absolute dominance thresholds do not,
and dominance has now been observed to move in both directions depending on a
sampling choice that has nothing to do with the model. The 0.5 threshold is
unreachable on a fixed grid at every scale measured here, which is recorded in
`docs/milestones/M3.md` next to the old numbers rather than in place of them.

## Consequences

- `LoopResult` no longer stores one dict of series per quantity; it holds one
  `Series` and exposes the old names as read-only properties. Anything that wrote
  to `result.utilisation[...]` breaks, deliberately: the series belong to the
  sampler now.
- The report card gains `sample_s`, `samples` and `grid_jitter_s`, and
  `grid_uniform` becomes a measurement rather than an inference from overruns.
  The demo's grid criterion asks the sampler; overruns are reported next to it
  with no pass mark attached, because a model too slow for its cadence is a
  finding about the model and not a defect in the run.
- `sample_s` must be a whole multiple of `scenario.step_s` and this is enforced
  in the constructor. It is a real restriction — 7.5 s is not available at
  `step_s = 1.0` — and it is what makes "every grid point is a tick boundary"
  checkable rather than hopeful.
- Series are now longer than the number of rounds whenever `sample_s` is smaller
  than the cadence. Anything that indexed a series by round number is wrong; use
  `Series.times`.
- The trace M4 still has to write (`instrument/events.py`) inherits the grid
  question. Events are timestamped and irregular by nature, so the trace must
  record the sampler's `interval_s` and its `skipped` count, or a detector run
  over a saved trace will be unable to answer what a live one can.
- ADR 0010's revision table was measured with `adaptive_cadence` on and therefore
  at a different cadence per row. It is left in place, and the same sweep on a
  held cadence is recorded in `docs/milestones/M3.md` beside it.

## Alternatives rejected

**Keep per-round sampling and reject runs with overruns.** This is what the M3
report card effectively did, and it makes the harness unable to measure the
models it most needs to measure. A model that cannot decide inside its slot is
the interesting case, not the invalid one.

**Interpolate onto a uniform grid after the fact.** Restores `uniform()` and the
FFT's precondition while inventing the values the precondition exists to
protect. Linear interpolation across a 70 s gap in a series that swings by three
times its capacity per round is not a smoothing, it is a fabrication, and it
would be invisible in the output.

**Resample by taking the world's tick series and decimating.** Equivalent for
utilisation, and it was used above to measure the undersampled case. Not
equivalent in general: `path_share` and `mean_cost_ms` are derived from state
that would have to be reconstructed per tick, and storing a point per tick at
`step_s = 1.0` over a 7200 s episode is 7200 points per tracked series where the
grid needs 240. The tap costs one function call per tick and stores only what is
due.

**Define the band in seconds instead of rounds.** Then a run at a 60 s cadence
and a run at 30 s would be scored against the same absolute frequency, and the
slower model would be credited for oscillation it is physically incapable of.
The cadence is what "fast" means for a control loop.

**Let the sampler interpolate the first partial interval so that `times[0] == 0`.**
Rejected for the same reason as decision 3, and it would misrepresent a run's
opening state as a measurement. The first sample is at the first whole interval;
a run shorter than one interval yields an empty series, and `uniform()` on an
empty series is vacuously true.
