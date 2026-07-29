# scionfit design note

**v0.1 · what was built, what was decided, and what is deliberately missing**

---

## 1. The claim

Existing SCION benchmarks answer *"is your prediction accurate?"*

`scionfit` answers *"does your model survive being deployed alongside other copies of itself?"*

Those are different questions. A model can score well on the first and be unusable in practice, because accuracy on a recorded dataset says nothing about behaviour in a loop where the model's own output changes the data.

The concrete case: a model that always names the single best path scores well on an offline recommendation benchmark and causes a stampede in deployment. Nothing currently in the SCION tooling can detect that before you deploy.

## 2. What already exists, and why we layer rather than compete

Three relevant systems exist, all newer than eighteen months, two of them from netsys-lab.

| | ScionPathML | scion-dqn-sim | ietf-scion-testbed |
|---|---|---|---|
| What it is | measurement toolkit + real dataset + 5 ML tasks | BRITE topologies + simulated beaconing + DQN selector | a real 12-AS SCION deployment on LXC |
| Real data | yes, 4 ASes × 4 weeks | no | yes, live |
| Control plane | no | simulated | real |
| Link shaping | no | in-model | **real, via `linkd` REST** |
| Many agents contending | **no** | **no** | not exercised |
| Closed loop | **no** | **no** | **no** |
| Model-agnostic interface | no, task-specific | no, DQN hardwired | n/a |

All five ScionPathML tasks are open-loop supervised learning: one model, one prediction, scored against recorded data, nothing acts on the prediction. scion-dqn-sim evaluates a single agent on a single source–destination pair.

Sharper than that: **ScionPathML's Task 4 scores a recommender by whether its top-ranked path met a QoE target.** That rewards precisely the concentration behaviour that causes herding. A model that learns to herd scores well on it. That is not a criticism of their work, which is solving a different and also necessary problem; it is the clearest possible statement of the gap.

So the decision was to layer. We call these tools rather than reimplement them, for two reasons. The weak reason is effort. The strong reason is that reimplementing SCION beaconing is exactly where domain guesswork would show, and a reviewer would rightly ask why we did not use the tools built by the people who deployed SCION.

## 3. The fidelity ladder

| Tier | Backend | Fidelity | Cost |
|---|---|---|---|
| 0 | ScionPathML dataset | real measurements, replayed | instant |
| 1 | built in | analytical link model | ms, thousands of runs |
| 2 | scion-dqn-sim | BRITE topology, simulated beaconing | minutes |
| 3 | ietf-scion-testbed | real SCION stack, shaped via `linkd` | hours, needs hardware |

**The design goal is that one scenario definition drives all four tiers.**

Tier 3 is what makes that goal worth pursuing. `linkd` applies `tc netem/tbf` to real inter-AS interfaces behind a REST API, so the same perturbation that degrades a link in the tier-1 analytical world can degrade a real link on real hardware. That converts the sim-to-real gap from an assumption into a measurement, and the sim-to-real gap was named in the architecture proposal as the largest single risk in the whole programme.

The dependency ordering matters: **the scenario schema has to be fixed before the adapters harden**, or portability is lost and we end up with four incompatible scenario formats. That is the first thing to settle in v0.2.

## 4. The interface

One class, four methods: `reset`, `observe`, `predict`, `advise`. Three rules governed the design.

**A twenty-line moving average must be able to implement it.** If only a large neural network can satisfy the interface, then the interface cannot express the baselines we need to compare against, and it cannot express the incumbent Path Oracle either. A benchmark that cannot represent the thing it is arguing against is useless. `EMAOracle` in `reference/models.py` is 60 lines including docstrings, and it reproduces the incumbent's behaviour.

**A model must be able to declare what it does not do.** A pure forecaster is a legitimate object of study. We need to run one and watch it fail, not reject it at the door. So `Capabilities` carries an explicit declaration and a model that honestly says `demand_conditioned=False` gets `DECLARED_ABSENT` on the relevant probes, with a note, not a failure.

**No dependencies.** The interface imports nothing outside the standard library. Implementing it costs a model author nothing and pulls no version conflicts into their project.

### The declaration asymmetry

| you declared | probe says | result |
|---|---|---|
| absent | fails | `DECLARED_ABSENT` — honest, recorded, not punished |
| absent | passes | `PASS` plus a note to update the declaration |
| present | fails | `FALSE_CLAIM` — treated as worse than failing |

Declaring a capability you do not have is the only thing scored worse than lacking it, because a wrong declaration silently corrupts every downstream comparison that trusts it. A missing capability is visible; a false one is not.

## 5. Why the probes are behavioural, not structural

The obvious way to check conformance is to inspect the model: does it have a graph layer, does it output quantiles. That approach fails immediately, because it privileges one implementation style and rejects anything written differently.

Every probe here instead changes exactly one input and observes the output. R1 does not ask whether the model contains a graph; it degrades one shared link and checks whether predictions move on the paths that use it and not on the paths that do not. A model can pass that with a GNN, with a hand-written composition rule, or with something nobody has thought of yet.

Two consequences follow, and both are deliberate:

**Probes never compare two models against each other.** Only one model against itself under a controlled change. That is what makes a result portable across model families and, later, across fidelity tiers.

**A probe that cannot fail is deleted.** `tests/test_conformance.py::test_probes_discriminate` asserts that the reference implementation and the incumbent get materially different scores. CI runs the full cross-model matrix on every push. If a change makes the probes stop discriminating, the build fails.

## 6. Two bugs the discrimination test found, in its own reference implementation

Worth recording, because it is the clearest evidence that the approach earns its keep. The first run of the cross-model matrix produced this:

```
                  R1    R2    R3    R4    R5    R6    R7    R8    R9   R10   verdict
ema             PASS  PASS  PASS  PASS    -     -     -     -   PASS    -    CONFORMANT
reference       PASS  PASS  PASS  PASS  PASS  LIED  LIED  PASS  PASS  PASS   MISDECLARED
```

Both results are wrong, and each exposed a real defect.

**The incumbent was scored CONFORMANT.** The verdict logic treated an honest `DECLARED_ABSENT` as non-blocking, so a model that cannot condition on demand, cannot emit a distribution and cannot handle staleness came out clean. Fixed: conformance now requires requirements to be *met*, not merely met-or-honestly-declined. An honest limitation is still a limitation.

**The reference implementation was flagged as lying about demand conditioning.** It was not lying; the tier-1 world generator was producing topologies where one interface lay on *every* path. On such a topology the total load on that interface is 1.0 regardless of how traffic is split, so the reference's max-over-links load measure went constant and a correct model looked demand-blind. Two fixes: the world now caps interface reuse so genuine alternatives always exist, and the reference blends its own share with the busiest link rather than using the maximum alone. `tests/test_world.py::test_no_universal_interface` is the regression test, and it names the seeds that originally broke.

The second bug is the more interesting one. It is a *benchmark* bug, not a model bug: a degenerate environment that makes a real capability untestable. That class of defect is invisible unless you deliberately try to make good and bad models come out different, which is the entire argument for building the discrimination test before building anything else.

## 7. What v0 deliberately does not do

**No closed-loop tier yet.** Many agents, contention, oscillation metrics, axiomatic scoring. That is v1. Conformance ships first because it is a prerequisite: there is no point running a model through the multi-agent tier if it cannot represent demand, because the outcome is a foregone conclusion. That is what the `OPEN-LOOP ONLY` verdict means and why it is a routing decision rather than an insult.

**The three external adapters are stubs.** Type mappings written, call shapes fixed, none validated against a live export or instance. `docs/ADAPTERS.md` lists the checklist per tier and says explicitly that no tier 0, 2 or 3 numbers should be published until it is done. The `linkd` endpoints in particular are inferred from a README and are certainly partly wrong.

**The tier-1 world is not a network simulator.** It models one thing carefully: cost rises with load, and paths sharing a link are coupled. That is enough to interrogate a model's structure and nowhere near enough to draw performance conclusions from. It is stated that way in the module docstring so nobody reports a number from it by accident.

## 8. v1: the closed-loop tier

The half nobody else has. Sketch, not commitment:

- N host agents per scope, each sampling independently from the published advisory
- realised load fed back through the environment, producing the next round of observations
- oscillation index (spectral peak dominance in the fast band, excluding slow environmental drift), path-switch rate, Jain fairness, modal-path share
- the five axioms from the Baumeister and Keshvadi line, computed as functions of agent count and contention
- baselines already present: min-RTT greedy, capacity-proportional, EMA incumbent
- **the central experiment**: same model with and without demand conditioning, in closed loop, showing whether the oscillation collapses

Only models that reach `PARTIAL` or `CONFORMANT` are worth running there. That is the conformance layer's job: it decides what is worth spending the expensive tier on.

## 9. Open questions

Ordered by how much they change the design.

1. **Is offered load observable in real SCION?** Requirement R6 and everything downstream of it assume we can observe or estimate demand per link. If that is fundamentally unavailable, R6 must be weakened to a latent proxy and probe R6 needs redesigning.
2. **How many hosts realistically share a source–destination pair?** The whole independent-sampling argument scales as `1/√N`. At N≈10 the argument is much weaker than at N≈1000, and the closed-loop tier's parameters depend on the answer.
3. **Should the tier-1 world be replaced by tier 2 entirely?** If scion-dqn-sim is solid enough to run in seconds, the analytical world becomes redundant and we lose a maintenance burden. Needs someone to clone it and find out.
4. **Do we wrap ScionPathML's five tasks, or leave them alone?** Wrapping Task 4 specifically is tempting, because showing its score next to R8 for the same model makes the gap concrete in one table. Risk: it reads as a criticism of their benchmark rather than a complement to it, and they are the people whose data we want to use.
5. **Is `linkd` accessible to us?** Tier 3 needs a Proxmox host. Without it the ladder tops out at tier 2 and the sim-to-real claim stays theoretical.

---

*v0.1 · 2553 lines including tests · MIT · no runtime dependencies · Python 3.10+*
