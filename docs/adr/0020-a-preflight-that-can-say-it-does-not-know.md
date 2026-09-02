# 0020 — A pre-flight that can say it does not know

## Context

A model reaches the harness as an import path and some constructor arguments
(ADR 0013), and the normal way in is an adaptor the submitter writes, because
only they know whether their model wants a batched tensor, a feature row, or an
HTTP call. That adaptor is the part most likely to be wrong, and today the
cheapest way to find out it is wrong is to run something:

- `scionarena models <spec>` loads the model and prints what it declares. It
  never calls `predict` or `advise`, so a shape error survives it intact.
- `scionfit check` is the real answer and runs the probe suite against a built
  substrate — minutes, and more at a serious tier.
- `bench run` is an hour, and finds the error in cell one of ninety.

So the first feedback an adaptor author gets on a transposed array is a failed
cell in a sweep, attributed to their model, with a traceback from inside the
driver. That is the worst possible place to learn it.

## Decision

**`scionarena adapt <spec>` — a pre-flight that calls the contract against
synthetic input, in seconds, and reports three states rather than two.**

It builds no substrate. The topology it uses is a handful of `InterfaceAttrs`
and `PathRef` objects constructed directly, which is why it costs a second and
why it can run on a machine that could not hold the realistic tier.

Every check returns one of:

- **ok** — the behaviour was observed and matched.
- **contradicted** — the behaviour was observed and did not match what the model
  declared. This is the only state that is a finding.
- **unchecked** — the pre-flight could not establish it here. Not a failure, not
  a pass, and counted separately so the summary line cannot be read as a score.

The third state is the point. A cheap check that reports two states is a check
that must guess, and a pre-flight that guesses **pass** is worse than no
pre-flight: it sends a broken adaptor into an hour of compute with a green tick
behind it. Several declarations cannot honestly be established without a
substrate — `staleness_aware` needs telemetry that ages, `self_consistent`
needs a closed loop, `uses_tools` needs a session — and those are `unchecked`,
by name, every time.

**It never reports a verdict.** The output says what it found and then says, in
those words, that conformance is the check that decides. A pre-flight that
printed PASS would become the thing people quote, and it is not measuring what
the probes measure.

## Consequences

An adaptor author gets a loop measured in seconds: write, check, fix. The errors
it can catch are exactly the ones that cost the most to find later — a `predict`
that returns a list instead of a mapping, weights keyed on something that is not
a path id, a model that raises on `demand=None`, a `reset` that does not clear.

It also catches the one thing ADR 0019 made possible to get wrong: a model
declaring `uses_tools` without implementing `act`. Under `drive="auto"` that
declaration is now believed, so the mistake changes which code path a sweep
takes. The pre-flight names it in a second; the sweep names it after building
a world.

The three states have to be counted separately in every output, including the
one-line summary, or the distinction is rebuilt in the reader's head as
pass/fail and lost. `unchecked` is printed with its count and its reason.

Cost: a second implementation of "does this model behave", beside the probe
suite. They will drift unless something holds them together, and what holds them
together is that the pre-flight checks a strict subset and says so — a check
here that conformance does not have is a check in the wrong place.

## Alternatives rejected

**Extend `scionarena models` instead of adding a command.** Rejected: `models`
is documented as loading and printing without running anything, and its value is
that it is the one thing that cannot itself fail for a model's own reasons.
Calling `predict` inside it would make a model that raises look like a loader
bug.

**Run the real probe suite on a smoke-tier substrate.** Closer to correct and
still the wrong tool: it is minutes rather than seconds, it needs the substrate
importable, and it answers a question the author is not asking yet. They want to
know whether the wiring is right, not whether the model is good.

**Two states, treating unchecked as pass.** Rejected, and this is the decision
the ADR exists for. The output would be a green tick over an unexamined claim,
and the first thing anyone does with a green tick is quote it.

**Two states, treating unchecked as fail.** Rejected for the opposite reason: an
honest limitation would render as a failure, which is the same mistake
`DECLARED_ABSENT` exists in conformance to avoid.
