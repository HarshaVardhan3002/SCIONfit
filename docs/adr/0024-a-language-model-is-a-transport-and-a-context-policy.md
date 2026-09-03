# 0024 — a language model is a transport and a context policy

Status: accepted
Date: 2026-09-03
Milestone: M6 / M8 (Phase 5, item 2)

## Context

The headline comparison is a tool-using language model against classical models. Almost
everything it needs already exists: `ToolUsingModel` and `act`, the six tools, the budget,
the parity pair (ADR 0019), and — since ADR 0021 — a clock that charges the model for its
own deliberation. Two things do not.

**A language model reaches the harness over a network, and a benchmark cannot depend on
that.** An API call is nondeterministic, costs money per cell, needs a key that must never
reach a result file, and fails in ways that have nothing to do with path selection. A suite
that cannot run in CI without a key is a suite nobody re-runs.

**The interesting measurement is not the answer, it is what the model kept.** Invariant 1
says the harness never summarises: the model gets the raw event log and decides what to
retain. For a classical model that is a design detail. For a language model it is *the
experiment* — the log outgrows any context window inside an episode, and what the model
throws away determines what it can still see. Nothing in the harness measured it.

## Decision

**1. The language model enters through a transport seam.** `LanguageModelAgent` owns the
loop — subscribe, list scopes, query, probe, publish, retain — and delegates exactly one
decision per scope per turn to a `Transport`:

```python
class Transport(Protocol):
    def decide(self, prompt: Prompt) -> Turn: ...
```

`Prompt` is plain data: the scope, the paths, what the model chose to keep, and this turn's
new records. `Turn` is plain data: which paths to probe, what weights to publish, what to
keep, and why. Three implementations ship:

| transport | what it is | reproduces | needs a key |
|---|---|---|---|
| `scripted` | a deterministic policy written in Python | yes | no |
| `replay` | a recorded run, replayed from a JSONL file | yes | no |
| `http` | a real model over the Anthropic or OpenAI API | no | yes |

`scripted` is the default and is what CI runs. It is **not a language model and must never
be reported as one**: its architecture tag is `llm_scripted`, distinct from `llm_api`, and
the report groups by architecture, so the two cannot land in one row. What it is for is the
plumbing — that a model of this shape can be loaded by name, driven agentically, charged
for its thinking, scored on every family, and compared in a parity pair.

`http` uses the standard library only. No new dependency in `core/`, and none anywhere: a
benchmark that needed an SDK to run one architecture would have that SDK's version in every
result.

**2. Every API turn is recorded, so an API run replays offline.** `http` with `record_to`
writes one JSONL line per decision, keyed by a hash of the prompt. `replay` reads it back.
That is what makes an API result auditable after the fact: the numbers can be recomputed by
someone with no key, from the file, and any disagreement is a harness change rather than a
different sample from the model.

**3. What the model kept is measured, and the model reports it.** A new optional method:

```python
def report_state(self) -> Mapping[str, float]: ...
```

The driver calls it once at the end of an episode, files the result on
`LoopResult.model_report`, and it reaches metrics as `MetricInput.model`. Three operational
metrics read it — `context_bytes`, `context_retained`, `context_evictions` — and return
`None` for every model that does not implement the method, which is the existing convention
for a metric with no input.

It is `getattr`-optional rather than part of `PathModel` because a model that keeps nothing
should not have to say so, and because the contract is public API after M6.

**4. The context policy is a constructor argument, so it is a variant.** `retain="recent"`
drops the oldest records, `retain="per_path"` keeps the newest few per path, `retain="none"`
carries nothing between turns. Three variants of one architecture, which is what makes the
context-rot question answerable by a sweep rather than by an argument.

## Consequences

- A sweep can include `llm` today with no key and no network, and every seam the API model
  will use is exercised.
- An API run costs money per cell and does not reproduce. `think="measured"` makes its
  latency real and its trace machine-dependent; the report already says so.
- `report_state` is new optional surface on the model side. Nothing calls it in a way that
  can change a run: it is read after the last turn, and what it returns reaches metrics, not
  the model.
- The key is read from the environment, is never written to a result file, and is never
  included in a recorded prompt.

## Alternatives rejected

**Ship only the HTTP transport.** Honest and unrunnable: no CI, no test, and the first time
anything downstream broke would be an hour into a paid sweep.

**Make the scripted transport pretend to be the language model.** It would let the suite
print an `llm` row with no key, and that row would be quoted. A benchmark whose headline
number can be produced without the thing it is measuring is worse than one that has no row
at all.

**Put context management in the harness.** It is invariant 1, and it is the measurement.

**Measure context by counting the session log.** The log is what the harness produced, not
what the model kept, and the difference between those two is the entire question.

**An SDK dependency for each provider.** Two SDKs, two version pins, and a result file whose
numbers depend on which minor version of a vendor client was installed. The wire format is
a POST with a JSON body.
