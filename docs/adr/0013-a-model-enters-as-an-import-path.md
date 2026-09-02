# 13. A model enters the harness as an import path

Date: 2026-09-02 · Status: accepted · Amends [0008](0008-tool-costs-rate-limits-and-the-exposure-boundary.md)

## Context

Every model the harness has ever run came out of `REFERENCE_MODELS`, a four-entry
dictionary in `scionarena.reference.models`. `conformance check` could take a
`module:Attr` string, but that path was six lines long, existed in one front-end only,
and reported a failure to load as whatever exception Python happened to raise —
`ModuleNotFoundError`, `AttributeError`, or a `TypeError` from deep inside somebody's
`__init__`. Nothing checked the object against `PathModel` before a run began, so a model
missing `advise` failed several seconds into a scenario, from inside the driver.

The project's stated purpose is comparative evaluation of models the authors of the
harness did not write. There is currently no supported way for one to enter.

## Decision

**A model is named by an import path: `mypackage.mymodule:MyModel`.** The user installs
their code into the same environment (`pip install -e .`), passes the string on the CLI or
types it into the field in the UI, and the harness imports the module, takes the
attribute, instantiates it if it is a class, and hands it to the driver. There is no
plugin manifest, no entry-point group, no registration decorator, and no sandbox.

This is what `lm-eval-harness` and `evaluate` do, users already know the shape, and it
costs a model author nothing: an existing scikit-learn wrapper or LLM client becomes
loadable by having the four methods and a `capabilities` attribute. The built-in names
(`minrtt`, `reference`, …) become aliases *for import paths* rather than a separate
lookup, so there is exactly one resolution rule and the reference models go through the
same door as everybody else's.

**A single file is also a spec.** `./my_model.py:MyModel` reads the part before the colon
as a path when it ends in `.py`, executes the file, and takes the attribute. Somebody with
one script and no `pyproject.toml` can be measured without first learning to publish a
package, which is the entire barrier this decision exists to remove. The file is executed
exactly as an import executes it, with exactly the same consequences.

**Loading is one function, in `exposure`.** `exposure.loading.load_model` is above
`core` and below every front-end, so `conformance`, `bench` and the UI share it without
importing one another, which the layers contract forbids.

**It checks conformance structurally, at load time, and every failure is a sentence.**
Missing module, missing attribute (with the closest name in that module offered),
constructor that needs arguments (with the required parameters named), an exception raised
during construction, a method that is absent or is not callable, a `capabilities` object
missing flags — each is a `ModelLoadError` naming the spec, the problem and what to do.
The check is structural rather than `isinstance(obj, PathModel)`: the protocol is
`runtime_checkable`, but its answer is one bool, and "your model is not a PathModel" tells
an author nothing they can act on.

**Declared capabilities are reported before the run, not after it.** `capability_report`
turns a `Capabilities` into the list of probes that will be run against the model and the
probes that will be recorded as `DECLARED_ABSENT`, and says which is which. The
declaration is what the user controls, so it is the thing to show them while they can
still change it.

## Consequences

`scionarena models <spec>` loads a model and prints what it will be tested on, without
running a scenario — the fastest possible check that an integration is right. `demo` and
the UI take specs wherever they took reference names, so the comparison the project exists
to make can be run against an outside model today, before `bench` exists.

Two models can now declare the same `capabilities.name`, and the demo keyed its figure
series on that name. Labels are disambiguated by spec when they collide; without that,
two runs silently merge into one line.

A load executes the user's module in this process, with this process's privileges. That is
inherent in the decision and worth saying plainly rather than implying otherwise by
silence: the harness is a local research tool, and loading a model is as trusting as
`import`. A spec from an untrusted source is not safe to load, and no wording in an error
message changes that.

The capability-to-probe table lives in `exposure.loading` as static data, because
`exposure` may not import `conformance` to ask the probe registry directly.
`tests/test_loading.py::test_the_capability_table_still_agrees_with_the_probe_registry`
fails if a probe gains, loses or renames a capability without the table following.

## Alternatives rejected

**An entry-point group in `pyproject.toml`.** Discoverable — `scionarena models` could
list every installed model without being told any names. It also requires the author to
package and reinstall before a run, which is exactly the friction that stops somebody
trying the harness on the model they have open in an editor. Aliases give the listing for
the models we ship; an import path costs an outside author nothing.

**A subprocess or container per model.** Real isolation, and it buys crash containment we
do not currently need and a per-call boundary the tool protocol would have to cross. The
substrate hands a model a live `SessionLike` whose calls move simulated time; putting a
process boundary in the middle of that is a design for `deploy` (M9), not for loading.

**Requiring `isinstance(model, PathModel)`.** One line, and it accepts a class with the
four method names attached to something that is not a model, while rejecting an
almost-conforming model with a message that names nothing. The structural check finds the
same set and can say which method is missing.

**A registration decorator (`@scionarena.model`).** Couples the author's module to ours at
import time for nothing: the harness already knows the name, because the user typed it.
