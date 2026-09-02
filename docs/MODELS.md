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
