# Loading your model

The harness runs models it did not write. You do not register anything, subclass anything,
or package anything: you name your model with an import path and it is loaded.

```
scionarena models ./my_model.py:MyModel        # does it load? what will it be tested on?
scionarena conformance check ./my_model.py:MyModel
scionarena demo --models "./my_model.py:MyModel,reference"
scionarena ui                                  # the same string, in the models field
```

A **spec** is one of three things, and everything in the harness accepts all three:

| spec | when |
|---|---|
| `minrtt` | one of the built-in models — `scionarena models` lists them |
| `mypackage.mymodule:MyModel` | your code is installed in this environment (`pip install -e .`) |
| `./my_model.py:MyModel` | you have a single file and have not packaged anything |

The part after the **last** colon is the name of a class, a factory function, or an
already-built instance. A class or factory is called with no arguments; if yours needs
some, either give it defaults or point the spec at a small factory that supplies them.

Loading runs your module in this process, exactly as `import` would. Do not load a spec
you did not write or do not trust.

---

## A conforming model

Fifteen lines, no dependencies, and the harness will run it against everything:

```python
from scionarena.exposure.contracts import Advisory, Capabilities, Dist, Prediction

class MyModel:
    capabilities = Capabilities(name="MyModel", version="0.1.0")

    def reset(self, topo, seed=0):
        self.mean = {}

    def observe(self, obs, topo):
        for o in obs:
            if o.latency_ms is not None:          # None means not measured, not zero
                prev = self.mean.get(o.path_id, o.latency_ms)
                self.mean[o.path_id] = 0.8 * prev + 0.2 * o.latency_ms

    def predict(self, topo, paths, horizon_s=0.0, demand=None):
        return {
            p.path_id: Prediction(
                latency_ms=Dist.point_estimate(self.mean.get(p.path_id, 50.0)),
                throughput_mbps=Dist.point_estimate(100.0),
                loss=Dist.point_estimate(0.0),
            )
            for p in paths
        }

    def advise(self, topo, paths, sla, n_hosts=1):
        best = min(paths, key=lambda p: self.mean.get(p.path_id, 50.0))
        return Advisory(weights={best.path_id: 1.0}, reason="lowest mean latency")
```

The lifecycle is `reset` once, then `observe` / `predict` / `advise` per round.

Two things are worth getting right in `observe`. A metric of `None` means *not measured*,
which is not the same as measured-and-zero — probe R3 checks you tell them apart. And
`topo` is a fresh snapshot every call, so interfaces and paths you have never seen will
appear; raising on one is a recorded outcome, not a harness bug.

`examples/my_model.py` is a complete, deliberately mediocre model you can run today.

---

## Declaring what you do

`Capabilities` is your own account of your model, and it changes how you are scored.
Everything defaults to `False`, which is always a safe declaration.

```python
capabilities = Capabilities(
    name="MyModel",
    version="0.1.0",
    authors="you <you@example.org>",
    distributional=True,          # predict() returns quantiles, not just a mean
    demand_conditioned=True,      # the `demand` argument changes the answer
    emits_assignment=True,        # advise() spreads load rather than ranking
    notes="Quantile regression per path, entropy-regularised assignment.",
)
```

The rule that matters:

- **Declare `False` and fail the probe** → `DECLARED_ABSENT`. Not a defect. A pure
  forecaster is a legitimate thing to study and the report says what it does not do.
- **Declare `False` and pass anyway** → `PASS`, with a note suggesting you update the
  declaration.
- **Declare `True` and fail** → `FALSE_CLAIM`. This is the only outcome scored as worse
  than a plain failure, because a wrong declaration corrupts every comparison downstream
  that trusted it.

So declare what your model does, not what you would like it to do. `scionarena models
<spec>` prints exactly which probes your declaration turns on and which it turns off,
without running anything — check it before a long run rather than after one.

---

## When it will not load

Every failure names the spec, the problem and the fix. The ones worth expecting:

| message | what to do |
|---|---|
| `no module named 'mypackage'` | `pip install -e .` from your package root, in *this* environment |
| `module ... has no attribute 'MyModell'` | it offers the closest names in that module |
| `cannot be constructed as given: missing a required argument: 'k'` | give `k` a default, or point at a factory |
| `does not implement PathModel: missing or uncallable: advise` | the four methods are `reset`, `observe`, `predict`, `advise` |
| `capabilities is missing staleness_aware, ...` | use `scionarena.exposure.contracts.Capabilities`; a missing flag reads as `False` and silently costs you probes |
| `running ./my_model.py raised ImportError: ...` | your file fails before the harness reaches the model |

None of these can happen mid-run: the check is at load time, so a wrong spec costs you a
second rather than the length of a scenario.

---

## Models that use tools

A model that drives itself — an agent that queries, probes and publishes on its own
schedule — additionally implements `act(session, deadline_s)` and declares `uses_tools=True`.
It still implements the four methods, so the same object can be compared against a
fixed-cycle model. `scionarena.reference.agents:BudgetedProber` is a worked example;
it takes its scopes at construction, so load it with arguments:

```python
from scionarena.exposure.loading import load_model

model = load_model("scionarena.reference.agents:BudgetedProber", args={"scopes": [("1-ff00:0:1", "1-ff00:0:9")]})
```

Every tool call costs budget and can be refused. A refusal is a returned result, never an
exception, and handling it is part of the job rather than an error path.

---

See ADR [0013](adr/0013-a-model-enters-as-an-import-path.md) for why loading works this
way, and `src/scionarena/exposure/contracts.py` for the full types.

---

## The models that ship

`scionarena models` lists them. Every one is loadable by its bare name, and every one takes
constructor arguments through `--arg name=value`, which is what makes a *variant* of an
architecture expressible without writing a class.

| name | architecture | what it is |
|---|---|---|
| `ema` | `ewma` | exponentially weighted nowcast. A mandatory baseline |
| `minrtt` | `heuristic` | pick the lowest measured latency, always |
| `proportional` | `static` | split by declared capacity and ignore every measurement |
| `reference` | `stochastic` | the three-block reference: estimator, demand response, entropy-regularised assignment. Passes every probe, and exists to show the suite is satisfiable |
| `prober` | `stochastic` | the agentic reference: subscribes, round-robins probes across border routers, backs off on rate limits |
| `gbdt` | `gbdt` | histogram gradient boosting, **refitting inside the loop** from its own observations. Needs the `[trees]` extra |
| `layered` | `layered` | ranks on the static layer and lets congestion widen the interval. `--arg discipline=false` gives the variant that ranks on lagged load, which is the model probe R11 exists to fail |
| `llm` | `llm_scripted` / `llm_api` | a tool-using language model over a transport seam. See below |

### The language model

```
scionarena bench --models llm                                    # offline, no key
scionarena bench --models llm --arg retain=per_path              # a different context policy
scionarena bench --models llm --arg transport=http --arg model=claude-opus-5
scionarena bench --models llm --arg transport=replay --arg path=runs/recorded.jsonl
```

Three transports, and which one you used is recorded on every cell through the architecture
tag:

| transport | what it is | reproduces | needs a key |
|---|---|---|---|
| `scripted` | a deterministic policy in Python | yes | no |
| `replay` | a recorded run, replayed from a JSONL file | yes | no |
| `http` | a real model over the Anthropic or OpenAI API | no | yes |

**`scripted` is not a language model and is never reported as one.** It is tagged
`llm_scripted` rather than `llm_api`, and the report groups by architecture, so the two
cannot land in the same row. What it is for is the plumbing: that a model of this shape
loads by name, drives itself agentically, pays for its own thinking, is scored on every
family and survives a parity pair. A benchmark whose headline row can be produced without
the thing it measures is worse than one with no row, because the row would be quoted.

`http` reads its key from `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and needs network egress
from **every worker process** in the sweep. Both are deliberate choices rather than
defaults. Point `--arg base_url=http://localhost:8000/v1/chat/completions` with
`--arg provider=openai` at any OpenAI-compatible server to run a local open-weight model
over the same path.

`--arg record_to=runs/recorded.jsonl` writes one line per decision, keyed by a hash of the
prompt. That is what makes a paid run auditable: the numbers can be recomputed by somebody
with no key, from the file, and any disagreement is a change to the harness rather than a
different sample from the model. The key never appears in that file.

### What it kept

`retain` decides what the model carries between turns — `recent`, `per_path` or `none` —
inside `context_bytes`. It is a constructor argument rather than a setting because the
context-rot question is answered by sweeping it. The harness never summarises for the model
(invariant 1), so what the model throws away is the experiment, and `context_bytes`,
`context_retained` and `context_evictions` are what it is measured on.
