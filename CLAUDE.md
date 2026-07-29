# CLAUDE.md — operating manual for the builder

You are building **scionarena**: a harness that exposes a realistic SCION network to a
centrally deployed AI recommendation node, and measures what happens.

Read `docs/HANDOFF.md` before your first change. Read the milestone spec in
`docs/milestones/` for whatever you are working on. Do not start a milestone whose
predecessors are unfinished.

---

## What this is, in two paragraphs

Existing SCION tooling is simulators and emulators built for humans to look at. This is
not that. This is the environment side of an **AI–environment loop with no human in it**.
The model is given raw access: observation streams, probing tools with real costs, the
full unsummarised history, and an action channel whose output actually changes the
network. The user watches the model interact with the environment; the user is not in
the loop.

One substrate, four front-ends built on it: **conformance** (can this model represent
what deployment requires), **bench** (scored, reproducible, comparable), **gym** (an RL
environment with rich tool access), and **deploy** (the same harness pointed at real
SCION). They share the substrate deliberately. A model that trains in `gym` and passes
`conformance` and scores in `bench` should run in `deploy` with no code change. If a
design decision breaks that property, it is the wrong decision.

---

## Hard invariants

Break these and the project loses its point. If a task seems to require breaking one,
stop and write to `docs/OPEN_QUESTIONS.md` instead.

1. **The harness never summarises for the model.** No feature engineering, no rolling
   windows computed on the model's behalf, no "helpful" aggregation. The model gets the
   raw event log and decides what to keep. Memory management is the model's problem and
   testing it is the point.

2. **Every tool call has a cost and the cost is charged.** Probes consume bandwidth and
   are rate-limited. Queries take time. A model that probes everything must lose on
   budget what it gains in information.

3. **The network does not wait for the model.** Wall-clock is simulated but real. A
   decision that takes 800 ms is applied 800 ms late, against a network that moved. Never
   pause the world while the model thinks.

4. **Determinism from a seed.** Same seed plus same model plus same version means the
   same trace, byte for byte, for the substrate. Model nondeterminism is the model's; the
   world's is not permitted.

5. **The four front-ends share one substrate.** No forked world model per front-end.

6. **Probes and detectors must be able to fail.** A probe nothing fails measures nothing.
   Every new probe ships with a reference model that fails it, enforced in CI.

7. **Nothing in `core/` imports a front-end.** Dependency direction is
   `core <- exposure <- {conformance, bench, gym, deploy}`. Never the reverse.

---

## Domain facts that are easy to get wrong

These have already been got wrong once. Read them twice.

### Physical links are static. Cryptographic material is what churns.

The earlier version of this project assumed inter-AS links appear and disappear on the
timescale of an hour. **That is wrong.** Corrected model:

- The physical inter-AS link topology is **stable** for the duration of a run. Real
  topology changes are rare, planned, and modelled as explicit scheduled events, not as
  background noise.
- What refreshes constantly is the **beaconed path segment**: it is signed, it carries an
  expiry, and it is periodically re-beaconed and re-signed.
- A re-signed segment may describe **exactly the same sequence of interfaces** while
  carrying new cryptographic material and, depending on how the identifier is derived, a
  new identifier.

**Why this matters more than the old assumption did.** If a model keys its per-path
memory on a path identifier that changes on re-signing, it silently discards its entire
history every refresh cycle while appearing to work. Nothing in its outputs looks wrong.
That is a real, severe, and completely invisible failure mode, and probe `R4` exists to
catch it. See `docs/milestones/M5.md`.

Whether the SCION path fingerprint is stable across re-signing is listed in
`docs/OPEN_QUESTIONS.md` as Q1 and is **not yet resolved**. Build the substrate so both
behaviours are configurable (`identity_policy: "structural" | "crypto_bound"`) and test
both. Do not hardcode an assumption about it.

### Scale

12 ASes is a demo, not a test. Design targets, configurable, validated at each:

| tier | ASes | inter-AS links | paths per (src,dst) |
|---|---|---|---|
| smoke | 20 | 60 | 5 |
| dev | 200 | 800 | 30 |
| **realistic** | **2,000** | **10,000** | **100–300** |
| stress | 20,000 | 100,000 | 500+ |

`realistic` is the number that matters. Everything in `core/` must be array-backed and
must not hold a Python object per path in the hot loop. Path sets are materialised lazily
per scope.

### Other corrections carried forward

- Forward and reverse path availability differ. Maintain per-direction state.
- A path is observed only while some host is sending on it. Sparsity is the normal case.
- Observations are biased toward paths the model recommended. That bias is a feature of
  the problem, not a bug in the generator. Do not correct it in the substrate.

---

## Working conventions

**Language and tooling.** Python 3.11+. `ruff` for lint and format. `mypy --strict` on
`core/` and `exposure/`, best-effort elsewhere. `pytest`. No runtime dependency in
`core/` beyond `numpy`. Heavy optional deps go behind extras.

**Commits and PRs.** One milestone task per PR. Every PR states which milestone and task
it closes, and which acceptance criterion it satisfies. A PR that satisfies no stated
criterion is out of scope; open an issue instead.

**Tests.** Every new module ships tests in the same PR. Regression tests name the bug they
prevent. Example, already in the repo:
`tests/test_world.py::test_no_universal_interface` names the seeds that originally broke.
Follow that pattern; a test whose failure message does not tell you what broke is half a
test.

**Performance.** Any change to `core/` runs the benchmark suite. Regressions beyond 15%
on the `realistic` tier fail CI. Numbers live in `benchmarks/baseline.json`.

**Documentation.** An architecture decision goes in `docs/adr/NNNN-title.md` before the
code that implements it. Keep them short: context, decision, consequences, alternatives
rejected. If you find yourself explaining a decision in a code comment more than three
times, it wanted an ADR.

**Naming.** `scionarena` is the umbrella package. `scionfit` remains the conformance
front-end inside it. Both names are cosmetic and changeable up to M6; after M6 they are
public API.

---

## Definition of done, for any change

- [ ] `ruff check` and `ruff format --check` clean
- [ ] `mypy --strict` clean on `core/` and `exposure/`
- [ ] `pytest` green, new code covered
- [ ] performance suite within budget on `realistic`
- [ ] determinism test passes: same seed produces an identical trace hash
- [ ] the cross-model discrimination matrix still discriminates
- [ ] docs updated: milestone checklist ticked, ADR written if a decision was made
- [ ] no new dependency in `core/` without an ADR

---

## When you are uncertain

The failure mode to avoid is inventing a SCION fact and building on it. Several such
inventions have already had to be unwound.

- **A SCION protocol detail you are unsure of** → do not guess. Add it to
  `docs/OPEN_QUESTIONS.md` with the specific question and what you assumed provisionally,
  make the assumption configurable, and continue. Flag it in the PR description.
- **A design choice with two defensible answers** → write the ADR with both, implement the
  one that is easier to reverse, and say so.
- **Something that would break an invariant** → stop, write it up, do not work around it.

Mark anything you could not verify with `# ASSUMPTION(Qn):` referencing the open question
number. A grep for `ASSUMPTION(` must return an accurate list of everything the project is
standing on that has not been checked.

---

## Milestone order

```
M0  foundation reset          M5  conformance v2
M1  substrate at scale        M6  benchmark
M2  exposure layer            M7  RL environment
M3  closed loop               M8  agent harness
M4  instrumentation           M9  fidelity ladder + deploy shim
```

M0 through M4 build the substrate and are strictly sequential. M5, M6, M7 are front-ends
on it and can proceed in parallel once M4 lands. M8 depends on M2 and M4. M9 depends on
everything and needs hardware access that may not exist yet.

Do not build ahead. The single most valuable property of this codebase is that the seams
described in `docs/HANDOFF.md` §4 stay clean, and they stay clean by being respected while
each layer is small.
