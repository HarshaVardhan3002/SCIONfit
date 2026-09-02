# 0019 — A cell records what the model is, and how it was driven

## Context

Two facts a benchmark result needs before it can be compared to another one are
missing from every cell written so far.

**What the model is.** A cell is `(model, axes, repeat)`. The product's stated
goal is two-level: find the best architecture, then the best variant within it.
"Architecture" is a grouping over models that nothing records, so the question
*does a graph network beat a tool-using LLM here* is answerable today only by
someone who already knows which submitted name is which.

**How it was driven.** `exposure/contracts.py` has two protocols and the second
inherits the first:

```python
class ToolUsingModel(PathModel, Protocol):
    def act(self, session: SessionLike, deadline_s: float) -> None: ...
```

so an agentic model implements the four fixed-cycle methods as well as `act`,
and the same object can be run either way. Two runners exist. `run_episode`
(`exposure/session.py`) branches on `capabilities.uses_tools` and calls `act`.
`run_loop` (`exposure/loop.py`) is the closed loop with the sampler, the
forecast ledger and the metric bundle — and it **never calls `act` at all**.

`bench` uses `run_loop`. So every sweep cell ever written ran the fixed cycle,
whatever the model declared. A submitted agentic model would have been scored on
a code path it does not implement for, with no error, no warning, and an
`operational` family reporting the decision latency of a `predict` call the
author never intended anyone to time. The capability is declared, cross-checked
by conformance, recorded on the cell since ADR 0018 — and unreachable from the
benchmark that reads it.

Even once it is reachable, the comparison it enables is confounded. The fixed
cycle's probing policy is the harness's, hardcoded in `_one_scope`: query the
scope, probe `probes_per_cycle` paths, round robin. Both paths are charged
through the same `Session` and the same `Budget`, so neither model gets data
free; the asymmetry is not cost, it is **authorship of the probe policy**. A
sweep therefore compares

    (a forecaster + our probing policy)  against  (an agent + its own policy)

and scores the agent on two competences where it scores the forecaster on one.
If the agent wins, we cannot say whether it forecasts better or merely probes
better, and those two findings have entirely different consequences for anyone
deciding what to deploy.

## Decision

**A model declares an architecture tag, and the harness — not the model —
decides which way the model is driven. Both are recorded on the cell.**

1. `Capabilities.architecture: str` — free-form, conventionally one of a short
   list (`persistence`, `ewma`, `gbdt`, `gnn`, `dqn`, `llm_api`, `llm_local`).
   It is not validated against an enumeration, because an enumeration in the
   contract means a new architecture requires a change to the module every model
   author imports.

2. `LoopConfig.drive: "auto" | "fixed" | "agentic"`.
   - `auto` reproduces the declaration-based branch, so it is what an unchanged
     caller gets — except that it now actually reaches `act`.
   - `fixed` forces a tool-using model through the observe/predict/advise cycle.
   - `agentic` on a model that cannot `act` raises. A silent fallback to the
     fixed cycle is precisely the bug this ADR exists to fix, and reintroducing
     it as an error path would be worse than leaving it where it was.

3. `run_loop` gains the agentic branch, inside the sampled episode, with the
   deadline set from the cadence. The model publishes its own advisories through
   the tool it already has; the driver stops calling `observe` and `advise` and
   does not otherwise change.

4. The **resolved** value, not the requested one, is recorded on `LoopResult` and
   on `CellResult` as `drive`, so a cell says which way it ran rather than which way it was
   asked to. It is called `drive` rather than `mode` because `SweepSpec.mode`
   already means the shape of the matrix (`oat` or `grid`), and two unrelated
   `mode` fields one layer apart is a bug waiting for a tired reader.

5. `record_forecasts` still fires in agentic mode, after `act`, over whatever
   paths the model chose to learn about. An agent that never queried a scope has
   no paths there and records no forecast for it. That is a finding about the
   agent, not a gap in the ledger.

6. **A sixth tool, `list_scopes`.** Making the agentic path reachable exposed a
   hole that had never mattered because nothing used it: all five tools required
   a `(src, dst)` the model had no way to learn. The fixed cycle's driver knows
   the scopes and never tells the model; an agent loaded by name from a sweep —
   the only way `bench` loads anything — could therefore not have advised on
   anything at all. `BudgetedProber` hid this by taking its scopes as a
   constructor argument, which a bench user cannot supply because they have not
   seen the world, which in turn is why it was absent from `BUILTIN_MODELS` and
   why the only tool-using model in the repo could not appear in a suite.

   The tool returns the scopes the host population is actually on, read off the
   world rather than remembered from what the driver was asked to run, so a
   scope added mid-run appears and both drives are told the same thing. It costs
   a query: a call that costs nothing is a call a model may make every round
   without consequence, and then the budget stops meaning anything (invariant
   2). Deployment-faithful, too — a real node is *configured* with what it
   serves rather than discovering it, and configuration you have to ask for is
   still configuration.

## Consequences

The parity pair becomes expressible: the same model, same seed, same axes, run
both ways. The `fixed` cells compare architectures with information
acquisition **held equal**, which is the comparison the accuracy family was
designed for and has never run. The difference between the pair isolates the
value of choosing your own probes, per architecture and per scenario — a number
that exists nowhere.

Two things the report must then say, and neither is optional. A model forced
into `fixed` mode is not being shown at its best, and a page that prints the
number without that sentence invites it to be read as the model's score. And the
harness's probe policy is a variable in every `fixed` result: it is dumb on
purpose, identical for every model, and therefore fair without being good. It
belongs in the limits section beside the tier.

Existing results keep rendering. `drive` and `architecture` default to empty and
absent means unknown, which for every cell written before this ADR is true —
they ran driven, but they ran driven by accident, and back-filling a value
recorded nowhere would be a guess printed as a fact.

Cost: agentic cells are slower and their latency is real, which is the point.
An agent that spends its whole slot is applied late, against a world that moved.

## Alternatives rejected

**Have `run_loop` branch on `uses_tools` and stop there.** The one-line fix, and
it leaves the confound intact while making it invisible: every agentic model
would be compared against every forecaster on a different information diet, with
nothing on the page saying so.

**Validate the architecture tag against an enumeration.** Rejected: it puts a
list of known architectures in the module every model author imports, so adding
one becomes a change to the contract. The tag is a grouping key; an unknown
value groups by itself, which is the correct behaviour for something new.

**Derive the architecture from the import path or the class name.** Rejected for
the reason ADR 0018 rejected re-deriving the tier at render time: it produces a
value that is internally consistent and false. `models.GNNSelector` might be a
transformer with an unfortunate name.

**Make `agentic` fall back to the fixed cycle when the model cannot act.** Rejected.
That is the current bug with a different spelling.

**Run only the parity pair and drop single-mode cells.** Rejected: it doubles
the cost of every sweep for a decomposition many users will not want, and a
model with no agentic path cannot form a pair at all.
