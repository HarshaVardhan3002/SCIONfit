"""Two models that differ only in which layer their *ranking* comes from.

Invariant 6: a probe nothing fails measures nothing. R11 asks whether a model
ranks on the layer its requirement class allows, and the only honest way to ship
that probe is to ship something it fails.

The spec's argument is a timescale one. A path's observable state comes in three
layers that move at very different speeds:

* **static** -- what the beacon declared: hop count, declared latency, declared
  bandwidth, the interface sequence itself. Changes when the topology or the
  policy changes, which is rare and planned.
* **liveness** -- is this path up and answering at all. Changes in seconds, and a
  path that is down must not be ranked first however good it looks.
* **dynamic** -- congestion. Changes continuously, and any estimate of it is
  already old by the time a decision reaches the hosts.

A latency-class model is allowed to *rank* on the first two and must let the
third widen its intervals rather than reorder its ranking. Ranking on a
fifteen-second-old congestion estimate is routing on lagged load, which is the
documented oscillation mechanism: everyone moves to the path that was cheapest
fifteen seconds ago, which makes it the most expensive one, and the population
swaps again next round.

**The discipline is about the ranking, not about the nowcast.** Both variants
report the same point estimate, and it is the honest one: declared latency plus
whatever congestion has been observed on top of it. A model that refused to let
observations into its point estimate would be failing R1 and R3 for a reason
that has nothing to do with layers. What differs is which of those two numbers
orders the advisory, and that is the single line marked below.
"""

from __future__ import annotations

import math
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

__all__ = ["LayeredRanker"]

#: Quantiles the intervals are expressed at. 0.1/0.9 brackets the 80% the
#: accuracy family scores coverage against.
QUANTILES = (0.1, 0.5, 0.9)
#: How much of the interval width the observed congestion contributes, as a
#: multiple of the static estimate. The dynamic layer's whole permitted effect
#: on a disciplined model's *decision*.
CONGESTION_WIDTH = 1.5
#: Floor on the interval, so a model with no observations still brackets rather
#: than claiming a point.
BASE_WIDTH = 0.12
#: Simulated seconds of silence after which the interval has doubled. What
#: ``staleness_aware`` means here: an estimate nobody has confirmed for ten
#: minutes is not the estimate it was.
AGE_SCALE = 300.0


class LayeredRanker:
    """Ranks on the static layer, or -- with ``discipline=False`` -- on the dynamic one."""

    def __init__(self, discipline: bool = True, alpha: float = 0.3) -> None:
        self.discipline = bool(discipline)
        self.alpha = float(alpha)
        # Keyed on the **interface sequence**, never on the identifier the path
        # arrived under. A re-beaconed segment describes the same hops under a
        # new identifier, and a model keyed on the identifier discards its whole
        # history every refresh cycle while every one of its outputs still looks
        # plausible. Probe R12 grades exactly this.
        self._seen: dict[tuple[str, ...], float] = {}
        self._static: dict[tuple[str, ...], float] = {}
        self._at: dict[tuple[str, ...], float] = {}
        #: The path ids seen this episode, so ``observe`` can find the hops an
        #: observation belongs to. Rebuilt from every snapshot, so a rename is
        #: picked up on the turn it happens.
        self._hops: dict[str, tuple[str, ...]] = {}
        self.capabilities = Capabilities(
            name=f"LayeredRanker({'disciplined' if discipline else 'lagged'})",
            version="1.0.0",
            architecture="layered",
            # The declaration that makes R11 applicable at all. A model leaving
            # this empty has claimed nothing R11 could contradict.
            requirement_class="latency",
            distributional=True,
            staleness_aware=True,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            reports_confidence=True,
            emits_assignment=True,
            stateful=True,
            notes=(
                "Ranks on declared latency; congestion widens the interval."
                if discipline
                else "Ranks on the freshest observed latency, which is lagged load."
            ),
        )

    # ------------------------------------------------------------------ model

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self._seen.clear()
        self._static.clear()
        self._at.clear()
        self._hops.clear()

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        for p in topo.paths:
            self._hops[p.path_id] = tuple(p.interfaces)
        static = {p.path_id: self._declared(topo, p) for p in topo.paths}
        for o in obs:
            # ``None`` is not measured. Training it as zero would teach the
            # model that unobserved paths are instant (probe R3).
            if o.latency_ms is None or not math.isfinite(o.latency_ms):
                continue
            key = self._hops.get(o.path_id)
            if key is None:
                continue
            previous = self._seen.get(key)
            self._seen[key] = (
                o.latency_ms
                if previous is None
                else (1.0 - self.alpha) * previous + self.alpha * o.latency_ms
            )
            self._at[key] = o.t
            if o.path_id in static:
                self._static[key] = static[o.path_id]

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> Mapping[str, Prediction]:
        out: dict[str, Prediction] = {}
        for path in paths:
            declared = self._declared(topo, path)
            key = tuple(path.interfaces)
            observed = self._seen.get(key)
            excess = self._excess(key, declared)
            # The nowcast is honest for both variants: declared plus whatever
            # has actually been seen on top of it -- and *under* it, because a
            # path measuring faster than it declared is information too. Without
            # the signed version, an observation of zero and no observation at
            # all produce the same number, which is probe R3 exactly.
            centre = declared * (1.0 + excess) if observed is not None else declared
            age = self._age(topo, key)
            stale = 1.0 + age / AGE_SCALE + min(2.0, horizon_s / AGE_SCALE)
            # A path nobody has measured is bracketed as widely as a fully
            # congested one. Otherwise the model is *more* confident about a
            # path it has never seen than about the ones it has, which is what
            # probe R4 marks weak.
            spread_of = (
                BASE_WIDTH + CONGESTION_WIDTH * abs(excess)
                if observed is not None
                else BASE_WIDTH + CONGESTION_WIDTH
            )
            width = centre * spread_of * stale
            out[path.path_id] = Prediction(
                latency_ms=Dist(
                    mean=centre,
                    quantiles={
                        QUANTILES[0]: max(0.0, centre - width / 2.0),
                        QUANTILES[1]: centre,
                        QUANTILES[2]: centre + width / 2.0,
                    },
                ),
                throughput_mbps=Dist(mean=self._bandwidth(topo, path)),
                loss=Dist(mean=0.001),
                confidence=1.0
                / (1.0 + abs(excess) + age / AGE_SCALE + horizon_s / AGE_SCALE)
                / (1.0 if observed is not None else 2.0),
            )
        return out

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        if not paths:
            return Advisory(weights={}, reason="no paths")
        declared = {p.path_id: self._declared(topo, p) for p in paths}
        hops = {p.path_id: tuple(p.interfaces) for p in paths}
        # ------------------------------------------------------------------
        # The whole probe, in one expression. Both variants know the same two
        # numbers; the disciplined one lets only the static layer decide the
        # order, and the other ranks on how congested a path was last time
        # anyone looked.
        rank_on = (
            declared
            if self.discipline
            else {
                pid: declared[pid] * (1.0 + self._excess(hops[pid], declared[pid]))
                for pid in declared
            }
        )
        # ------------------------------------------------------------------
        best = min(rank_on.values())
        spread = max(rank_on.values()) - best
        age = max((self._age(topo, hops[pid]) for pid in declared), default=0.0)
        # Scaled by the observed spread rather than an absolute temperature: a
        # fixed temperature against a spread of hundreds returns a numerically
        # one-hot vector, which is a ranking however it was computed. Widened by
        # age, which is what makes stale advice vague rather than confidently
        # wrong.
        # Congestion moves mass through the *temperature*, never through a
        # per-path factor. Dividing each weight by its own congestion term was
        # this model's first version and it reordered the published ranking on
        # exactly the input R11 tests -- the static ranking was untouched and the
        # advisory still swapped, because a big enough demotion carries a path
        # past its neighbour. Raising the temperature flattens the distribution
        # and cannot reorder it, which is the property the discipline needs.
        congestion = mean_excess = 0.0
        if declared:
            mean_excess = sum(
                max(0.0, self._excess(hops[pid], declared[pid])) for pid in declared
            ) / len(declared)
            congestion = mean_excess
        scale = max(1e-9, 0.25 * spread) * (1.0 + age / AGE_SCALE) * (1.0 + 2.0 * congestion)
        weights = {pid: math.exp(-(c - best) / scale) for pid, c in rank_on.items()}
        total = sum(weights.values()) or 1.0
        return Advisory(
            weights={k: v / total for k, v in weights.items()},
            temperature=scale,
            confidence=1.0 / (1.0 + age / AGE_SCALE),
            reason="static ranking, dynamic width" if self.discipline else "freshest observation",
        )

    # ---------------------------------------------------------------- helpers

    def _excess(self, key: tuple[str, ...], declared: float) -> float:
        """Observed level as a signed fraction of the declared one.

        Signed, and floored at -0.9 rather than at zero. A path measuring faster
        than it declared is a measurement, and clamping it to zero made an
        observation of 0 ms and no observation at all produce the same
        prediction -- which is the failure probe R3 is for.
        """
        observed = self._seen.get(key)
        if observed is None:
            return 0.0
        base = self._static.get(key, declared) or 1.0
        return max(-0.9, (observed - base) / base)

    def _age(self, topo: TopologySnapshot, key: tuple[str, ...]) -> float:
        at = self._at.get(key)
        return 0.0 if at is None else max(0.0, topo.t - at)

    @staticmethod
    def _declared(topo: TopologySnapshot, path: PathRef) -> float:
        """Sum of declared per-hop latency: the static layer, and nothing else."""
        total = 0.0
        for iid in path.interfaces:
            attrs = topo.interfaces.get(iid)
            total += 12.0 if attrs is None else float(attrs.declared_latency_ms)
        return total or 1.0

    @staticmethod
    def _bandwidth(topo: TopologySnapshot, path: PathRef) -> float:
        widths = [
            float(topo.interfaces[iid].declared_bw_mbps)
            for iid in path.interfaces
            if iid in topo.interfaces
        ]
        return min(widths) if widths else 100.0
