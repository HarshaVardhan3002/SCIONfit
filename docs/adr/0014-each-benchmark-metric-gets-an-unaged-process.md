# 14. Each benchmark metric is measured in a process the previous one has not aged

Date: 2026-09-02

## Status

Accepted. Supersedes nothing; changes how every number in `benchmarks/baseline.json`
is produced, so the baseline is re-recorded in the same commit.

## Context

The performance gate went red on `realistic.substrate_step_s` -- 4.5 ms against a
2.09 ms baseline, a reported 145% regression -- on a branch that changes no file
under `core/`. Stashing the branch reproduced it. The regression was not in the
code the gate exists to protect.

What it actually is: after a few million segment re-signings, everything in the
process is about twice as slow, and stays that way. A *freshly built* world run
for 600 simulated seconds costs 2.1 ms per step in a young process and 4.5 ms per
step in an aged one. Same code, same world, same seed. Ruled out by measurement
rather than by argument -- the re-signing accumulator (draining every step changes
nothing), garbage collection (`gc.disable()` changes nothing), `Segment.__dict__`
(`slots=True` changes nothing), CPU downclocking (45 s of idle does not recover
it, while a new process is immediately fast), and simulation state (work per step
is flat: 600 `_resign_all` calls and 1.64 M re-signings in every 600 s window,
with `n_segments` never moving). The evidence script is
`docs/evidence/substrate_step_process_ageing.py`.

`measure_tier` measured all six metrics in one process, in a fixed order, with
`substrate_step_s` **last** -- after four allocation-heavy benchmarks. B1's
corrected 5 s beacon interval raised the churn those earlier benchmarks produce
by sixty fold. So the gated number was partly a function of how much allocation
had happened earlier in the same process, which is not a property of the code
under test.

That is a measurement bug of the same family as the four in M0, and it is worse
than the ones it hides, because it is *stable*: it reproduces, so it reads as a
real regression, and the obvious response -- re-record the baseline -- writes the
contamination into the file the gate compares against.

Three ways out were considered.

**Widen the tolerance.** Rejected outright. The observed contamination is 145%
and the gate exists to catch a hundredfold. A tolerance that absorbs 145% cannot
resolve anything the gate was built for, and a gate that passes by measuring
nothing is worse than a red one, because nobody re-examines it.

**Fix the ageing.** Attractive, and out of reach: five candidate causes were
measured and eliminated, which leaves allocator fragmentation or something below
the interpreter. Chasing it means chasing CPython's allocator on one machine, and
the payoff is a faster benchmark harness, not a faster substrate -- a real run
builds one world and steps it, which is the young-process case already.

**Stop letting one metric's process state reach the next.** Chosen.

## Decision

Each metric is measured in its own subprocess, which does its own setup and
measures one number. `measure_tier` becomes an orchestrator: it spawns
`python benchmarks/run.py --measure-one <metric> --tier <tier>` per metric and
merges the JSON each child prints.

Consequences of that, spelled out because they are the whole point:

- **Setup is no longer shared.** Each child rebuilds the topology and the segment
  store it needs. That is slower in wall-clock and is the price of the property.
  Setup is not timed, so it costs the suite time and costs the numbers nothing.
- **Measurement order stops being a hidden input.** Adding a metric, or reordering
  the ones that exist, can no longer move an unrelated number. Before this, adding
  an allocation-heavy benchmark anywhere above `substrate_step_s` was a silent
  regression in `substrate_step_s`.
- **Repeats stay inside one child.** Timings are the minimum of the repeats, and
  ageing only ever makes a repeat slower, so the minimum is the least-aged
  measurement. One process per metric is enough; one per repeat buys nothing.
- **`--in-process` keeps the old behaviour** for a quick local look, and says in
  its help that the numbers it produces are not comparable to the baseline.

The baseline is re-recorded in the same commit, on the interpreter the gate is
pinned to, and gains one metric (`n_segments_after_50_scopes`) for the reason
below. `realistic.substrate_step_s` goes *up*, from 2.09 ms to 3.4 ms, which is
not what removing a contamination was expected to do -- see the next section.

## What isolating it then exposed

Two things, neither of which the diagnosis predicted, both found by measuring the
*old* core through the *new* harness. `f68cc3d` -- the commit whose `--update`
wrote the numbers the gate compares against -- was checked out into a worktree and
measured with `PYTHONPATH` pointing at its `src/`.

**There is no regression between that commit and HEAD.** Isolated, its
`realistic.substrate_step_s` is 3.45 ms and HEAD's is 3.42 ms. In one process, its
is 3.94 ms and HEAD's is 4.48 ms. The whole reported 145% is measurement: about
15-30% of it is the process ageing this ADR removes, and the rest is that

**the recorded 2.09 ms is not reproducible at its own commit.** Same machine, same
interpreter, same numpy, same `duration_s`, same code -- `benchmarks/run.py` is
byte-identical between `f68cc3d` and HEAD -- and the calibration says this machine
is now *faster*, so the scaled expectation is 1.84 ms and the honest measurement
is nearly twice that whichever way it is taken. Why that value was written cannot
be recovered from the repository; the profiling in that commit's message reports
2.09 ms from an ad-hoc script, and the likeliest reading is that the number in the
file came from a shorter world than the suite builds. It is recorded here as
unexplained rather than guessed at. What matters for the decision is that the
gate was comparing against a number no run reproduces, and re-recording is
therefore a correction and not a capitulation.

**`n_segments` had the same disease as the timings.** A store materialises path
sets lazily per scope, so its count grows as it is queried, and the old suite read
it at the end of the tier off the store that `link_metrics_batch`'s setup had
already queried fifty times. The file says 18,327; the count at rest is 15,002.
Nothing recorded which of the two it was, and adding a query anywhere above it
moved it silently. Both numbers are now recorded, under names that say which is
which: `n_segments` at rest, and `n_segments_after_50_scopes`.

That third one is why the fix is worth more than the red gate it clears. The
ageing was visible because it was large. This was not visible at all.

## Consequences

The suite takes longer: an interpreter start and a topology or segment-store
build per metric, roughly a second per metric at `realistic`. For a gate that
runs once per CI job this is not a cost worth optimising, and the alternative is
a number that means less.

A child that fails takes the run down with its stderr attached, rather than
silently contributing a zero. `run_isolated` raises on a non-zero exit or on
output that is not the one JSON line it expects.

Comparisons against baselines recorded before this ADR are meaningless, in both
directions. There is no migration path and no attempt at one: `schema` stays at
2 because the file's *shape* is unchanged, and the honest signal is
`recorded_on`, which the re-record rewrites anyway.

The tolerance stays at 25%, unchanged, for the reason it was set to 25% -- the
normaliser's own measured error. This ADR removes a source of error; it does not
license spending the removal on a looser gate.

## Alternatives rejected

- **Widen `TOLERANCE` to cover it.** See above. The gate would then pass on the
  hundredfold regression it was modelled on.
- **Measure `substrate_step_s` first instead of last.** Fixes this one number by
  moving the contamination onto the other five, and re-introduces itself the next
  time someone adds a metric or changes an order. The problem is the shared
  process, not the position in it.
- **Re-run the whole suite once per metric in-process, discarding all but one
  number.** Same isolation property, without the interpreter start -- except it
  does not have the property, because the discarded measurements age the process
  exactly as much as the kept ones did.
- **Force a fresh allocator arena between metrics** (`gc.collect()`, dropping
  every reference, `malloc_trim` where it exists). Measured: `gc.collect()` and
  draining every accumulator recover nothing, and the rest is not reachable from
  Python without leaving what CPython guarantees.
