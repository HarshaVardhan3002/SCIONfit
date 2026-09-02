"""Copy this file. Change three methods. Delete the rest.

This is the worked example of the normal way into the harness: **an adaptor you
write**, sitting between the harness and whatever your model actually is. We
cannot know what we are dealing with -- a scikit-learn regressor, a torch module
wanting a batched tensor, an HTTP endpoint, a compiled thing with a C API -- and
guessing is how a harness grows a directory of half-working importers, one per
framework, each subtly wrong. So you write the fifty lines that only you can
write, and everything else is ours.

What this file wraps is deliberately awkward: ``BoxRegressor`` below takes a 2-D
array of floats and returns a 1-D array of floats. It knows nothing about SCION,
nothing about paths, nothing about time, and it has no notion of uncertainty. If
the adaptor pattern works for that, it works for most things.

Run it before you run anything else::

    scionarena adapt ./examples/adaptor_template.py:TemplateAdaptor

Seconds, no substrate. It calls every contract method against synthetic input and
tells you what would break in a sweep, which is otherwise something you find out
an hour in, from a traceback inside the driver.

Then, when the shapes are right::

    scionfit check ./examples/adaptor_template.py:TemplateAdaptor
    scionarena bench run --models ./examples/adaptor_template.py:TemplateAdaptor

The only import is ``scionarena.exposure.contracts``. That module imports nothing
from the rest of the project -- no numpy, no torch, no SCION library, and nothing
that can reach the substrate -- and import-linter proves it on every commit
(ADR 0008). Depending on it costs you nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scionarena.exposure.contracts import (
    SLA,
    Advisory,
    Capabilities,
    Demand,
    Dist,
    Observation,
    PathRef,
    Prediction,
    TopologySnapshot,
)

# --------------------------------------------------------------------------
# 1. The model you actually have. Delete this; import yours instead.
# --------------------------------------------------------------------------


class BoxRegressor:
    """A stand-in for something with no idea what a path is.

    Takes ``rows``: a sequence of equal-length float vectors. Returns one float
    per row. That is the entire interface, which is roughly the interface most
    trained models present once the framework is stripped away.
    """

    #: What the columns mean, in order. Yours will differ; the point is that
    #: *something* has to write this down, and it is the adaptor's job.
    FEATURES = ("hop_count", "declared_latency_ms", "min_declared_bw", "last_seen_latency")

    def predict(self, rows: Sequence[Sequence[float]]) -> list[float]:
        # A stand-in for weights someone trained. Linear, deliberately dull.
        return [
            0.6 * row[1] + 8.0 * row[0] + 400.0 / max(1.0, row[2]) + 0.4 * row[3] for row in rows
        ]


# --------------------------------------------------------------------------
# 2. The adaptor. This is the part you write.
# --------------------------------------------------------------------------


class TemplateAdaptor:
    """Translate the contract into feature rows, and one float back into a path.

    Three things to get right, and they are the three the pre-flight checks:

    * ``predict`` returns a **mapping keyed by path id**, one entry per path it
      was handed. A list in path order is the most common mistake and it fails
      silently: the harness keys truth on the path id, so a list joins against
      nothing and every accuracy metric quietly reports nothing at all.
    * ``advise`` returns weights over **paths that were offered**. Weights naming
      anything else are dropped, so the advice published is not the advice given.
    * ``reset`` clears whatever you keep between rounds. A sweep runs the same
      object over many worlds, and state that survives a reset leaks one episode
      into the next -- which looks like a model that learns and is a bug.
    """

    def __init__(self, temperature: float = 1.0) -> None:
        # Constructor arguments come from the spec, so a sweep can vary them:
        #   scionarena bench run --models "./adaptor_template.py:TemplateAdaptor"
        # See ADR 0013 for the spec grammar.
        self.temperature = float(temperature)
        self.model = BoxRegressor()

        # ------------------------------------------------------------------
        # Declare what you do, and -- more usefully -- what you do not.
        #
        # An honest False is a *supported answer*: it yields DECLARED_ABSENT on
        # the probes that would have tested it, which is a recorded finding and
        # not a failure. A False claim is the one thing that produces a hard
        # FAIL, because the harness cross-checks every declaration behaviourally.
        # There is no advantage anywhere in claiming something you do not do.
        # ------------------------------------------------------------------
        self.capabilities = Capabilities(
            name="TemplateAdaptor",
            version="0.1.0",
            authors="you",
            # The grouping key for the architecture-then-variant comparison.
            # Use the name of the method, not of your model: two variants of one
            # architecture should share it so the report can ask whether the
            # difference between them helped.
            architecture="linear",
            # A point estimator. Saying otherwise would fail probe R5, and would
            # also make the coverage numbers meaningless, which is worse.
            distributional=False,
            # The regressor never sees demand, so its prediction cannot move
            # with it. Declaring True here would fail R6 on the first cell.
            demand_conditioned=False,
            monotone_in_demand=False,
            # It spreads traffic rather than picking one path. R8 records this.
            emits_assignment=True,
            # Features are read off the interfaces in the snapshot it is given,
            # so an interface first seen a second ago is as usable as any other.
            # This is what R4 and R2 ask about, and it is why the features above
            # are all per-path rather than per-path-*identity*.
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            reports_confidence=False,
            # It keeps a latency memo between rounds, and reset() clears it.
            # Note the default is True: a stateless model has to say so.
            stateful=True,
            notes="Wraps an array-in/array-out regressor. See examples/adaptor_template.py.",
        )

        #: The only state. Keyed by path id, cleared on reset.
        self._last_latency: dict[str, float] = {}

    # ---------------------------------------------------------------- lifecycle

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        """Start a fresh episode. Clear everything the last one left behind."""
        self._last_latency.clear()

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        """Ingest measurements. May arrive empty, and usually does.

        The harness never summarises for you (invariant 1): these are raw
        samples, sparse, biased toward the paths you recommended, and each field
        may be ``None`` meaning *not measured* -- which is not the same as
        measured-and-zero, and probe R3 checks that you tell them apart.

        Deciding what to keep is your problem, and testing that decision is a
        large part of what the benchmark is for. This keeps one number per path.
        """
        for o in obs:
            if o.latency_ms is not None:
                self._last_latency[o.path_id] = o.latency_ms

    # ------------------------------------------------------------------ the map

    def _row(self, path: PathRef, topo: TopologySnapshot) -> list[float]:
        """One path, as the flat vector the regressor eats.

        Everything here comes off the snapshot or off memory, so a path built
        from interfaces never seen before still produces a row. That property is
        what ``handles_unseen_interfaces`` is claiming, and building it into the
        feature map is the whole of earning the claim.
        """
        seen = [topo.interfaces[i] for i in path.interfaces if i in topo.interfaces]
        latency = sum(i.declared_latency_ms or 10.0 for i in seen)
        bandwidth = min((i.declared_bw_mbps or 100.0 for i in seen), default=100.0)
        return [
            float(path.hop_count),
            float(latency),
            float(bandwidth),
            float(self._last_latency.get(path.path_id, latency)),
        ]

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> Mapping[str, Prediction]:
        """Predict each path at ``t + horizon_s``.

        Two things the contract requires and a wrapper usually forgets:

        * ``demand`` may be ``None`` and must be tolerated. Raising is a recorded
          outcome, not a harness bug -- and it costs you every decision metric.
        * ``paths`` may contain interfaces absent from the snapshot you were
          reset with. Same rule.

        The returned mapping is keyed by ``path.path_id``. Build it from
        ``paths`` and never from an index into your own arrays: a re-signed
        segment can arrive with a new identifier for the same sequence of hops,
        and a model keyed on position rather than identity silently discards its
        history every refresh cycle while looking like it works.
        """
        if not paths:
            return {}
        rows = [self._row(p, topo) for p in paths]
        scores = self.model.predict(rows)

        out: dict[str, Prediction] = {}
        for path, row, score in zip(paths, rows, scores, strict=True):
            # A point estimate is legal and honest. ``Dist.point_estimate``
            # exists so a baseline can be written; ``is_distributional`` is then
            # False and probe R5 records exactly that.
            out[path.path_id] = Prediction(
                latency_ms=Dist.point_estimate(max(0.0, float(score))),
                throughput_mbps=Dist.point_estimate(row[2]),
                loss=Dist.point_estimate(0.001),
                # confidence stays None, matching reports_confidence=False.
            )
        return out

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        """The distribution hosts should sample from.

        A one-hot is legal -- probe R8 records it as a ranking rather than an
        assignment -- but a model that always sends everything down its current
        favourite is the one the stability family exists to catch: it moves the
        congestion instead of relieving it, and then chases it.
        """
        if not paths:
            return Advisory(weights={}, reason="nothing offered")

        predicted = self.predict(topo, paths, horizon_s=0.0, demand=None)
        costs = {p.path_id: predicted[p.path_id].cost() for p in paths}

        # Softmax on negative cost. Temperature is a constructor argument so a
        # sweep can vary it, which makes two settings of it two variants of one
        # architecture -- exactly the comparison the report groups for.
        #
        # Scaled by the *observed* cost spread, not used as an absolute. An
        # absolute temperature is a bug that only shows up on some worlds: path
        # costs here run from a few milliseconds to a few thousand, so T=1.0
        # against a spread of 300 returns weights like 1e-130 and the advisory
        # is a one-hot in everything but arithmetic. The pre-flight caught
        # exactly that in the first version of this file, which is why the line
        # below exists and why it is the lesson worth copying.
        floor = min(costs.values())
        spread = max(costs.values()) - floor
        scale = max(1e-6, self.temperature * spread if spread > 0 else self.temperature)
        weights = {
            path_id: pow(2.718281828, -(cost - floor) / scale) for path_id, cost in costs.items()
        }
        total = sum(weights.values()) or 1.0
        return Advisory(
            weights={k: v / total for k, v in weights.items()},
            temperature=self.temperature,
            reason=f"softmax over {len(paths)} paths at T={self.temperature} of the spread",
        )


# --------------------------------------------------------------------------
# 3. If your model needs to drive itself, add one more method.
# --------------------------------------------------------------------------
#
# Everything above is the *fixed cycle*: the harness observes, asks you to
# predict, asks you to advise. That is right for a forecaster, and it is what
# every classical baseline uses.
#
# An agent -- an LLM deciding what to measure, a policy budgeting its own probes
# -- wants the other entry point. Add ``act(session, deadline_s)``, declare
# ``uses_tools=True``, and you are handed the session instead of being driven:
#
#     def act(self, session, deadline_s: float) -> None:
#         found = session.call("query_paths", src=src, dst=dst, limit=20)
#         ...
#         session.call("publish_advisory", src=src, dst=dst, weights=weights)
#
# Three things about that path, all of which the reference agent in
# ``scionarena.reference.agents`` demonstrates:
#
# * Every call costs, and a refused call is a *returned result* with ``ok=False``
#   -- never an exception. Rate limits, exhausted budgets and timed-out probes
#   all arrive that way, and handling them is part of the job.
# * The clock runs while you think. Returning after the deadline is permitted and
#   recorded: your advice is simply applied late, to a world that moved.
# * ``list_scopes`` tells you which source-destination pairs you serve. You
#   cannot be handed them in the constructor, because whoever names your model in
#   a suite has not seen the world it will run in.
#
# Declaring ``uses_tools=True`` without an ``act`` is now a real mistake rather
# than a harmless one: a sweep believes the declaration and chooses a different
# code path. ``scionarena adapt`` names it in a second.
