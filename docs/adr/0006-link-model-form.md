# 6. The load-to-metrics form

Date: 2026-07-29

## Status

Accepted. Required by M1 deliverable 3, which says the exact form gets an ADR.

## Context

`core/linkstate.py` turns offered load into latency, loss and available
bandwidth. Probe R7 asks whether a model has learned that cost rises with load,
so the substrate must have that property exactly: **cost non-decreasing in
offered load**, no exceptions, or the probe measures nothing and a model can
pass it by being wrong in the same direction the world is.

Beyond monotonicity the form is a modelling choice. Options considered: a
queueing formula from first principles (M/M/1 or M/D/1), the BPR function from
transport engineering, a piecewise-linear table, or a learned form fitted to
traces we do not have yet.

## Decision

**BPR plus an explicit queueing tail, per direction, with every constant in a
frozen dataclass.**

```
u        = clip(offered / (capacity * health), 0, 0.995)
latency  = free_flow * (1 + alpha * u**beta) + queue_ms * u**q / (1 - u)
loss     = base_loss + max_loss * clip((u - onset) / (1 - onset), 0, 1)**gamma
available= max(usable_capacity - offered, 0)
cost     = latency + loss_penalty_ms * loss
```

Monotonicity is structural, not tested-in: every term is a non-negative power of
a utilisation that only rises with load, over a denominator that only shrinks.
The property test over 10,000 random states exists to catch a future edit that
breaks this, not to establish it.

**Why BPR and not a queueing formula.** M/M/1 gives `1/(1-u)`, which is
principled about a single queue and wrong about a link: it ignores that
operators provision against a target utilisation, and it has no free parameters
to fit when tier-0 traces arrive. BPR is the standard congestion form in
transport, it is convex, it has two parameters that mean something, and it is
easy to fit by least squares — `fit_bpr` does exactly that and is the M9
calibration hook.

**Why the extra tail.** BPR alone is too gentle near capacity. It was built for
roads, and roads do not have buffers: it does not reproduce the sharp latency
knee a full link shows. The `queue_ms * u**q / (1-u)` term supplies the knee and
is separately tunable, so calibration can move the knee without disturbing the
mid-range fit.

**Utilisation is clipped at 0.995.** At 1.0 the tail divides by zero, and past
it the form has no meaning: a link offered twice its capacity is not twice as
slow, it is dropping packets, which the loss term already handles.

**Zero usable capacity means utilisation at the ceiling, not zero.** A dead link
has to be the worst link on the list. Dividing by zero and clipping the
resulting nan to 0.0 would make a failed link look like the fastest path
available, and every downstream measurement would be built on it.

**Background traffic is exogenous, diurnal, weekly, seeded, and unreadable.** It
is folded into offered load before any metric is computed and is never exposed.
Per-link diurnal phases are drawn from a narrow spread around a common phase
rather than uniformly over the day: uniform phases cancel in aggregate, leaving
the network with no time of day at all, which is both unrealistic and
convenient in the wrong direction.

The background draw is **counter-based on `(seed, bucket)`**, not a running
generator. A stateful RNG would make the world depend on how often it was
stepped, and invariant 4 would hold only between runs with identical step
sequences.

**Metric arrays are memoised and invalidated on any change to load, health or
time.** Composition asks for them once per scope; recomputing 40,000 directions
per scope per step is twenty times the step budget while looking like the same
code. Cached arrays are marked read-only so a caller cannot mutate shared state.

## Consequences

- R7 has a substrate whose behaviour it can legitimately test against.
- Absolute latency numbers are indicative until M9 calibration; only the shape
  is load-bearing. Marked `ASSUMPTION(Q2)` in the source.
- Six constants now describe congestion. They belong in the scenario schema, or
  two runs described as identical will not be.
- `cost()` collapses metrics to one number. It is deliberately not what a model
  is shown — invariant 1 forbids summarising on the model's behalf — and exists
  so the monotonicity property is a statement about something and scenario code
  has a scalar to sort on.

## Alternatives rejected

**M/M/1 or M/D/1 from first principles.** Principled about a queue, wrong about
a provisioned link, and nothing to calibrate.

**Piecewise-linear table per link.** Maximally flexible, monotone by
construction if the table is, and impossible to fit from sparse traces without
overfitting. Also makes every scenario carry a table per link.

**Fit the form itself to tier-0 traces later.** That is what the parameters are
for. Changing the functional form after M3 would move every published number,
so the shape is fixed now and the constants are left free.
