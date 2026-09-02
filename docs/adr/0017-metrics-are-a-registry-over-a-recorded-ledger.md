# 17. Metrics are a registry over a recorded ledger, and accuracy needs a ledger that did not exist

Date: 2026-09-02

## Status

Accepted. Phase 3. Closes M4's registration criterion and supplies what Phase 4's
report is a report *of*.

## Context

Four families are required: accuracy, decision quality, stability, operational
behaviour. Stability exists (ADR 0010, 0011). The other three did not, and one of
them could not, because **the closed loop never asked a model to predict
anything**.

`run_loop` calls `observe` and then `advise`. `predict` — the method every
accuracy metric in §28 is about, the one that carries the distributions and the
horizons — was never called outside the conformance probes. So there was no
prediction to score, no realised outcome recorded to score it against, and
pinball loss, CRPS and interval coverage were unimplementable rather than
unimplemented. That is a bigger gap than "some metrics are missing": it means the
harness could not have told a well-calibrated model from a confidently wrong one
in any run it has ever done.

Decision quality has a subtler version of the same problem. Regret against the
hindsight-optimal assignment is *the* number that separates this project from
every open-loop SCION benchmark — a model can predict accurately and route badly,
and only this sees it — and computing it needs the true per-path cost on the same
grid the advice was measured on. The sampler recorded an aggregate mean cost and
one reference path's share. Neither is enough.

## Decision

### A metric is a registered function of one recorded bundle

`instrument/metrics.py` holds a registry. A metric declares its name, family and
direction, and takes a `MetricInput` — series, forecast ledger, truth series,
latencies, session summary. It returns a float, **or a mapping**, and a mapping is
flattened into `name.key`. That is how "stratified, never as a single number" is
enforced rather than requested: an accuracy metric returns one value per horizon
and there is no way for it to return a mean over them.

Registration is a decorator and the runner does not enumerate metrics, so a new
metric is a new function in a new module and nothing else. That is M4's
acceptance criterion and it is what lets the registry grow through Phases 5 and 6
without churn.

`MetricInput` is declared in `instrument/`, which may not import `exposure`. So it
is plain data — arrays, mappings, floats — assembled by whoever ran the loop. The
metric functions therefore cannot reach the substrate, the session or the model,
which is the property that makes them safe to run on a result file read back from
disk months later.

### The loop records a forecast ledger, and it costs the model to produce

Each round, for each scope, the driver calls `model.predict(...)` at the declared
horizons and records `(t, scope, path, horizon, point, quantiles)`. The call
happens **inside the turn**, so the time it takes is charged as decision latency
exactly as `advise` is. A model that ships intervals pays for shipping them, and a
model whose forecast head is slow is measurably worse. That is invariant 2 applied
to the one piece of model output that was previously free.

It is **off by default** (`LoopConfig.record_forecasts`). Turning it on changes
what a round costs and therefore what the world does while the round runs, so
every M3 and M4 number recorded before this would move. `bench` turns it on
always, because §28 makes accuracy mandatory; `demo` and `conformance` leave it
off.

### Truth is recorded per path, on the sample grid, for the paths the model saw

`Series.path_cost` maps `(src, dst, path_id)` to the true cost of that path at
each sample. Bounded by construction: only the scopes being driven, and only the
paths the model was shown. Eight scopes at a twenty-path limit is a hundred and
sixty series, not the thirty thousand that recording every path at the realistic
tier would be.

### Hindsight-optimal is the best *fixed* path, and that makes regret an upper bound

The honest hindsight-optimal is a fixed point: move the traffic and the cost of
where you moved it changes. Computing that per sample means solving an
equilibrium per sample, which is a research problem inside a metric.

So `regret_ms` is measured against `min_p cost_p` at each sample — the cheapest
path *as costs actually were*. That is a lower bound on achievable cost, because
piling the whole scope onto it would have raised it. Therefore the reported regret
is an **upper bound on true regret**, and it is named and documented as one rather
than presented as the thing itself. It is still the number that separates a model
that predicts well and routes badly from one that does not, because the bound is
the same for every model on the same world.

### Adaptive conformal is the coverage detector, per §19.5

`α_{t+1} = α_t + η (target − 1{y_t ∈ C_t})`. No exchangeability assumption, which
matters here specifically: the observations are biased toward the paths the model
recommended, so the sequence is not exchangeable by construction and a classical
conformal guarantee would not hold. The metric reports both the trailing empirical
coverage and the drift of `α`, because a model can hold nominal coverage by
widening its intervals without limit and the drift is what shows it.

## Consequences

`predict` becoming a hot-path call means a model that only implemented `advise`
now fails a `bench` run rather than a probe. That is correct — §28 requires
accuracy — and the failure is a recorded cell with a sentence, not a dead sweep.

Accuracy metrics are `None` for a run recorded before this, and the registry
returns `None` rather than zero for a metric whose inputs are absent. Zero would
be a score.

The compliant-share threshold — the fraction of the population that must follow
advice before a mechanism stops working — is deliberately *not* a metric here. It
is a property of a sweep across the defector axis, not of one run, and computing
it per cell would produce a number that looks like a threshold and is one point.
It lives in `bench/score.py` as a function over results.

## Alternatives rejected

- **Score accuracy from the advisory instead of from `predict`.** An advisory is a
  decision, not a forecast; it has no horizon and no distribution. Scoring it as
  though it were would report a model with excellent routing and no calibration as
  well calibrated.
- **Call `predict` outside the turn so it is free.** Then a model can buy accuracy
  with unlimited computation and pay nothing, which inverts the one property the
  harness exists to measure.
- **Record truth for every path.** Thirty thousand series per run at the realistic
  tier, to score twenty per scope.
- **Solve the equilibrium for hindsight-optimal.** The right answer and the wrong
  place; a metric that takes longer than the run it scores will be switched off.
- **A metric registry keyed on the runner enumerating names.** That is what M4's
  criterion exists to forbid, and it is how the last four metrics ended up welded
  into `LoopResult`.
