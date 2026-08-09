# 0008 — Tool costs, rate limits, and the exposure boundary

Status: accepted. Date: M2.

## Context

Invariants 1 and 2 say the harness never summarises and every tool call is charged. Both
are easy to state and easy to violate by accident, because the convenient implementation of
"give the model a view of the network" is a dictionary that the harness keeps up to date.
That dictionary is a summary, it is free, and it makes the M8 memory experiments
meaningless before they are written.

M2 has to decide four things: what the model's view actually is, what a call costs, what a
rate limit is keyed on, and where the substrate stops being visible.

## Decisions

**1. The model's view is an accumulation of tool results, not a window onto the world.**
`Session.view()` returns a `TopologySnapshot` assembled from what previous tool calls
returned, timestamped with when they returned it. Nothing else writes to it. A model that
makes no calls sees the initial snapshot forever, including the parts of it that have since
become false. This is the mechanism behind the first acceptance criterion, and it is also
what makes staleness a property the model can be wrong about.

**2. Cost has three dimensions and every one of them is charged before the result is
returned.** `Cost(wall_clock_s, probe_units, nbytes)`. Wall clock advances the simulated
world — a 2-second bandwidth test happens against 2 seconds of network — so invariant 3 is
enforced by the same code path that charges for the call. Probe units are the scarce
resource; bytes are the pressure against reading everything.

The costs are differentiated on purpose, roughly two orders of magnitude across the probe
kinds:

| call | wall clock | probe units | bytes |
|---|---|---|---|
| `query_paths` | 2 ms + 0.1 ms/path | 0 | ~200/path |
| `probe_path` latency | one RTT | 1 | 128 |
| `probe_path` loss | 20 RTTs, floor 0.5 s | 20 | 2.5 KB |
| `probe_path` bandwidth | 2 s | 100 | 5 MB |
| `get_history` | 0.2 ms/event | 0 | ~event size |
| `subscribe` | 5 ms | 0 | charged per delivered event |
| `publish_advisory` | 1 ms | 0 | small |

If probing everything were affordable the budget reasoning we want to measure would not
exist. A flat cost per tool would have the same effect more quietly.

**3. A bandwidth test puts its own traffic on the path.** `probe_path(kind="bandwidth")`
adds load to the path's interfaces for the duration of the test and reads the result back
out of the loaded link state. Measurement disturbs the thing measured; a harness where it
does not teaches models a false lesson about the cost of looking.

**4. Rate limits are keyed on the thing that is actually limited.** SCMP is rate-limited by
the border router that answers, so the key is the path's first egress interface — one
router, one allowance — not the path, not the AS, and not the model. Path lookups are limited per destination AS,
because that is which path server answers. Two probes on two different paths that leave
through the same border router contend; the same probe through a different one does not.
Violations return a refused `ToolResult` with `error="rate_limited"` and a `retry_after_s`,
and are charged the wall clock of having asked. Silently dropping them, or silently
queueing them, would both hide the constraint we are trying to expose.

**5. Budget exhaustion is a condition, not an exception.** A call that cannot be afforded
returns `ok=False, error="budget_exhausted"` naming the dimension, and the session records
it. The episode continues. What a model does when it goes blind — keep publishing on stale
information, publish nothing, fall back to declared capacity — is one of the more
interesting things this harness can observe, and raising would delete it.

**6. Models are written against `exposure.contracts` and that module imports nothing.**
Rather than let `contracts` reference `Session` (which imports `core`), the model-facing
handle is a `SessionLike` protocol declared in `contracts` itself. `Session` satisfies it
structurally. This is what makes "no model can reach `core/`" provable by import-linter
rather than by convention: the module a model imports has no edge into the substrate at all.

**7. Streams deliver raw records at substrate-step granularity and charge on delivery.**
A subscription accumulates `RawEvent`s — re-signed segments, per-path telemetry samples,
path-server changes — with no windowing, no averaging and no derived fields. Bytes are
charged as they arrive, which is what "ongoing bandwidth" means. Telemetry only covers paths
that carry traffic, so what a model sees is biased towards what it recommended. That bias is
in the problem and is not corrected here.

## Consequences

- Adding a tool means one entry in `TOOLS` and one test. The registry is data, and
  `Session` never names a tool.
- The tool schemas are literal JSON Schema and are emitted directly as Anthropic
  `input_schema` or OpenAI `function` definitions, so an LLM agent needs no adapter.
- The tool log is complete enough to replay: same scenario, same seed, same calls in the
  same order reproduces the session state hash. M4's operational metrics and M8's memory
  tests both read this log.
- Charging the model's own thinking time is **off** by default (`charge_real_time=False`),
  because real elapsed seconds are not reproducible and invariant 4 is about the substrate.
  `Stopwatch(fixed_s=...)` gives tests a deterministic nonzero decision latency, and `gym`
  and `bench` can turn the real thing on when the point is realism.

## Alternatives rejected

- **A single scalar cost per call.** Simpler to explain, but it collapses the trade-off
  between a cheap echo and an expensive bandwidth test, which is the trade-off.
- **Rate limiting per model.** Easy, and wrong: it makes the limit a property of the agent
  rather than of the network, so a model cannot learn to spread its probes across border
  routers, which is the actual mitigation available to it.
- **Raising on exhaustion.** Would make the interesting behaviour unobservable.
- **A live view object that the harness keeps fresh.** Convenient for the reference models,
  fatal to invariant 1.
