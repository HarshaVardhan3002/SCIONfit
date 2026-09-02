# 0021 — A decision costs what it takes to make

## Context

Invariant 3 is the reason this project is a harness rather than a simulator:

> **The network does not wait for the model.** Wall-clock is simulated but real. A
> decision that takes 800 ms is applied 800 ms late, against a network that
> moved. Never pause the world while the model thinks.

It is not implemented. Simulated time advances from exactly two places:
`Session._advance`, called with a tool's modelled `cost.wall_clock_s`, and
`Session.advance`, which a driver calls between rounds. **A model's own compute
is free.** A model that spends ten real seconds in `predict` and makes no tool
calls has a `decision_latency_s` of zero, and its advice lands as though it had
answered instantly.

`Session.__init__` takes `charge_real_time: bool` and `stopwatch: Stopwatch`
for precisely this, assigns both to attributes, and never reads either — a grep
for `charge_real_time` returns two lines and both are in the constructor. The
`Stopwatch` in `core/clock.py` is complete, tested, and documents the intended
use in its own docstring, including the `fixed_s` override "how a determinism
test pins a run that would otherwise depend on the speed of the machine it ran
on". Only the wiring is missing.

This matters more than a normal gap because of what the benchmark is for. The
headline comparison is a tool-using language model, which decides in seconds,
against classical models that decide in microseconds. The coordinator's
objection to language models is not an opinion but a budget, and *the budget is
the metric*. With thinking free, that comparison is measured wrong, and it is
wrong in the direction that flatters the language model — the one direction a
benchmark cannot afford to be wrong in, because it is the direction someone
would accuse it of being biased toward.

The reason it was left is real and is the whole difficulty: **charging measured
real time breaks determinism.** Invariant 4 requires that the same seed produce
the same trace, and a run whose world advances by however long this machine took
is a run that does not reproduce on another machine. Fidelity and determinism
pull in opposite directions here and no single default satisfies both.

## Decision

**Charge the model's own compute to simulated time, under a policy the run
selects and the cell records.** `LoopConfig.think`, one of:

| policy | what happens | reproduces | what it is for |
|---|---|---|---|
| `free` | nothing is charged; only tool latency moves the clock | yes | today's behaviour, and the default |
| `fixed` | every model call costs `think_s` simulated seconds | yes | a controlled "what if this model took two seconds" |
| `measured` | the call is timed and the world advances by that much | no, across machines | fidelity: what this model actually costs here |

`free` stays the default so that no number recorded before this ADR silently
changes meaning. `fixed` is the deterministic way to ask the question, and is
what a determinism test uses — it is `Stopwatch(fixed_s=...)` doing the job its
docstring already describes.

**What is measured is the model's code, not the harness's.** `Session`
accumulates the real seconds spent inside `call`, and the thinking window
subtracts that delta. Otherwise the agentic path would charge a model for time
spent inside our own tool handlers, and a slow handler would read as a slow
model. The fixed-cycle methods make no tool calls, so the subtraction is a
no-op there and the implementation stays uniform.

The window wraps model code and nothing else: `observe`, the forecast, and
`advise` in the fixed cycle, and the whole of `act` in the agentic one. The
world advances *during* the turn, so a later call in the same turn sees a world
that moved while the model was thinking about the earlier one. That is the
faithful behaviour and it is the point of the invariant.

`decision_latency_s` picks this up with no change, because it is already
`now - turn_start`, and so does everything downstream of it — the advisory's
landing time, the operational family, the overrun count.

## Consequences

Invariant 3 becomes true for compute and not only for I/O. The operational
family starts measuring what it claims to measure, and `decision_p95_s` becomes
comparable between a model that thinks and one that probes.

The parity pair sharpens. Under `measured`, the difference between an agent and
the same estimator driven through the fixed cycle now includes the agent's own
reasoning cost, which is most of what makes an agentic architecture expensive.

**A `measured` run is machine-dependent and must say so.** The report's limits
page already states that operational numbers compare models only within one run
on whatever machine took it; that sentence now has to cover the decision numbers
as well, and the policy is recorded per cell so a reader can tell which kind of
run they are holding. A `measured` cell and a `free` cell are not comparable and
the report must not put them in one column.

`bench` keeps `free` until there is a model whose compute is worth charging.
Moving the default earlier would invalidate every recorded baseline for a
difference of microseconds, which buys nothing and costs the whole existing set.

Cost: the world now moves inside a turn under two of the three policies, so a
model calling `predict` and then `advise` is advising about a slightly later
world than it predicted for. That is correct, it is what deployment does, and it
will make some currently-clean numbers slightly worse.

## Alternatives rejected

**Charge measured time always.** Rejected: it breaks invariant 4 outright, and
the benchmark's only asset is that two people get the same number.

**Charge nothing, and report wall-clock separately.** This is effectively the
status quo, since `wall_clock_s` is already recorded per cell. It fails because
a number reported beside the run does not *act*: late advice is only late if
the world moved, and reporting the delay without applying it measures a model in
a world where being slow is free.

**Let the model declare its own cost via `advance()`.** The session already
allows it and the docstring invites it ("a model calls it to think"). Rejected
as the mechanism: it is self-reported, it is optional, and the one thing a
competitive benchmark must not do is let a submission choose what it is charged.

**Reuse `extra_latency_s`.** It is a different knob and stays. It delays when an
advisory lands without advancing the world during the turn, which makes it the
right tool for "what if this model were slower" as a *scenario* variable, and
the wrong one for "what did this model actually cost".

**A boolean, as the dead parameter had it.** Rejected: two states cannot express
the three cases, and the two that matter most — deterministic-and-charged versus
faithful-and-charged — are exactly the two a boolean collapses.
