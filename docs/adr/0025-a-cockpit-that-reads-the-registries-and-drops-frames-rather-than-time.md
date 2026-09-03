# 0025 — a cockpit that reads the registries, and drops frames rather than time

Status: accepted
Date: 2026-09-03
Milestone: M8 (Phase 7)

## Context

`ui.py` already serves one page from the standard library, runs a job on a worker thread,
reports progress per decision round and can stop a run. It runs `demo`. What Phase 7 asks
for is different in kind: watch a *sweep*, live, while it happens, and see enough to
understand **why** a model oscillates rather than that it does.

Two invariants constrain that hard, and they are the whole design.

**Invariant 3 — the network does not wait for the model.** It must not wait for a browser
either. A live view that applies backpressure to the run is a live view that changes the
run it is watching, and every number it shows is then a number of a different experiment.

**Invariant 1 — the harness never summarises for the model.** Nothing the interface
computes may reach the model. If watching a run could change it, no live result would be
reproducible from its seed and invariant 4 goes with it.

There is a third constraint that is not an invariant but decides the shape of the code:
**add a metric and the interface shows it with no interface change; delete it and it
disappears.** Four registries already exist — `REGISTRY`, `AXES`, the probe list,
`MANDATORY_BASELINES` — and this is the phase that makes them load-bearing rather than
decorative.

## Decision

**1. A live channel out of the worker processes, which drops frames rather than time.**
Cells run in a process pool (ADR 0014, deliberately), so the channel is a `multiprocessing`
queue rather than a shared object. `LiveChannel.emit` uses `put_nowait` and **counts the
drop** when the queue is full. The run never blocks on a reader.

Every frame carries a per-cell sequence number. A reader that sees `seq` jump knows exactly
how many frames it missed, marks the gap, and **never interpolates across it**. A smooth
line drawn through a hole is a lie about a system whose entire subject is instability.

The channel is write-only from the worker and read-only from the server. `Session` has no
handle on it and neither does any model.

**2. `run_loop` gains `on_round`, and it is the only new instrumentation.** Called once per
decision round with a plain dict built from numbers the round already computed: the
simulated time, the round's decision latency, how concentrated the advisories were, what
the world's cost and deviation did. Three deltas aligned on one timeline — what the model
was shown, what it did, what the world did back — which is the lens, and it needs no new
measurement because invariant 1 already requires the raw log to exist.

**3. Metrics declare a shape, and there is one renderer per shape.** `Metric.shape` is
`"scalar"` or `"stratified"`, set at registration. It cannot be inferred: a metric returning
a `Mapping` is stratified and a metric returning a float is not, and the only way to know
which without running it is for the metric to say. A metric needing its own case in the
interface is a metric that has not declared its shape.

**4. The interface is a package, not a longer `ui.py`.** `cockpit/` holds `channel.py`,
`panels.py` and `app.py`; `ui.py` keeps serving `demo` and is unchanged. The plan says the
skeleton stays, and it does — what would not have survived is one module holding a queue
protocol, four registry readers, a configurator and a page.

**5. A partial run says what it did not run.** The configurator computes the cell count and
a time estimate *before* the run, from a measured per-cell cost per tier, and every response
carries `omitted`: the metrics, axes and models the selection left out. A narrowed suite
that renders like a full one is the dishonesty Phase 4 exists to prevent, moved into the
interface where it is easier to commit and harder to see.

**6. The raw log is one click away and never the default.** Nothing renders "the log" as a
panel. A researcher chasing something nobody anticipated needs it, and hiding it would be
dishonest about what the harness holds.

## Consequences

- A watched sweep and an unwatched one produce the same trace. That is asserted, not
  assumed: the digest of a cell run with a channel attached must equal the digest without.
- Frames are lost under load, by design, and the count is displayed. A cockpit that showed
  every frame would be one that had slowed the world down to show them.
- `Metric.shape` is a new required consideration when registering a metric. The default is
  `"scalar"`, so an unthinking registration is right for the common case and wrong for a
  stratified one — which the panel renderer surfaces immediately, because the values arrive
  as `name.key` and a scalar renderer has nowhere to put the key.
- The time estimate is a per-tier constant measured on one machine. It is labelled as such.

## Alternatives rejected

**A shared object or a callback into the parent.** Cells run in separate processes for a
measured reason (ADR 0014). A callback would either not cross the boundary or would
serialise the pool through one thread.

**A bounded queue that blocks when full.** The obvious way to lose no data, and it makes
the browser a term in the world's clock.

**Interpolating over dropped frames.** The line looks better and says something false.

**Inferring a metric's shape from one call.** It works until a metric returns a scalar on
the run it was inferred from and a mapping on the next, at which point the interface has
one renderer and two shapes with nothing saying which.

**A framework and a build step.** `core` is numpy-only and a demonstration machine is
usually one with nothing installed on it. The page is one string and some JSON endpoints,
for the same reason `ui.py` is.
