# 15. The mechanism ladder is six knobs on the host population, not four host classes

Date: 2026-09-02

## Status

Accepted. First half of M6 (Phase 2). The sweep engine in ADR 0016 sweeps these.

## Context

The benchmark's mechanism ladder has four rungs: point rankings, then intervals,
then **selector discipline**, then the **jittered mirror**. Rungs 1 and 2 exist --
a model publishes a distribution and the population samples it. Rungs 3 and 4 do
not, and rung 3 is the one that decides whether the benchmark measures anything a
deployment would recognise.

The gap is what a real selector does between being told and moving. It does not
resample a distribution from scratch every time it reconsiders. It keeps a short
list of candidates it is willing to consider, it refuses to move for a small
improvement, it will not move twice in quick succession, and it does not
reconsider at the same instant as every other selector. A population without
those four properties is maximally responsive, which flatters an unstable model:
its advice is followed instantly and completely, so oscillation appears wherever
the advice oscillates and nowhere else. Add discipline and the population becomes
a low-pass filter with memory, and a model that flaps can be damped by the
network -- or can drive it into a slower, larger oscillation than the one it is
publishing. Which of those happens is a result, and right now it cannot be asked
for.

The fourth rung is the same argument about *arrival* rather than about response:
an advisory currently lands on every host of a scope in the same instant. A real
mirror reaches its readers over a window.

Two constraints shape the design.

**Hosts are counts, not objects** (ADR 0009). Ten thousand hosts in a scope is one
`multinomial` draw over a path-count array. Nothing here may introduce a Python
object per host, so every rung has to be expressible as an operator on count
arrays. That is a real constraint and it is why the design below is stated in
terms of transition mass rather than in terms of what "a host" does.

**The defaults must reproduce today, exactly.** Invariant 4 is determinism from a
seed, and the M3 and M4 results in `docs/evidence/` were measured against the
current population. A new knob that shifts the default draw invalidates them
silently.

## Decision

Six fields on `HostParams`, each defaulting to the behaviour that exists now.

| field | default | rung | what it does |
|---|---|---|---|
| `k_paths` | `None` | 3 | a selector considers at most the top *k* advised paths |
| `eps_set` | `0.0` | 3 | ...and only those within a factor of the best |
| `hysteresis` | `0.0` | 3 | switch only if the candidate beats the incumbent by this margin |
| `dwell_s` | `0.0` | 3 | having switched, do not switch again for this long |
| `timer_jitter` | `1.0` | 3 | 1.0 = independent timers, 0.0 = the whole scope reconsiders together |
| `mirror_jitter_s` | `0.0` | 4 | seconds over which a new advisory reaches the population |

**`k_paths` and `eps_set` are the same operation** -- a mask on the advised
distribution, renormalised -- and they are also **paths-per-selector**, the axis
the plan listed as missing. That is not a coincidence being exploited: a selector
that will place traffic on at most *k* paths is exactly a population sampling a
distribution truncated to *k* paths. Truncation is applied to the *advisory*, not
to the path set, so `stale_weight` and the path arrays are untouched and the
mass a selector refuses to use shows up in `deviation()` -- where it belongs,
because the gap between what the model asked for and what the population did is
now partly the population's discipline and the report has to be able to say so.

**`hysteresis` is a threshold rule on the advised weight**, and it is exact rather
than sampled per host. A mover currently on path *i* accepts a destination *j*
only if `w[j] > w[i] + hysteresis`. The acceptable set for *i* is therefore a
prefix of the paths sorted by descending weight, so the whole transition is
computed from one sort and one prefix sum: source paths are grouped by how long
their acceptable prefix is, and each group draws one multinomial over that
prefix. Mass that does not accept stays where it is. `O(k log k)` plus one draw
per distinct prefix length, rather than the `k x k` acceptance matrix the obvious
implementation builds -- which at 300 paths and 100 scopes is 9 M entries per
step and does not fit the budget.

**`dwell_s` is a ledger, not a per-host timestamp.** The population records how
many hosts moved in each of the last `ceil(dwell_s / dt)` steps, and that many are
ineligible to move now. For a count model this is exact: the number locked is
exactly the number that moved inside the window. Which *individuals* they are is
not represented, and does not need to be -- every host in a scope is exchangeable
by construction.

**`timer_jitter` interpolates between two population behaviours that already
bracket the truth.** Today a host reconsiders with probability `dt / resample_s`
each step, independently, which is a fully jittered exponential timer -- so today
is `timer_jitter = 1.0`. At `0.0` the scope reconsiders as one body when the clock
crosses a multiple of `resample_s`, which is the synchronised herd. In between,
that fraction of the population is on independent timers and the rest moves
together. The synchronised end is not a strawman: it is what a fleet restarted
by one deploy looks like, and a model that is stable against jittered selectors
and unstable against synchronised ones has found a real edge.

**`mirror_jitter_s` blends rather than switches.** A published advisory does not
replace `intended`; it becomes the new target while the previous one is retained,
and the distribution the population samples is the mix, weighted by how far
through the window the clock is. At `0.0` the mix is the new advisory
immediately, which is today.

## Consequences

`deviation()` stops being purely sampling noise the moment any rung-3 knob is
non-zero, and the scoring has to know which it got. `ScopeState.to_dict` gains
`locked` and `mirror_frac` so that a result file records what the population was
doing, not only what the model asked for. A run that reports a large deviation
with `k_paths=1` has not found a badly behaved model.

The 1/sqrt(N) concentration criterion applies to the *effective* distribution --
the truncated, blended one the population actually sampled -- not to the advisory.
`concentration_bound` therefore has to be given the effective distribution, and
`ScopeState.effective()` exists to hand it over. A test that fed it `intended`
would fail for a correct implementation, which is the trap this paragraph exists
to mark.

The cost of the default path is unchanged: every rung is behind a branch that a
default `HostParams` does not take, so `substrate_step_s` does not move. The cost
with hysteresis on is a sort plus a small number of multinomials per scope per
step, which is real and is the sweep's to pay.

Rungs 1 and 2 are not represented here because they are properties of the *model*
-- whether it publishes point rankings or intervals -- and belong to
`Capabilities`, which already carries `distributional`. The ladder is
model-side for its bottom half and population-side for its top half, and that
asymmetry is in the spec rather than introduced here.

## Alternatives rejected

- **Four host classes with an object per host.** The obvious implementation, and
  it violates ADR 0009's scale decision. Ten thousand hosts per scope times a
  hundred scopes is a million objects stepped at 1 Hz.
- **A per-host switch timestamp array** instead of the dwell ledger. Exact in the
  same way, `O(n_hosts)` memory instead of `O(dwell_s / dt)`, and it makes hosts
  distinguishable -- which then invites code that treats them as such and
  quietly reintroduces the object-per-host cost.
- **Sampling hysteresis per mover** (draw a candidate, accept or reject). Simple
  and correct in expectation, and it needs a draw per host rather than per group,
  which is the loop ADR 0009 exists to avoid.
- **Truncating the path set rather than the advisory** for `k_paths`. Cheaper,
  and it destroys the measurement: the paths a selector declines to use would
  vanish from `path_ids`, `stale_weight` would move for a reason that has nothing
  to do with re-signing, and the deviation would read zero because the advisory
  would have been renormalised onto what the population was willing to do.
- **Putting the jittered mirror on the clock** as a per-host `advisory_apply`
  event. It is where the *decision* delay already lives (ADR 0009), so it looks
  right -- but decision delay is one event per scope and this would be one per
  host per publication, which is the object-per-host cost wearing a different hat.
