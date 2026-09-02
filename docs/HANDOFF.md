# scionarena — master handoff

**What we are building, why, and in what order.**
Read this once, in full, before writing code. Then work from `docs/milestones/`.

---

## 0. Where the existing code stands

`scionfit` v0.1 is in this repo and works. Treat it as a **proof that the interface shape
is viable**, not as a foundation. Most of it gets rewritten in M1–M5; the parts worth
keeping are named below.

### Coverage against the architecture proposal

| | status |
|---|---|
| **R1–R10** | one probe each, all passing/failing correctly against four reference models — **but** R4 tests the wrong thing (built on the now-corrected churn assumption), and all ten run only against a 6-path toy world |
| **G1–G6** | **not covered, and correctly so.** G1–G6 are design gaps with candidate fixes and discriminating experiments. They belong to the benchmark and RL front-ends (M6, M7), not to conformance. A conformance probe asks *can this model represent X*; a G-experiment asks *which of three ways of doing X actually works*. Different machines, different milestones. |
| E0–E8 experiments | not built. M6 and M7. |

### What survives

- `interface.py` — the `PathModel` protocol and the `Capabilities` declaration. The
  three design rules behind it hold. It gets **extended** in M2 (tools, budgets) but the
  existing methods keep their signatures. Now `exposure/contracts.py` (M0).
- The `DECLARED_ABSENT` / `FALSE_CLAIM` asymmetry. Keep exactly.
- The behavioural-probe discipline: change one thing, compare a model against itself,
  never against another model.
- `test_probes_discriminate` and the CI matrix. Keep and extend.
- Four reference models, as the seed of a larger baseline set.

### What gets replaced

- `env/world.py` entirely. It is a 6-path analytical toy with the wrong churn model.
  Moved unchanged to `backends/analytical.py` in M0; rewritten in M1.
- Probe `R4`. Rewritten against the corrected domain, and it becomes one of the more
  valuable probes rather than one of the weaker ones.
- The single-tier assumption throughout.

---

## 1. Charter

### The thing itself

A harness that exposes a **realistic-scale SCION network** to a **centrally deployed AI
recommendation node**, gives that node raw access and real tools, lets its decisions
actually change the network, and measures everything that happens including the AI's own
operational behaviour.

The nearest analogues are agentic harnesses built for human–environment–AI loops. This is
the same shape with the human removed from the loop: the model acts on the environment
directly, and the human observes the interaction rather than participating in it.

### Four front-ends, one substrate

| front-end | question it answers | milestone |
|---|---|---|
| `conformance` | can this model represent what deployment requires? | M5 |
| `bench` | how does it score, reproducibly, against others? | M6 |
| `gym` | can a model be *trained* here? | M7 |
| `deploy` | can the same harness drive real SCION? | M9 |

They share the substrate so that a model trained in `gym`, checked by `conformance`, and
scored by `bench` runs in `deploy` unchanged. That property is the whole architectural
argument. Protect it.

### Why this does not already exist

Everything in the SCION space is a simulator, an emulator, or a supervised-learning
dataset. None of them expose the network *to an agent*, none of them charge for
information, none of them close the loop with a population of hosts acting on the advice,
and none of them measure the AI's own pathologies. We are not competing with them; we sit
on top and use them as fidelity tiers.

### Success criteria

1. A model author implements one Python class and gets a conformance report, a benchmark
   score, and a trainable RL environment, with no other integration work.
2. The oscillation result from the architecture proposal is demonstrated at realistic
   scale, not in a 6-path toy.
3. An LLM agent with tool access can run a full episode, and its memory behaviour,
   context degradation, and latency are measured rather than assumed.
4. One scenario definition runs at all four fidelity tiers with comparable metrics.
5. Someone outside the project uses it without asking us how.

### Explicit non-goals

- **Not a SCION implementation.** We call `ietf-scion-testbed` and `scion-dqn-sim`. We do
  not reimplement beaconing at packet level.
- **Not a packet simulator.** ns-3 and SEED exist. Our substrate is flow/fluid level with
  a calibrated link model, and tier 3 is where packet truth comes from.
- **Not a model.** The PAS oracle is a separate deliverable. This harness must be
  useful to someone who disagrees with our architecture entirely.
- **Not a human-facing tool.** Dashboards are for inspecting traces after the fact, never
  in the decision loop.

---

## 2. The corrected domain model

Everything in this section is load-bearing and some of it was wrong before.

### 2.1 What is static, what changes

**Static for the duration of a run:**

- the set of ASes and ISDs
- the physical inter-AS link topology
- link capacities, propagation delays, MTUs
- AS business relationships (core / parent-child / peering)

Real topology change is rare and planned. Model it as an **explicit scheduled event** in a
scenario file, never as background churn.

**Changing continuously:**

- **link state** — offered load, queueing delay, available bandwidth, loss. This is the
  fast dynamic and it is what the model is trying to predict.
- **path segment cryptographic material** — segments are beaconed, signed, carry an
  expiry, and are periodically re-beaconed. A refreshed segment can describe an identical
  interface sequence with new signatures and a new expiry.
- **path availability** — a segment can vanish from a path server's answer because of AS
  policy filtering, without the underlying link going anywhere.

### 2.2 The identity problem

This is the sharpest ML-relevant consequence of the correction, and the thing to get right.

A model maintains per-path state: estimates, histories, confidence. It keys that state on
something. If it keys on an identifier that changes when a segment is re-signed, then
every refresh cycle it silently discards everything it knew, while its outputs continue to
look plausible.

The substrate must therefore expose **both** identifiers on every path:

```python
path.structural_id  # hash of the ordered interface sequence — stable across re-signing
path.segment_id  # tied to the current cryptographic material — changes on refresh
```

and be configurable as to which one `path_id` aliases:

```yaml
identity_policy: structural | crypto_bound
```

A robust model works under both. A model keyed on `segment_id` collapses under
`crypto_bound` and looks fine under `structural`. Probe R4 runs both and compares. This
is a much better test than the one it replaces.

> **Open question Q1, unresolved.** Whether the real SCION path fingerprint is stable
> across re-signing. Ask the mentor. Until answered, both policies are supported and
> neither is default-blessed. Do not hardcode.

### 2.3 Scale

`realistic` is 2,000 ASes / 10,000 inter-AS links / 100–300 paths per source-destination
pair. Consequences that must shape M1:

- **No Python object per path in the hot loop.** State is numpy arrays indexed by integer
  ids. `PathRef` and friends are views constructed at the exposure boundary, not the
  storage format.
- **Path sets are materialised lazily per scope.** Nobody enumerates all paths.
- **Beaconing is not simulated per message.** Segment construction and expiry are
  event-scheduled; the beaconing *process* is abstracted, its *consequences* are not.
- **The step loop is event-driven.** Not per-tick-per-link.

### 2.4 The link performance model

Load to metric, per link, monotone in load, calibrated where possible against tier-0 data.

Starting form is BPR-style with a queueing tail; the exact functional form is an M1
decision that gets an ADR. What is not negotiable: **monotone non-decreasing cost in
offered load.** That property is what makes an equilibrium unique and is what R7 tests
models against. The substrate must have it or the test means nothing.

Background (non-PAS) cross-traffic is exogenous, diurnal, and seeded. The model can only
infer it, never read it.

---

## 3. Target architecture

```
                        ┌─────────────────────────────────────┐
                        │  conformance   bench   gym   deploy │   front-ends
                        └──────────────────┬──────────────────┘
                                           │   (front-ends depend downward only)
                        ┌──────────────────▼──────────────────┐
                        │           EXPOSURE LAYER            │
                        │  observation streams · tool API     │
                        │  cost + rate limits · raw event log │
                        │  action channel · budget enforcement│
                        └──────────────────┬──────────────────┘
                                           │
                        ┌──────────────────▼──────────────────┐
                        │              SUBSTRATE              │
                        │  topology (static) · segments (churn)│
                        │  link state · host population       │
                        │  traffic · clock · scenario engine  │
                        └──────────────────┬──────────────────┘
                                           │
                        ┌──────────────────▼──────────────────┐
                        │           BACKENDS (tiers)          │
                        │  0 replay · 1 analytical            │
                        │  2 dqn-sim · 3 real testbed         │
                        └─────────────────────────────────────┘

        INSTRUMENTATION taps every layer and is written to, never read by, the model.
```

### Package layout

```
src/scionarena/
  core/            substrate. numpy-backed. no front-end imports. mypy strict.
    topology.py      static AS/link graph, loaders
    segments.py      beaconing abstraction, expiry, re-signing, identity policy
    linkstate.py     load -> metrics, monotone, calibrated
    hosts.py         host population, sampling, traffic generation
    clock.py         simulated wall-clock, event queue, latency accounting
    scenario.py      scenario schema + engine (portable across tiers)
    trace.py         deterministic trace + hashing
  exposure/        the only thing a model ever touches
    contracts.py     PathModel, Capabilities, the data types
    tools.py         tool registry, schemas, cost model, rate limits
    streams.py       beacon / telemetry / path-server feeds
    budget.py        probe, wall-clock, compute budgets
    session.py       one model's view of one episode
    loading.py       a model enters as an import path      (ADR 0013)
    loop.py          the closed loop, fixed or agentic     (ADR 0019)
    precheck.py      the thirty-second check, no substrate (ADR 0020)
  backends/        tier adapters behind one interface
    replay.py  analytical.py  dqnsim.py  testbed.py
  instrument/
    sampler.py       samples off the world's clock, and the truth to score against
    metrics.py       the registry: four families, plus a support count per family
    detectors.py     swing, oscillation, flap, convergence
    report.py        the interactive HTML view, hand-written SVG, no dependency
    figures.py       the PDF's charts (matplotlib, [report] extra, imported lazily)
  conformance/     probes, runner, report card      (M5)
  bench/           axes, sweep, results, score, report  (M6)
  gym/             gymnasium env, wrappers, vec     (M7)
  agent/           LLM loop, memory + context tests (M8)
  deploy/          production shim                  (M9)
```

### 4. The seams

Four boundaries. Each is a place where a future contributor should be able to substitute a
component without touching anything else. **Protecting these is more important than any
individual feature.**

**Seam A — substrate ↔ backend.** The substrate defines *what* happens; a backend defines
*how faithfully* it is computed. Same scenario, four tiers. Anything tier-specific that
leaks into `core/` is a bug.

**Seam B — substrate ↔ exposure.** The substrate holds truth in arrays. The exposure layer
decides what a model may see, at what cost, with what delay. **Any information a model
gets must pass through a tool or a stream.** No back door into `core/`.

**Seam C — exposure ↔ front-end.** All four front-ends drive the same session object. A
front-end that needs new substrate access asks for a new *tool*, not a new import.

**Seam D — model ↔ everything.** The model implements `PathModel` and calls tools. It never
imports `scionarena.core`. Enforced by an import-linter rule in CI, not by convention.

---

## 5. What we reuse, and how

We do not reinvent SCION. Four upstreams, each used for exactly what it is best at.

| upstream | what we take | how | risk |
|---|---|---|---|
| **ietf-scion-testbed** | a real 12-AS SCION stack; `linkd` REST link shaping; ID-INT tracing; border-router RTT/traffic metrics | tier-3 backend. `linkd` is what makes scenarios portable to real hardware. | needs a Proxmox host we may not have |
| **scion-dqn-sim** | BRITE topology generation; simulated beaconing and segment registration; six baseline selectors | tier-2 backend and a topology source for M1 | 4 commits, WIP, pin a hash, expect to fork |
| **ScionPathML** | real SCIONLab measurement traces; QoE profiles; five open-loop task definitions | tier-0 replay; **calibration of the tier-1 link model**; their tasks wrapped as a bench category | small, homogeneous, 4 ASes only |
| **SEED emulator** (vendored) | container-based SCION; already in the repo | alternative tier-3 if testbed hardware is unavailable | heavier than linkd for shaping |
| **Gymnasium** | RL API, vector envs, wrappers | `gym/` front-end | none |
| **CAIDA AS-relationships** | realistic AS-level graph structure at 10^4 scale | M1 topology generator input | not SCION; needs mapping to ISD/core structure, and that mapping is an assumption |

**Rule.** If an upstream does the job, depend on it or vendor it with a pinned hash and a
`VENDORED.md` entry saying what was changed and why. Reimplementing gets an ADR
justifying it. "It was easier" is not a justification; "the upstream cannot express X and
here is the X" is.

---

## 6. Roadmap

Ten milestones. M0–M4 are strictly sequential. M5/M6/M7 parallelise after M4. Full specs
in `docs/milestones/`.

**Landed: M0 through M3. M4 in progress.** The substrate beacons, composes paths and
carries load at the realistic tier; models reach it only through costed, rate-limited
tools; and the loop is closed. `scionarena demo` and `scionarena ui` produce the M3 result
in one command.

M3's "known limitation" -- series sampled once per decision round, so a model whose rounds
get more expensive hands the detectors a grid that is not uniform -- is closed.
`instrument/sampler.py` takes samples from a substrate tap on multiples of absolute
simulated time, so an overrunning round costs the model its slot and costs the grid
nothing, and the fast band is defined in decision rounds rather than in cycles per sample
so that the rate can change without the measurement changing. [ADR
0011](adr/0011-sampling-off-the-worlds-clock.md) has the decision and the sweeps; one of
them does not reproduce M3's dominance column and says so. The rest of M4 -- trace,
metric registry, the remaining detectors, the viewer -- is still open.

**CI was red from M0 until M4 and M0's gate says "CI green", so read that tick with the
correction attached.** Every run failed on `mypy`, which was pinned to `python_version =
"3.11"` and therefore parsed the 3.12 and 3.13 runners' numpy stubs under 3.11 rules and
died inside `numpy/__init__.pyi` before reaching any of our code. Nothing after that step
had ever run on any leg. Clearing it exposed three more, all of them measurement bugs
rather than substrate bugs, and all three are written up where they live: a wall-clock
budget asserted in `pytest` that measured the runner (`tests/test_performance.py`), a
pinned conformance digest that hashed the interpreter's float-summation strategy
(`tests/test_trace.py`), and a performance gate compared against a three-milestone-stale
baseline recorded on another interpreter (`benchmarks/run.py`). The lesson is the cheap
one: a gate nobody has watched fire is not a gate.

Two things measured at the realistic tier that the next milestone inherits. First, an
episode is bounded by memory rather than by the clock: 257 MiB per decision round for the
stochastic model against 21 MiB for the greedy one, so 120 rounds over 100 scopes wants
~34 GiB. That is invariant 1 working as specified -- nothing is summarised, so nothing can
be dropped -- but it means the retention policy is a design question M4 has to answer
rather than inherit, and the model that explores pays for it first. Second, the cost of a
model's own turn is worth watching in a profiler and not just in a budget: the reference
model spent minutes per round on a scan that was quadratic in the size of the network,
which nothing in the suite noticed because every test that would have caught it ran at the
smoke tier. Both are written up in `docs/milestones/M3.md`.

| | milestone | one-line goal | gate |
|---|---|---|---|
| **M0** | foundation reset | repo restructured, existing tests green in new layout | CI green, no behaviour change |
| **M1** | substrate at scale | static topology + crypto churn + link state at 2,000 ASes | build <10 s, step <10 ms, deterministic |
| **M2** | exposure layer | tools, costs, rate limits, raw log, budgets | a model can learn *only* through tools |
| **M3** | closed loop | host population, advice → traffic → state | oscillation reproduced at realistic scale |
| **M4** | instrumentation | event trace, metric registry, pathology detectors | detectors fire on synthetic positives |
| **M5** | conformance v2 | R1–R10 ported, R4 rewritten, AI-behaviour probes added | matrix discriminates; R4 catches identity amnesia |
| **M6** | benchmark | scenario suite, scoring, reproducible result files | two machines produce identical scores |
| **M7** | RL environment | Gymnasium API, flat + tool action spaces | a PPO baseline learns something |
| **M8** | agent harness | LLM loop, memory / context-rot / latency tests | context-rot curve produced for a real LLM |
| **M9** | fidelity ladder | tiers 2 and 3 validated, deploy shim | one scenario, three tiers, comparable metrics |

**Do not build ahead.** The seams stay clean by being respected while each layer is still
small enough to change.

---

## 7. What gets measured

Three families. M4 builds the registry; M5–M8 populate it.

**Task performance** — prediction accuracy (pinball, CRPS, calibration), decision quality
(utility vs the perfect-information bound), efficiency vs the capacity-proportional floor.

**Operational behaviour of the AI itself.** This is the part nobody else measures.
Decision wall-clock and its distribution. Tool calls per decision and information gained
per unit cost. Probe budget consumed. Memory footprint and what the model chose to
retain. Behaviour when the budget is exhausted or the deadline is missed.

**Pathologies.** Oscillation index (spectral peak dominance in the fast band, excluding
slow environmental drift). Herding (modal-path share vs host count). **Context rot**
(performance as a function of history length, with distractor injection). **Overfitting**
(train/eval split across disjoint topology partitions and traffic regimes). **Identity
amnesia** (state loss across re-signing). Staleness misuse (confidence that fails to decay
with information age).

---

## 8. Open questions

Live list in `docs/OPEN_QUESTIONS.md`. These four block design decisions and should go to
the mentor early.

**Q1 — Is the SCION path fingerprint stable across re-signing?** Determines whether
identity amnesia is a real deployment risk or only a hypothetical. Blocks the default for
`identity_policy`. Both are implemented regardless.

**Q2 — Is offered load per link observable or estimable in real SCION?** R6 and the whole
demand-conditioning argument assume yes. If no, the response model conditions on a latent
proxy and several probes need redesigning.

**Q3 — What is realistic scale for a deployed SCION ISD?** The `realistic` tier is
currently a guess. It sets performance budgets throughout `core/`.

**Q4 — Do we have tier-3 hardware?** Without a Proxmox host, `ietf-scion-testbed` is
unreachable and the ladder tops out at tier 2, which makes the sim-to-real claim
theoretical rather than demonstrated.

Two more worth raising: what the realistic SCMP probe rate limit is (it sets the tool cost
model), and how many hosts realistically share a source-destination pair (the independent-
sampling argument scales as `1/√N` and is much weaker at N≈10 than at N≈1000).

---

## 9. Notes to the builder

**On scope.** The vision is large; the v0 is not. Every milestone's job is to leave a
*port* where the ambitious version plugs in later. A tool registry with three tools is
fine if adding the fourth requires no changes elsewhere. A metric registry with six
metrics is fine on the same condition. Judge each milestone on whether the next one gets
easier, not on how much it does.

**On the temptation to build the model.** Do not. The harness must be useful to someone
who thinks our architecture is wrong. Reference models exist to exercise the harness, and
they are deliberately simple. If a reference model starts looking like a research
contribution, it has grown past its job.

**On honesty in artefacts.** The existing repo marks unvalidated adapters as unvalidated
and records the two bugs the discrimination test found in its own reference implementation
(`docs/DESIGN.md` §6). Keep that standard. Something written down as broken is worth more
than something quietly assumed to work.
