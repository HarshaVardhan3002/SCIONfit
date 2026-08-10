# 0009 — Closing the loop: hosts, delay, and realised load

Status: accepted. Date: M3.

## Context

Up to M2 the model's advice went nowhere. `record_advisory` accepted a distribution,
remembered it, and used it to decide which paths telemetry covered. Nothing about the
network changed. That is enough to test the exposure boundary and useless for the question
the project exists to ask, which is what happens when the recommender's own advice is the
dominant input to the state it is recommending against.

M3 has to decide four things: who turns advice into traffic, when advice takes effect, what
the load a path carries actually is, and how "this is oscillating" is measured without
also flagging a model that is correctly tracking a moving environment.

## Decisions

**1. Hosts are counts, not objects.** `core/hosts.py` holds one `ScopeState` per (src, dst)
scope with numpy arrays over its path set, and a population is a number. Sampling ten
thousand hosts is one `rng.multinomial` draw, not ten thousand loops. At the realistic tier
with a hundred concurrent scopes, a Python object per host is a million objects per step and
the step budget is 10 ms.

This is not only a performance decision. The `1/√N` concentration that the whole
stochastic-advisory argument rests on *is* the multinomial variance, so drawing the counts
directly makes the property exact rather than approximated by a loop that happens to
reproduce it.

**2. Advice takes effect at `now + decision_latency`, scheduled on the clock.** Publishing
does not change the world; it schedules an `advisory_apply` event. The delay is the
simulated time the model spent deciding — which is the wall clock its own tool calls
charged it, plus any latency the scenario injects — so a model that probes more is applied
later, against a network that moved further. Invariant 3 stops being a rule someone has to
remember and becomes the only path the code offers.

`Clock.at` already refuses to schedule into the past, so back-dating an advisory onto the
state it was computed from raises rather than quietly producing a better-looking result.

**3. Offered load is recomputed from scratch every step, never accumulated.** Each step the
population resamples, the per-path host counts become Mbps, and the scope's contribution is
scattered onto its paths' egress interfaces with one `bincount`. The substrate then writes
`hosts + scheduled surges` into `LinkState` as a whole array. Incremental add/subtract would
drift, and a drift of a few Mbps per step over a 3,600-second run is a bug that looks like
a finding.

**4. Defectors are a fixed fraction that ignores the advisory and plays greedy.** They pick
the currently cheapest path by the link state at the *start* of the step — what they could
have observed — and all of them pick the same one. The hook is `defector_fraction` and
`defector_kind`; only `"greedy"` exists now and the PAS v3 taxonomy fills the rest in M6.
A defector population is the cheapest available demonstration that a correct advisory is
not sufficient, because the compliant hosts sample a distribution that is optimal against
a demand that is not the demand.

**5. The oscillation index is spectral peak dominance in a fast band.** `instrument/
detectors.py`, ported from the proposal's toy: de-mean, take the power spectrum of a load
series, discard frequencies below `f_min`, and report the largest bin as a fraction of the
band's total power. Three things move a load series and the measure has to separate them:

| what moves it | spectral signature | index |
|---|---|---|
| environment drift the model is tracking | low frequency | excluded by `f_min` |
| independent host sampling | flat across the band | ~`1/n_bins` |
| a control-loop stampede | one sharp peak | ~1 |

The fast-band restriction is the whole point and it is the part that is easy to delete by
accident. An earlier version of this measure had no `f_min` and scored a model that
correctly followed a diurnal cycle as badly as one that flapped every other step. The
docstring says so, in those words, next to the constant.

## Consequences

- `Substrate` grows a `hosts` attribute and a `publish_advisory` method, and its `step`
  order becomes clock, segments, hosts, links. Hosts must write demand before the link
  state's metrics are asked for or every scope reads last step's congestion.
- `Session.record_advisory` now publishes into the substrate. Telemetry's `share` field
  becomes the *realised* share the hosts sampled, not the intended weight, and the two
  differ by exactly the sampling noise the model has to cope with.
- A scope whose path set changed under it loses the weights naming paths that are gone.
  Under `identity_policy="crypto_bound"` that happens on every re-signing, and the
  population falls back to uniform over what remains. This is the M5 R4 failure made
  physical rather than merely reported, and `ScopeState.stale_weight` records how much of
  the advisory evaporated.
- Cost and latency are now the same currency. A model that spends 400 ms of probe budget
  has its advisory applied 400 ms late, so the M2 cost model has a consequence beyond
  running out of budget.

## Alternatives rejected

- **Per-host objects with individual state.** Needed if hosts differ within a scope in ways
  that matter. They do not yet: an SLA class and a defector flag partition the population
  into a handful of groups, and groups are counts. Revisit if M6's adversary taxonomy needs
  per-host memory, and pay for it then.
- **Applying the advisory immediately and modelling latency as an observation delay.**
  Equivalent for a single scope and wrong for many: the delays would not compose, and the
  interesting cross-scope behaviour is exactly the composition.
- **Measuring oscillation as the variance of the load series.** Cannot tell a stampede from
  a busy hour, which is the distinction the measure exists to make.
- **Letting hosts read the true link state to sample.** They sample the published advisory
  and nothing else. Hosts that could see the truth would make the recommender irrelevant,
  which is a fine network and not this experiment. Defectors are the deliberate, bounded
  exception, and they read only what a host could measure for itself.
