# Plan — from the mentor's review to a benchmark someone else can run

**Status:** current plan of record. Supersedes the milestone ordering in `HANDOFF.md` §6
for everything after M4; the milestone specs themselves are unchanged and still hold.

Two inputs produced this document. The first is the product statement: *a person loads
their own model, presses run, a stress-test sweep executes, and a PDF comes out saying
what was tested and how the model did.* The second is the re-review of this repository at
`7f83fe4` against Master Spec v1.2, in `mentor_feedback/`.

They point the same way. The review closes two of our open questions, names four substrate
defects, and identifies our harness as the rig two of its own milestones are specified in
terms of. None of that changes the product; all of it changes what the product is allowed
to print.

---

## The ordering argument

The product is phases 1 through 5. Phase 0 is four bug fixes. Phase 0 goes first, and the
reason is not tidiness.

A benchmark's only asset is that its numbers mean something. Three of the four defects
corrupt the closed-loop results silently — no test fails, no output looks wrong, and the
report renders cleanly with figures that are incorrect. If the sweep engine and the PDF
land first, every artefact produced before the fix has to be withdrawn, and the withdrawal
is the thing a reader remembers. Phase 0 is roughly a week. The rest of this document is
months.

The corollary is the discipline for everything after: **no metric ships without a
reference model that fails it, and no number is published from a tier whose adapter has
never been run against the real thing.** That is invariant 6 and `docs/ADAPTERS.md`
respectively, and both already exist. They apply harder now that outsiders will read the
output.

---

## Phase 0 — correctness

Four defects, all confirmed at source. Each ships with a regression test named for the bug
it prevents, per the convention in `CLAUDE.md`.

### C1 — the AS policy filter leaks

`core/scenario.py`, `_on_policy_filter`. The handler enumerates `segments.iter_segments()`
once, at the moment the event fires, and filters what it finds. Path composition is lazy
by design (ADR 0005), so any segment materialised after the event has never been through
the filter. At the `dev` tier roughly eleven thousand paths still traverse the AS the
scenario says is filtered.

This is not a cosmetic leak. `as_policy_filter` is the event that expresses the corrected
domain model's central claim — a path becomes unavailable without anything physical
happening. A leaking filter means the one scenario that tests that distinction does not
test it.

**Fix.** Filtering is a predicate on the segment set, not a one-time sweep. Hold the
filtered AS set as scenario state and apply it at the point segments enter the served
inventory, so a lazily-composed segment is filtered on discovery. Regression test asserts
zero paths traverse a filtered AS after composition is forced at `dev`.

### C2 — the bandwidth probe leaves negative load behind

`exposure/session.py:585-590`. The probe adds `BWTEST_LOAD_MBPS` to each egress
interface, advances two seconds, reads the loaded metrics, then subtracts the same amount.
The subtraction is unconditional and assumes the addition is still there. When a host
population grid tick lands inside the two-second window it rebuilds demand from the
advisory, discarding the probe's contribution; the subtraction then applies to a baseline
that never carried it, and the link sits at −50 Mbps until the next tick rebuilds demand
again.

A model that probes is therefore measuring a network its own instrumentation has damaged,
and the damage is invisible in the report. This interacts badly with the budget results,
because the model that probes most is the model most affected.

**Fix.** The probe's load is a scoped contribution with an identity, not an arithmetic
adjustment — register it, and have demand reconstruction preserve registered probe load
across a rebuild. Regression test asserts no interface holds negative offered load after a
bandwidth probe that straddles a grid tick.

### C3 — the beacon stream drops re-signings

`core/segments.py:675`. `_resign_all` assigns `self.last_resigned` rather than extending
it, and `session._harvest_beacons` clears the list on drain. Any resigning round between
two drains is lost; measured, about 81 % of re-signings never reach the feed.

**Fix.** Accumulate rather than assign. One line. It becomes urgent under B1, which raises
the true event rate sixtyfold, and M5's rewritten R4 reads this feed.

### B1 — the beacon interval is wrong by sixty-fold

`core/segments.py:105`, `interval_s: float = 300.0`. Upstream origination, propagation and
registration all default to **5 seconds**. `lifetime_s = 21_600.0` is correct and its
comment is accurate; only the interval is wrong.

The `ASSUMPTION(Q1)` marker on `BeaconPolicy` did its job — the number was flagged as
unverified, and it has now been verified and found wrong. That is the marker working, not
failing.

This is the clock of the identity experiment. At 300 s an identifier under `crypto_bound`
changes about twelve times an hour and a model has real time to accumulate state between
refreshes; at 5 s it changes seven hundred and twenty times an hour, and the
re-beaconing-to-lifetime ratio moves from 72:1 to roughly 4,300:1. Our published result —
21 distinct identifiers, `stale_weight = 1.0`, over 6,000 s — was measured at the wrong
cadence and understates the effect by more than an order of magnitude.

**Fix.** `interval_s = 5.0` as the default; keep 300 available as a scenario override so
the old runs remain reproducible. Quantise `expiry_s` to the 337.5 s hop-field expiry
quantum, which is now a known constant rather than an assumption: a model reasoning about
time-to-expiry in a real deployment sees 256 discrete values, not a float. Re-record every
run under `docs/evidence/`.

Correcting it strengthens the result rather than weakening it, and it changes what M5's R4
should be — see Phase 6.

### Q1 and Q6 close

Both were answered from the `scionproto` source tree rather than by us, and both are
currently marked OPEN in `docs/OPEN_QUESTIONS.md`.

**Q1 — the SCION path fingerprint is structural.** `snet.Fingerprint` is a SHA-256 over the
ordered sequence of `(ISD-AS, interface-ID)` pairs and contains no cryptographic material.
The path combinator collapses multiple constructions of the same interface sequence by
default, exposing distinct segment IDs and MACs only to a caller that explicitly asks. The
fingerprint survives re-beaconing by design.

Mark RESOLVED with that evidence. `identity_policy` acquires a documented default —
`structural`, because that is what the stack does. Both policies stay implemented; they
are cheap and Phase 6 needs them.

**Q6 — there is no SCMP rate limiter.** The SCMP specification says only that SCMP "may be
subject to rate limiting", and no token bucket exists anywhere in the router tree. Our
three constants in `exposure/tools.py` are invented:

```python
SCMP_LIMIT = RateLimit(calls=5, per_s=1.0)
BWTEST_LIMIT = RateLimit(calls=1, per_s=30.0)
PATH_SERVER_LIMIT = RateLimit(calls=2, per_s=1.0)
```

Mark RESOLVED as *per-deployment, unspecified upstream, must be swept*. The fix is not to
delete the limits — an unlimited probe budget is not a safe assumption either — but to
move them out of the substrate and into `Scenario`, beside `BeaconPolicy` and `LinkParams`,
with the current values as defaults. Today two runs made under different real-world
assumptions are indistinguishable in the report. After the move, a run states its regime,
and Phase 2 sweeps it as an axis.

**Q2 — partial.** Per-link offered load is not directly observable (beacon metadata carries
static capacity; the data plane exposes no per-hop state to endpoints) but is estimable,
and v1.2 §7.1 states the condition exactly. R6 and R7 do not need redesigning, but their
claim is narrower than their names suggest — they hand the model a demand vector and check
it conditions on it. Record the narrower claim; Phase 6's `Demand` split sharpens it.

---

## Phase 1 — a model that is not ours — **landed**

Delivered as ADR [0013](adr/0013-a-model-enters-as-an-import-path.md), `exposure/loading.py`
and `docs/MODELS.md`. `scionarena models <spec>` loads a model and prints what it will be
tested on without building a scenario; `scionarena conformance check`, `scionarena demo`
and the UI field all take the same spec, and the reference models became aliases for import
paths so there is one resolution rule rather than two. A spec ending in `.py` is read as a
file, so a single script with no packaging is loadable.

Two things the section below did not anticipate. Splitting `module:attribute` at the first
colon is wrong on Windows, where `C:/models/mine.py:MyModel` resolved to a module named
`C` and the error told the user to `pip install C`; the separator is the last colon.
And the demo keyed its figure series on `capabilities.name`, so two models declaring the
same name drew as one line — `labels_for` disambiguates by spec.

Today `demo.py` resolves models from `REFERENCE_MODELS`, a dictionary in the repo. There
is no path by which an outsider's model enters the harness. This is the single change that
turns the project from a result into a product, and it is independent of everything else,
so it can start the moment Phase 0 is merged.

**Loading.** An import path: `mypkg.mymodule:MyModel`. The user pip-installs their code,
passes the string on the CLI or types it into a field in the UI, and the harness
instantiates it and checks it against `PathModel` at load time with a readable error when
it does not conform. No packaging burden, no sandbox, works identically for a scikit-learn
regressor and an LLM wrapper. This is what `lm-eval-harness` and `evaluate` do and users
already understand it.

**What the harness tells them.** `Capabilities` and the `DECLARED_ABSENT` / `FALSE_CLAIM`
asymmetry already exist and are the right shape: a model that declares it cannot do
something is scored as not doing it; a model that claims it can and cannot is scored as
wrong. Surface this at load time as a capability report, before any run, so a user sees
what their model will and will not be tested on.

**Deliverables.** `scionarena bench --model pkg.mod:Class`; the same field in the UI;
`docs/MODELS.md` with a fifteen-line worked example of a conforming model and the
capability declaration that goes with it.

---

## Blocking Phase 2 — the performance gate measured its own process — **cleared**

Found while running the gate for Phase 1, on a branch touching no file under `core/`:
`realistic.substrate_step_s` reported 4.5 ms against a 2.09 ms baseline, a 145% regression,
and it reproduced with the branch stashed. After a few million segment re-signings,
*everything in the process* is about twice as slow and stays that way — a freshly built
world run for 600 simulated seconds costs 2.1 ms per step in a young process and 4.5 ms in
an aged one. `benchmarks/run.py` measured `substrate_step_s` **last**, after four
allocation-heavy benchmarks in the same process, so the gated number was partly a function
of how much allocation preceded it.

**Fixed** in ADR 0014: each metric is measured in its own subprocess, which does its own
setup. Measurement order is no longer an input. Isolated, `substrate_step_s` repeats to
within 2% across processes, where it used to come out bimodal (2.19 / 4.36 / 4.70 / 4.42).
The tolerance was not widened.

Isolating it exposed two things the diagnosis did not predict, both found by measuring the
*old* core through the *new* harness in a worktree at `f68cc3d`:

- **There is no regression between that commit and HEAD.** Isolated, its `substrate_step_s`
  is 3.45 ms and HEAD's is 3.42 ms. The whole 145% was measurement.
- **The recorded 2.09 ms is not reproducible at its own commit** — same machine, same
  interpreter, same numpy, and `benchmarks/run.py` byte-identical between the two. Why it
  was written cannot be recovered; it is recorded as unexplained rather than guessed at.
- **`n_segments` had the same disease**, and this one was invisible. A store materialises
  path sets lazily, so its count grows as it is queried, and the old suite read it off a
  store that `link_metrics_batch`'s setup had already queried fifty times: 18,327 recorded
  for what is 15,002 at rest. Both are now recorded, under names that say which is which.

Baseline re-recorded; every metric within ±3% on a verification run. Evidence and the
ruling-out runs: `docs/evidence/substrate_step_process_ageing.py`.

---

## Phase 2 — the sweep engine (`bench/`, M6)

`src/scionarena/bench/__init__.py` is six lines. This is where the "automated stress-test
sweep" lives.

**Axes.** The five from Master Spec §28 and its M4½, which are also the axes we already
half-own:

| axis | range | status |
|---|---|---|
| population size | 10²–10⁴ selectors | have — `HostParams.n_hosts`, multinomial sampling |
| defector fraction | 0–100 % | partial — `defector_fraction` exists, one kind (`greedy`) only |
| mechanism ladder | point-rankings → +intervals → +discipline → +jittered mirror | partial — rungs 1–2 only |
| paths per selector | 1 … k | missing — hosts always sample a distribution |
| staleness | information delay, not decision delay | nearly — `extra_latency_s` and `decision_s` exist, telemetry delivery delay does not |

Plus, after Phase 0, **probe rate-limit regime** as a sixth axis, because Q6 made it a
scenario parameter rather than a constant.

Four of the six are done or close. The two missing ones are the same item:

**The selector-discipline rung is the highest-value unbuilt thing in the repository.**
ε-set width, hysteresis margin, minimum dwell, per-host timer jitter, in `HostParams`. The
seam is already where it needs to be — `HostParams.resample_s` is a minimum dwell in all
but name, and `_thin` already carries placed hosts across rounds. Rung 4, the jittered
mirror, is delivery jitter on advisory application, which currently lands instantly and
simultaneously for every host. Paths-per-selector is cheaper than it looks: a selector that
picks one path is a population of size one sampling the advisory, so it is a constraint on
the draw rather than a new mechanism.

**Engine.** Scenario matrix × models, executed in parallel, resumable after interruption,
writing one result file per cell carrying the seed, the substrate version digest, the
scenario, and the full metric set. Two machines running the same suite produce identical
scores — that is M6's gate and it is what makes the benchmark citable.

**Mandatory baselines.** §28 requires that accuracy be reported against persistence,
Tier-0-only, static-only, latest-sample and EWMA. Every sweep runs them whether the user
asked or not, and the report shows the user's model beside them. A model that does not beat
persistence has not earned a forecast head, and the report should say so in those words.
`CapacityProportional` already gives us the efficiency floor.

---

## Phase 3 — the metric registry

M4's remaining deliverables, plus what §28 requires. This is what the graphs are *of*, and
it is worth being explicit that **there is no loss curve here** — the harness evaluates
trained models, it does not train them. The four families below are the axes of the report.

**Accuracy.** Pinball loss, CRPS, and interval coverage against nominal — reported
stratified, never as a single number. The stratification that matters is by evidence
sparsity and by horizon, because a mean over strata hides exactly the case the model is
worst at.

**Decision quality.** True regret against the hindsight-optimal assignment. This is the
family that separates the project from every SCION benchmark that exists: a model can
predict accurately and route badly, and only this number sees it. Report it beside accuracy,
never merged with it.

**Stability.** Oscillation index and `fast_swing` exist (ADR 0010, 0011). Add convergence
time and the compliant-share threshold — the fraction of the population that must follow
advice before a mechanism stops working. That threshold is the number every stability
claim is conditional on, and it must be reported *per regime*, because a threshold measured
with 10⁴ mixing hosts says nothing about 10² single-path gateways.

**Operational.** Decision wall-clock and its distribution, tool calls per decision,
information gained per unit cost, probe budget consumed, memory retained, and behaviour
when the budget is exhausted or the deadline missed. Nobody else measures this, and it is
where an LLM will look dramatically unlike a gradient-boosted regressor. M3 already found
the shape of it: 257 MiB per decision round for the stochastic model against 21 MiB for the
greedy one. The retention policy question that raised is answered here.

**Coverage detector.** Already M4 deliverable 2, and v1.2 §19.5 now specifies the update
rule exactly — `α_{t+1} = α_t + η (target − 1{y_t ∈ C_t})`, adaptive conformal inference,
long-run coverage on arbitrary sequences with no exchangeability assumption. Run the event
log, count interval hits per bucket, report trailing coverage. It converts R5 from a shape
check into a test of the property that matters, and it is small.

**Registration.** A new metric registers without editing the runner. That is already the M4
acceptance criterion and it is what lets the registry grow through Phases 5 and 6 without
churn.

---

## Phase 4 — the report

**Charts.** matplotlib, behind a `[report]` extra. `core/` keeps its numpy-only rule; the
reporting stack is not a substrate dependency and must not become one.

**PDF.** matplotlib figures embedded into a document assembled with reportlab. Both are
pure wheels on Windows, neither needs a browser or a GTK stack, and the failure mode of an
HTML-to-PDF converter — silently different output from the interactive view — is the one
thing a benchmark report cannot afford. The existing HTML report stays as the interactive
view; the PDF is the artefact a user attaches to a paper.

**Contents.** What was tested (scenario, tier, seed, substrate digest, every axis value,
and the probe-limit regime the run assumed), what the model declared it could do, the four
metric families with their figures, the baseline comparison, and the conformance verdicts
with their `DECLARED_ABSENT` / `FALSE_CLAIM` distinction intact. A reader who has the PDF
and the seed can reproduce the run.

**One requirement that is easy to lose.** The report states its own uncertainty. A metric
computed over few observations is labelled as such rather than printed to three decimals.
The harness's whole argument is that it is honest about what it knows.

---

## Phase 5 — model adapters

Four families, in this order. The order is by dependency, not by interest.

1. **Classical baselines.** Persistence, EWMA, static-only, latest-sample, plus a
   gradient-boosted regressor. Cheap, and they are the bar — without them the LLM result
   has nothing to be measured against and means nothing.
2. **LLM over an API, as a tool-using agent.** The headline comparison. `ToolUsingModel`
   and `BudgetedProber` already exist, and the exposure layer already charges wall-clock
   and probe budget, so the accounting is in place. What is missing is the adapter and the
   context-management measurement that makes the comparison interesting: the harness never
   summarises (invariant 1), so what the model chooses to retain is the experiment.
3. **Local open-weight LLM.** Same tool-using path, different transport. Adds a serving
   dependency but makes cost-per-decision comparable without API spend, and makes the
   context-rot curve affordable to sweep.
4. **Deep learning — GNN nowcast and the DQN selector.** Heaviest by a wide margin: needs
   torch and a training pipeline the harness does not have, since everything else here
   arrives pre-trained. Worth splitting the two questions v1.2 §28 separates — does spatial
   structure improve the nowcast, and does anything beat persistence at +60 s / +300 s.
   Expect the second answer to be negative on many strata. That is a finding.

---

## Phase 6 — probes the review made possible

**R4, re-aimed.** Q1 removes its original target: a model that collapses under
`crypto_bound` is not wrong about deployment, because `crypto_bound` has no correspondent
in the deployed stack, and failing it would be a false positive against the thing
conformance claims to measure. The real hazard is a model keying memory on something that
is *not* the fingerprint — the raw path bytes, the dataplane path object, or the segment
IDs that `findAllIdentical=true` exposes. Keep both policies, rename the failure semantics,
score the delta as a hygiene grade rather than a conformance failure. With B1 corrected the
interesting question is no longer `structural` versus `crypto_bound` but *how fast can
identity churn before a model that keys correctly still loses* — and the substrate can
already run it.

**R11 — layer discipline.** v1.2 §20.6 states which layers each requirement class may rank
on: a latency-class model ranks on static plus liveness and lets the dynamic layer widen
intervals, never reorder. The reasoning is a timescale argument — ranking on a 15-second-old
congestion estimate is routing on lagged load, which is the documented oscillation
mechanism. The probe is one variable: hold the static layer fixed, vary only the dynamic
layer, ask a latency-class model for a ranking, and fail it if the ranking reorders. No new
substrate. It pairs with R7, which establishes the model can see the dynamic layer; R11
establishes it uses it in the right place. Today this defect reaches the closed loop
undetected and gets attributed to the model's advisory shape rather than to its metric
choice.

**Calibration under shift.** Apply a scheduled `link_degrade`, then measure whether the
model's intervals recover nominal coverage and how fast. A model with fixed split-conformal
intervals will not; one with an adaptive level will. That is a real discrimination between
two designs the spec now has an opinion about.

**`Demand` splits into `intended` and `realised`.** `session.demand_from_advisory` builds
`Demand` from the published advisory, while `streams.py` documents the distinction
emphatically — "`share` is the realised share, not the published weight… a model that
assumes its advice was followed exactly is wrong by exactly that much." The harness knows
this in one file and forgets it in the other. A model handed `Demand` today is handed its
own intent labelled as load. Split the field, populate `realised` from `hosts.shares()`,
and leave it `None` where no telemetry covers the path. That sharpens R6, strengthens R9,
and creates the only available hook for a performativity probe.

**`docs/SPEC-MAP.md`.** One table: each spec requirement against the probe or knob that
covers it, with the rest marked *not covered — deliberate* or *not covered — gap*. It is
what would have caught B1 and surfaced Q1 and Q6 without a source audit, and v1.2 is far
more mappable than v1.0 was.

---

## Deferred, and named so it is not forgotten

**Unit-level output in the contract (review B2).** v1.2 §7.1 defines the *unit* as a
computable object — stack observed paths as an incidence matrix, merge elements whose
columns are identical, and that partition is exactly what the evidence can identify. A
spec-conformant model predicts unit states and composes them into path predictions.
`PathModel.predict` returns a mapping keyed by path, and `exposure/contracts.py` contains
zero occurrences of `unit`, `tier`, `coverage`, `diversity` or `ambiguity`.

This is the largest structural gap and the one that decides whether the harness tests the
spec's central abstraction or only its surface. It is backward-compatible to add — an
optional `units` return alongside `paths`, and a model that omits it gets
`DECLARED_ABSENT`, which is what that status exists for. It would upgrade R2 from "produces
a number for an unseen path" to "its unit estimates compose to its own path predictions,
and units shared between paths move together", which is a far stronger property and much
closer to what deployment needs.

It is deferred because it is a contract change and the contract is public API after M6.
Scope it alongside M5 rather than inside it, and decide before M6 freezes the interface.

**Return-path composition (F2, carried).** `rtt_s` is hardcoded at 2× forward. v1.1
confirmed the real stack reverses paths exactly, so this is a known divergence with a known
correct answer. It blocks G3.

**Performance gate excludes the closed loop (C4, carried).** `substrate_step_s` is measured
with no host scopes registered, so the gate does not cover the path the headline result runs
on. M3 already found a quadratic scan that no test caught for the same reason — everything
that would have caught it ran at the smoke tier.

**Doc drift (C8, carried).** README says "no required dependencies"; `DESIGN.md` says
Python 3.10+; `analytical.py` still says "rewritten in M1" four milestones later. Also:
M3's acceptance checklist is entirely unticked while its own progress section states every
criterion but one is met.

---

## What this changes about the milestone map

`HANDOFF.md` §6 has M5 (conformance v2), M6 (benchmark), M7 (RL) parallelising after M4.
That still holds, with two adjustments.

**M6 moves ahead of M5 and M7.** The product statement is a benchmark, and Phases 1–4 are
M6. M5's probes are better after Phase 0 changes what R4 is for, and M7 is not on the
critical path to anything a user asked for.

**M4½ arrives, borrowed from the spec.** The closed-loop campaign over the five axes is
Phase 2's first suite. It is the thing the mentor's own milestone is specified in terms of,
we are the only rig that can run it, and neither document currently says so. Saying so is
worth doing explicitly, in both directions.
