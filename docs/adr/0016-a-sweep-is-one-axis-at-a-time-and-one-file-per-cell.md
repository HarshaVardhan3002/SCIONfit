# 16. A sweep varies one axis at a time, and writes one file per cell

Date: 2026-09-02

## Status

Accepted. Second half of M6 (Phase 2). Depends on ADR 0015 for the axes it
sweeps.

## Context

The benchmark's claim is that two machines running the same suite produce the
same scores, and that those scores are comparable across models. Everything
below follows from taking that literally.

Six axes are in scope: population size, defector fraction, the mechanism ladder,
paths per selector, staleness, and the probe-limit regime. Their full cartesian
product is 1,458 cells before any model or repeat is counted, and at the
`realistic` tier a cell is minutes. Nobody would run it, so a sweep that only
offers the grid is a sweep that gets run at the `smoke` tier and quoted as
though it were the real one.

Two further things had to be settled before any of it could be written down.

**Staleness was half-missing.** The plan called it "information delay, not
decision delay", and only decision delay existed: `extra_latency_s` makes the
*model* slow. A model can be infinitely fast and still be deciding about a world
it last saw thirty seconds ago, and that is the axis §28 asks for.

**Cells fail.** A model raises, a budget starves, a scenario turns out to have no
multi-path scope. A sweep that dies on the first of those has wasted an hour of
everything that ran before it.

## Decision

**One axis at a time, by default.** A suite names a baseline value per axis; the
default sweep is the baseline cell plus, for each axis, every other value on that
axis with the rest held at baseline. Sixteen cells rather than 1,458, and each
one answers a question of the form "what does this axis do to this model", which
is the question the report is going to ask anyway. `mode="grid"` runs the full
cartesian product for someone who wants an interaction, and says in its help
what it costs. This is the standard one-factor-at-a-time design and it has the
standard limitation -- it does not see interactions -- which is why the grid is
offered rather than removed.

**One JSON file per cell, named by a digest of what produced it.** The file
carries the seed, the axis values, the model spec, the suite digest, the
substrate digest and the whole metric set. Three properties fall out:

- *Resumable.* A cell whose file already exists, and whose recorded suite digest
  matches the suite being run, is skipped. Interruption costs at most one cell.
- *Comparable.* The suite digest is over the axes and their values, so a suite
  that has been edited does not silently reuse results from the suite before the
  edit. It is a mismatch, and the cell is re-run.
- *Mergeable.* Two machines that ran half a suite each have a directory that is
  the union, with no merge step, because a cell's identity is a function of the
  cell and not of the run.

**Every cell's seed is derived from the cell, not from a counter.** A counter
makes a seed a function of execution order, and execution order under a process
pool is not deterministic. The seed is a hash of `(suite, model spec, axis
values, repeat)`, so a cell run alone and a cell run in the middle of a grid draw
the same world.

**Cells run in a process pool, and a failed cell is a recorded result.** The
worker loads the model from its spec inside the child, which is also how the
result of each cell is kept out of the process the previous cell aged (ADR 0014
found that the hard way). A cell that raises is written with `error` set and the
sweep continues; the report can then say which cells are missing and why, which
is a finding about the model rather than a hole in the data.

**The five mandatory baselines are always run.** §28 requires accuracy to be
reported against persistence, Tier-0-only, static-only, latest-sample and EWMA,
and the requirement is worth nothing if it can be turned off. They are appended
to whatever models were asked for, deduplicated, and marked `mandatory` in their
result files so a report can render them as the floor rather than as peers. Two
already existed -- EWMA is `EMAOracle` and static-only is `CapacityProportional`
-- and three are added in `reference/baselines.py`.

`Tier0Only` is an interpretation and is marked as one. §19.1 defines Tier-0 as
"online robust filters, always-on... the availability floor", per-unit, with no
spatial structure and no forecast head, servable with inflated intervals. It is
implemented as a rolling median per interface with an interquartile spread, and
carries `# ASSUMPTION(Q7)` because "which robust filter" is not something the
spec pins down and the choice moves the number it is a baseline for.

## Consequences

The default suite is cheap enough to run on every change and too coarse to
publish an interaction from. That is the intended trade and the report has to
say which mode produced it, so `mode` is in the result file and in the suite
digest.

A result file is the unit of everything downstream. Phase 3's metric registry
adds keys to `metrics`; Phase 4's report reads a directory. Neither needs the
sweep to have been run in one go, on one machine, or by one person.

Rerunning a suite after changing `core/` silently reuses nothing, because the
substrate digest is recorded -- but it is recorded rather than checked, and a
mismatch is reported by the reader instead of forcing a re-run. Forcing would
mean a substrate change invalidates a week of `stress`-tier results that may
still be exactly what someone wants to compare against.

The one-at-a-time default means the baseline cell is run once and every axis is
measured against it. If the baseline cell is unrepresentative, every reading is
unrepresentative in the same direction. The baseline is therefore part of the
suite definition and shows up in the digest, rather than being a default buried
in the runner.

## Alternatives rejected

- **Full cartesian only.** 1,458 cells before models. It would be run at the
  smoke tier and quoted as the real thing.
- **A single results file appended to as cells finish.** Needs a lock under a
  process pool, is not mergeable across machines, and an interrupted write
  corrupts every result rather than one.
- **A counter for seeds, or a single seed for the whole suite.** Both make a
  cell's world depend on how the sweep was scheduled. The second also makes
  every cell in a sweep correlated, which is worse than it sounds when the axis
  under test is population size.
- **Threads instead of processes.** The loop is Python-bound and the GIL would
  serialise it, and threads would reintroduce exactly the shared-process
  measurement contamination ADR 0014 removed.
- **Skipping the mandatory baselines when the user names their own models.**
  The requirement exists because "our model scored 0.31" means nothing without
  the floor, and a floor that is optional is absent on the runs that most want
  to omit it.
