"""The mandatory baselines the benchmark reports every model against.

Master Spec §28 requires accuracy to be reported against **persistence,
Tier-0-only, static-only, latest-sample and EWMA**, and the requirement is worth
nothing if it can be switched off -- "our model scored 0.31" means nothing
without the floor, and a floor that is optional is absent on exactly the runs
that would most like to omit it. Every sweep runs all five (ADR 0016).

Two of the five already existed and are re-exported here so that the set is one
list in one place: ``EMAOracle`` is EWMA -- the spec's own "≈ the OVGU oracle"
-- and ``CapacityProportional`` is static-only. The three added here are the
ones with no smoothing, no measurements, and no spatial structure respectively.

None of these is a good predictor and none is meant to be. A model that does not
beat persistence has not earned a forecast head, which is a sentence the report
is expected to print in those words.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import median

from ..exposure.contracts import (
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
from .models import CapacityProportional, EMAOracle

__all__ = [
    "MANDATORY_BASELINES",
    "Persistence",
    "LatestSample",
    "Tier0Only",
    "CapacityProportional",
    "EMAOracle",
]

#: Spec -> what it is a floor for. Import paths, so they load exactly as a
#: user's model does (ADR 0013) and there is no second lookup table.
MANDATORY_BASELINES: Mapping[str, str] = {
    "scionarena.reference.baselines:Persistence": "persistence",
    "scionarena.reference.baselines:Tier0Only": "Tier-0-only",
    "scionarena.reference.models:CapacityProportional": "static-only",
    "scionarena.reference.baselines:LatestSample": "latest-sample",
    "scionarena.reference.models:EMAOracle": "EWMA",
}


def _uniform(paths: Sequence[PathRef]) -> Advisory:
    share = 1.0 / max(1, len(paths))
    return Advisory(weights={p.path_id: share for p in paths}, reason="nothing observed yet")


class Persistence:
    """Whatever is true now stays true, and whatever was advised stays advised.

    The baseline every forecast head has to beat before it ships. Its prediction
    for any horizon is its last observation, unchanged, and its advice is the
    advice it gave last time -- so it is also the *stability* floor: a model
    cannot be steadier than one that never changes its mind.
    """

    def __init__(self) -> None:
        self.capabilities = Capabilities(
            name="Persistence",
            version="1.0.0",
            authors="mandatory baseline, Master Spec §28",
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            notes="Last observed value, held. Advice is never revised once given.",
        )

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self.last: dict[str, Observation] = {}
        self.advice: dict[str, float] = {}

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        for o in obs:
            # Latest wins. Not a window and not a mean: persistence is the claim
            # that the most recent truth is the next one.
            self.last[o.path_id] = o

    def _one(self, path: PathRef, topo: TopologySnapshot) -> Prediction:
        seen = self.last.get(path.path_id)
        declared = sum(
            (topo.interfaces[i].declared_latency_ms or 10.0)
            for i in path.interfaces
            if i in topo.interfaces
        )
        latency = seen.latency_ms if seen and seen.latency_ms is not None else (declared or 10.0)
        loss = seen.loss if seen and seen.loss is not None else 0.0
        return Prediction(
            latency_ms=Dist.point_estimate(latency),
            throughput_mbps=Dist.point_estimate(
                seen.throughput_mbps if seen and seen.throughput_mbps is not None else 0.0
            ),
            loss=Dist.point_estimate(loss),
        )

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> dict[str, Prediction]:
        # horizon_s is ignored on purpose. That is what persistence *is*, and it
        # is why "does anything beat persistence at +300 s" is a real question.
        return {p.path_id: self._one(p, topo) for p in paths}

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        known = {p.path_id: self.advice[p.path_id] for p in paths if p.path_id in self.advice}
        total = sum(known.values())
        if total <= 0.0:
            first = _uniform(paths)
            self.advice = dict(first.weights)
            return first
        # Renormalised over the paths that still exist, and nothing else moves:
        # a path that appeared since the first advisory is never adopted, which
        # is the honest reading of "persistence" and is a real handicap.
        return Advisory(
            weights={k: v / total for k, v in known.items()},
            reason="the advice already given",
        )


class LatestSample:
    """The most recent observation, and nothing before it.

    EWMA with ``alpha = 1``: no smoothing, no memory, no filter. It exists to
    separate "the smoothing helped" from "the measurement was enough", which is
    a distinction a report that only carried EWMA could not draw.
    """

    def __init__(self) -> None:
        self.capabilities = Capabilities(
            name="LatestSample",
            version="1.0.0",
            authors="mandatory baseline, Master Spec §28",
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            notes="Newest sample per path, unsmoothed. Greedy on it.",
        )

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self.latest: dict[str, float] = {}

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        for o in obs:
            if o.latency_ms is not None:
                self.latest[o.path_id] = o.latency_ms

    def _cost(self, path: PathRef, topo: TopologySnapshot) -> float:
        seen = self.latest.get(path.path_id)
        if seen is not None:
            return seen
        return (
            sum(
                (topo.interfaces[i].declared_latency_ms or 10.0)
                for i in path.interfaces
                if i in topo.interfaces
            )
            or 10.0
        )

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> dict[str, Prediction]:
        return {
            p.path_id: Prediction(
                latency_ms=Dist.point_estimate(self._cost(p, topo)),
                throughput_mbps=Dist.point_estimate(0.0),
                loss=Dist.point_estimate(0.0),
            )
            for p in paths
        }

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        if not paths:
            return Advisory(weights={})
        best = min(paths, key=lambda p: self._cost(p, topo))
        return Advisory(
            weights={p.path_id: (1.0 if p is best else 0.0) for p in paths},
            reason="lowest latency in the newest sample",
        )


class Tier0Only:
    """A robust online filter per interface, and no structure above it.

    ASSUMPTION(Q9): §19.1 specifies Tier-0 as "online robust filters, always-on"
    -- t-digest quantile trackers and a Student-t state-space filter per
    directed edge, servable with inflated intervals, with no spatial transfer
    and no forecast head. It does not say which filter a *baseline* should use,
    and the choice moves the number this is a floor for. Implemented as a
    rolling median over a bounded window with the interquartile range as the
    interval, which keeps the two properties the spec is actually leaning on:
    robustness to heavy-tailed observation noise, and an interval that widens
    when the evidence is thin.

    The point of having it beside EWMA is the spec's own question: the Tier-1
    lift is measured *against Tier-0*, because a nowcast that cannot beat an
    online filter has not earned the machinery above it.
    """

    #: Bounded so an hour-long run does not turn the median into a lifetime
    #: average, which would stop being an online filter.
    WINDOW = 32

    def __init__(self, window: int | None = None) -> None:
        self.window = int(window or self.WINDOW)
        self.capabilities = Capabilities(
            name="Tier0Only",
            version="1.0.0",
            authors="mandatory baseline, Master Spec §28 / §19.1",
            distributional=True,
            reports_confidence=True,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            notes="Rolling median per interface with an interquartile interval. "
            "No spatial structure, no forecast head.",
        )

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self.samples: dict[str, list[float]] = {}
        self._paths: dict[str, PathRef] = {p.path_id: p for p in topo.paths}

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        self._paths.update({p.path_id: p for p in topo.paths})
        for o in obs:
            path = self._paths.get(o.path_id)
            if path is None or not path.interfaces or o.latency_ms is None:
                continue
            share = o.latency_ms / len(path.interfaces)
            for iid in path.interfaces:
                window = self.samples.setdefault(iid, [])
                window.append(share)
                if len(window) > self.window:
                    del window[0]

    def _interface(self, iid: str, topo: TopologySnapshot) -> tuple[float, float]:
        """Robust centre and half-spread for one interface."""
        window = self.samples.get(iid)
        if not window:
            attrs = topo.interfaces.get(iid)
            declared = (attrs.declared_latency_ms if attrs else None) or 10.0
            # Prior-only: the spec's thinnest evidence tier, served wide.
            return declared, declared
        centre = median(window)
        ordered = sorted(window)
        lo = ordered[len(ordered) // 4]
        hi = ordered[(3 * len(ordered)) // 4]
        # Thin evidence keeps the interval open even when the samples agree.
        floor = centre / max(1.0, len(window))
        return centre, max((hi - lo) / 2.0, floor)

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> dict[str, Prediction]:
        out: dict[str, Prediction] = {}
        for path in paths:
            per = [self._interface(i, topo) for i in path.interfaces] or [(10.0, 10.0)]
            centre = sum(c for c, _ in per)
            spread = sum(s for _, s in per)
            out[path.path_id] = Prediction(
                latency_ms=Dist(
                    mean=centre,
                    quantiles={
                        0.1: max(0.0, centre - spread),
                        0.5: centre,
                        0.9: centre + spread,
                    },
                ),
                throughput_mbps=Dist.point_estimate(0.0),
                loss=Dist.point_estimate(0.0),
            )
        return out

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        predicted = self.predict(topo, paths)
        if not predicted:
            return Advisory(weights={})
        # Spread over the paths whose upper quantile is under the best path's,
        # rather than piling onto one: an always-on floor that stampedes is not
        # a floor, it is the pathology the benchmark is looking for.
        upper = {
            k: (v.latency_ms.quantiles or {}).get(0.9, v.latency_ms.point)
            for k, v in predicted.items()
        }
        best = min(upper.values())
        feasible = [k for k, v in upper.items() if v <= best * 1.25]
        share = 1.0 / len(feasible)
        return Advisory(
            weights={k: (share if k in feasible else 0.0) for k in predicted},
            reason="uniform over the robust-interval feasible set",
        )
