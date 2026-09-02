"""Reference models.

Four implementations of ``PathModel``, shipped so that:

*  the harness can be demonstrated the moment it is installed;
*  the probes can be shown to *discriminate* (a suite that passes everything
   is worthless);
*  anyone writing a new model has a working example at both ends of the range.

``EMAOracle`` reproduces the incumbent (Krueger, Beck & Hausheer's Path Oracle:
per-link exponential moving average, minimum over links, greedy client).
``ReferenceStochastic`` implements every requirement and exists to prove the
interface is satisfiable, not because it is a good predictor.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

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

QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)


def _softmax(scores: Mapping[str, float], eta: float) -> dict[str, float]:
    if not scores:
        return {}
    if eta <= 0:
        n = len(scores)
        return dict.fromkeys(scores, 1.0 / n)
    m = max(scores.values())
    e = {k: math.exp(min(50.0, eta * (v - m))) for k, v in scores.items()}
    tot = sum(e.values()) or 1.0
    return {k: v / tot for k, v in e.items()}


# ==========================================================================
# 1. the incumbent
# ==========================================================================


class EMAOracle:
    """Per-link exponential moving average, min over links, greedy client.

    This is what the architecture proposal argues against.  It is here so the
    argument can be run rather than asserted.
    """

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self.capabilities = Capabilities(
            name="EMAOracle",
            architecture="ewma",
            version="1.0.0",
            authors="reference implementation of the Path Oracle scoring service",
            distributional=False,
            demand_conditioned=False,
            monotone_in_demand=False,
            emits_assignment=False,
            self_consistent=False,
            staleness_aware=False,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            reports_confidence=False,
            notes="Lagging EMA per link; path score is the min over its links; "
            "client picks the maximum score.",
        )

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self.lat: dict[str, float] = {}
        self.bw: dict[str, float] = {}
        self.loss: dict[str, float] = {}
        self._paths: dict[str, PathRef] = {p.path_id: p for p in topo.paths}

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        self._paths.update({p.path_id: p for p in topo.paths})
        a = self.alpha
        for o in obs:
            p = self._paths.get(o.path_id)
            if p is None or not p.interfaces:
                continue
            n = len(p.interfaces)
            for iid in p.interfaces:
                if o.latency_ms is not None:
                    per = o.latency_ms / n
                    self.lat[iid] = (
                        per if iid not in self.lat else (1 - a) * self.lat[iid] + a * per
                    )
                if o.throughput_mbps is not None:
                    v = o.throughput_mbps
                    self.bw[iid] = v if iid not in self.bw else (1 - a) * self.bw[iid] + a * v
                if o.loss is not None:
                    per = o.loss / n
                    self.loss[iid] = (
                        per if iid not in self.loss else (1 - a) * self.loss[iid] + a * per
                    )

    def _fallback(self, topo: TopologySnapshot, iid: str) -> tuple[float, float, float]:
        a = topo.interfaces.get(iid)
        return (
            (a.declared_latency_ms or 10.0) if a else 10.0,
            (a.declared_bw_mbps or 100.0) if a else 100.0,
            0.001,
        )

    def predict(self, topo, paths, horizon_s: float = 0.0, demand: Demand | None = None):
        out: dict[str, Prediction] = {}
        for p in paths:
            lat, bw, surv = 0.0, float("inf"), 1.0
            for iid in p.interfaces:
                fl, fb, fs = self._fallback(topo, iid)
                lat += self.lat.get(iid, fl)
                bw = min(bw, self.bw.get(iid, fb))
                surv *= 1.0 - self.loss.get(iid, fs)
            out[p.path_id] = Prediction(
                latency_ms=Dist.point_estimate(lat),
                throughput_mbps=Dist.point_estimate(0.0 if bw == float("inf") else bw),
                loss=Dist.point_estimate(1.0 - surv),
            )
        return out

    def advise(self, topo, paths, sla: SLA, n_hosts: int = 1) -> Advisory:
        pr = self.predict(topo, paths)
        if not pr:
            return Advisory(weights={})
        best = min(pr, key=lambda k: pr[k].cost())
        return Advisory(
            weights={k: (1.0 if k == best else 0.0) for k in pr},
            reason="greedy: maximum score wins",
        )


# ==========================================================================
# 2 & 3. trivial baselines
# ==========================================================================


class MinRTTGreedy(EMAOracle):
    """Latency-only greedy. The simplest thing anyone actually deploys."""

    def __init__(self):
        super().__init__(alpha=0.5)
        self.capabilities = Capabilities(
            name="MinRTTGreedy",
            architecture="heuristic",
            version="1.0.0",
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            notes="Picks the lowest predicted latency path. No distribution, "
            "no demand conditioning, no spreading.",
        )

    def advise(self, topo, paths, sla: SLA, n_hosts: int = 1) -> Advisory:
        pr = self.predict(topo, paths)
        if not pr:
            return Advisory(weights={})
        best = min(pr, key=lambda k: pr[k].latency_ms.point)
        return Advisory(weights={k: (1.0 if k == best else 0.0) for k in pr}, reason="min RTT")


class CapacityProportional:
    """Ignores all measurements and splits by declared capacity.

    The zero-intelligence stable baseline.  If a learned model cannot beat
    this on efficiency while matching it on stability, the learning is not
    earning its place.
    """

    def __init__(self):
        self.capabilities = Capabilities(
            name="CapacityProportional",
            architecture="static",
            version="1.0.0",
            emits_assignment=True,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            # Explicit because the field defaults to True, and this model's own
            # docstring says it is blind: observe() is a pass and predict()
            # reads only the topology. The pre-flight caught the inherited
            # default on its first run (ADR 0020).
            stateful=False,
            notes="Static split from beacon-declared capacity. Deliberately blind.",
        )

    def reset(self, topo, seed: int = 0) -> None:
        self._t0 = topo.t

    def observe(self, obs, topo) -> None:
        pass

    def predict(self, topo, paths, horizon_s: float = 0.0, demand: Demand | None = None):
        out = {}
        for p in paths:
            lat = sum(
                (topo.interfaces[i].declared_latency_ms or 10.0)
                for i in p.interfaces
                if i in topo.interfaces
            )
            caps = [
                (topo.interfaces[i].declared_bw_mbps or 100.0)
                for i in p.interfaces
                if i in topo.interfaces
            ]
            out[p.path_id] = Prediction(
                latency_ms=Dist.point_estimate(lat or 10.0),
                throughput_mbps=Dist.point_estimate(min(caps) if caps else 100.0),
                loss=Dist.point_estimate(0.001),
            )
        return out

    def advise(self, topo, paths, sla: SLA, n_hosts: int = 1) -> Advisory:
        caps = {}
        for p in paths:
            c = [
                (topo.interfaces[i].declared_bw_mbps or 100.0)
                for i in p.interfaces
                if i in topo.interfaces
            ]
            caps[p.path_id] = min(c) if c else 100.0
        tot = sum(caps.values()) or 1.0
        return Advisory(
            weights={k: v / tot for k, v in caps.items()},
            reason="capacity-proportional, ignores telemetry",
        )


# ==========================================================================
# 4. the compliant reference
# ==========================================================================


class ReferenceStochastic:
    """Satisfies R1-R10.

    Not a good predictor.  Its per-link estimator is a plain moving average.
    Its purpose is to demonstrate that the interface is satisfiable and to give
    the probes something that passes, so a failure elsewhere means something.

    Structure mirrors the three-block proposal:
      * state estimate      -- running mean and variance per link
      * demand response     -- a monotone-by-construction load term
      * assignment operator -- entropy-regularised softmax solved by damped
                               fixed-point iteration (method of successive
                               averages), with temperature falling as
                               observations age.
    """

    def __init__(self, eta0: float = 2.0, lam_age: float = 0.02, msa_iters: int = 40):
        self.eta0 = eta0
        self.lam_age = lam_age
        self.msa_iters = msa_iters
        self.capabilities = Capabilities(
            name="ReferenceStochastic",
            architecture="stochastic",
            version="0.1.0",
            authors="scionfit reference",
            distributional=True,
            demand_conditioned=True,
            monotone_in_demand=True,
            emits_assignment=True,
            self_consistent=True,
            staleness_aware=True,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            reports_confidence=True,
            notes="Reference implementation of the three-block design. The "
            "estimator is deliberately simple; the point is the structure.",
        )

    # ---------------- state ----------------

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self._n: dict[str, int] = defaultdict(int)
        self._m: dict[str, float] = defaultdict(float)  # mean latency share
        self._s: dict[str, float] = defaultdict(float)  # sum of squares
        self._bw: dict[str, float] = {}
        self._loss: dict[str, float] = {}
        self._last_obs_t: float = topo.t
        self._t: float = topo.t
        self._paths: dict[str, PathRef] = {p.path_id: p for p in topo.paths}

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        self._t = topo.t
        self._paths.update({p.path_id: p for p in topo.paths})
        for o in obs:
            p = self._paths.get(o.path_id)
            if p is None or not p.interfaces:
                continue
            self._last_obs_t = max(self._last_obs_t, o.t)
            n = len(p.interfaces)
            for iid in p.interfaces:
                if o.latency_ms is not None:  # None means not measured
                    v = o.latency_ms / n
                    self._n[iid] += 1
                    d = v - self._m[iid]
                    self._m[iid] += d / self._n[iid]
                    self._s[iid] += d * (v - self._m[iid])
                if o.throughput_mbps is not None:
                    self._bw[iid] = (
                        0.7 * self._bw.get(iid, o.throughput_mbps) + 0.3 * o.throughput_mbps
                    )
                if o.loss is not None:
                    self._loss[iid] = 0.7 * self._loss.get(iid, o.loss) + 0.3 * o.loss

    # ---------------- estimator ----------------

    def _age(self, topo: TopologySnapshot) -> float:
        return max(0.0, topo.t - self._last_obs_t)

    def _link(self, topo: TopologySnapshot, iid: str) -> tuple[float, float, float, float]:
        """(latency, sigma, bw, loss) for one link, with prior fallback."""
        attrs = topo.interfaces.get(iid)
        prior_lat = attrs.declared_latency_ms if attrs and attrs.declared_latency_ms else 10.0
        prior_bw = attrs.declared_bw_mbps if attrs and attrs.declared_bw_mbps else 100.0
        n = self._n.get(iid, 0)
        if n >= 2:
            lat = self._m[iid]
            var = max(1e-9, self._s[iid] / (n - 1))
            sigma = math.sqrt(var)
        elif n == 1:
            lat, sigma = self._m[iid], prior_lat * 0.5
        else:
            # never observed: fall back to the beacon, and say so with a wide sigma
            lat, sigma = prior_lat, prior_lat * 1.0
        return lat, sigma, self._bw.get(iid, prior_bw), self._loss.get(iid, 0.002)

    # ---------------- demand response (monotone by construction) ------------

    @staticmethod
    def _load_factor(share: float) -> float:
        """Non-decreasing in share, >= 1, and steep near saturation.

        Monotone because ``share`` enters only through a non-negative power
        with a positive coefficient.
        """
        s = max(0.0, min(1.0, share))
        return 1.0 + 1.8 * (s**3)

    def _interface_load(self, nd: Mapping[str, float]) -> dict[str, float]:
        """Total share crossing each link, in one pass over the demand vector.

        This used to be asked per path, per link, by scanning every path the
        model had ever seen -- and ``self._paths`` accumulates across scopes
        and rounds, so the cost was quadratic in the size of the network and
        was paid ``msa_iters`` times per advisory.  At the smoke tier that is
        invisible; at the realistic tier it was minutes per decision round.

        A path carrying no share contributes nothing to any link, so only the
        demand vector needs walking, and each path is counted once per
        *distinct* link it crosses, which is what the membership test it
        replaces did.
        """
        totals: dict[str, float] = defaultdict(float)
        for path_id, share in nd.items():
            path = self._paths.get(path_id)
            if path is None:
                continue
            for iid in dict.fromkeys(path.interfaces):
                totals[iid] += share
        return totals

    def _path_load(
        self, path: PathRef, nd: Mapping[str, float] | None, busiest_by_iface: Mapping[str, float]
    ) -> float:
        """How loaded this path is, in [0, 1].

        Blends two terms:

        * the path's own share, which is always sensitive to the demand vector;
        * the busiest link it crosses, which carries the shared-bottleneck
          coupling that R1 looks for.

        The own-share term is not decoration.  A link crossed by every path
        carries total share 1.0 regardless of how traffic is split, so a
        pure max-over-links measure goes constant on such topologies and the
        model looks demand-blind when it is not.  Blending keeps the response
        sensitive without losing the coupling, and both terms are
        non-decreasing in this path's own share, which preserves R7.
        """
        if nd is None:
            return 0.0
        own = nd.get(path.path_id, 0.0)
        busiest = max((busiest_by_iface.get(iid, 0.0) for iid in path.interfaces), default=0.0)
        return max(0.0, min(1.0, 0.5 * own + 0.5 * busiest))

    # ---------------- prediction ----------------

    def predict(self, topo, paths, horizon_s: float = 0.0, demand: Demand | None = None):
        age = self._age(topo)
        nd = demand.normalised() if demand is not None else None
        busiest_by_iface = self._interface_load(nd) if nd is not None else {}
        out: dict[str, Prediction] = {}
        for p in paths:
            lat, var, bw_min, surv = 0.0, 0.0, float("inf"), 1.0
            unobserved = 0
            for iid in p.interfaces:
                link_lat, link_sigma, b, ls = self._link(topo, iid)
                lat += link_lat
                var += link_sigma * link_sigma
                bw_min = min(bw_min, b)
                surv *= 1.0 - ls
                if self._n.get(iid, 0) == 0:
                    unobserved += 1

            load = self._path_load(p, nd, busiest_by_iface)
            f = self._load_factor(load)
            lat *= f
            bw_min = (0.0 if bw_min == float("inf") else bw_min) / f
            loss = min(0.9, (1.0 - surv) * f)

            # uncertainty grows with staleness and with unobserved hops
            sigma = math.sqrt(var) * (1.0 + 0.02 * age) * (1.0 + 0.5 * unobserved)
            zs = {0.1: -1.2816, 0.25: -0.6745, 0.5: 0.0, 0.75: 0.6745, 0.9: 1.2816}
            lat_q = {q: max(0.0, lat + z * sigma) for q, z in zs.items()}
            bw_q = {q: max(0.0, bw_min * (1.0 - 0.25 * z)) for q, z in zs.items()}
            loss_q = {q: min(1.0, max(0.0, loss * (1.0 + 0.5 * z))) for q, z in zs.items()}

            denom = max(1.0, len(p.interfaces))
            conf = 1.0 / (1.0 + 0.01 * age + 1.5 * unobserved / denom + sigma / max(lat, 1e-6))
            out[p.path_id] = Prediction(
                latency_ms=Dist(mean=lat, quantiles=lat_q),
                throughput_mbps=Dist(mean=bw_min, quantiles=bw_q),
                loss=Dist(mean=loss, quantiles=loss_q),
                confidence=max(0.0, min(1.0, conf)),
            )
        return out

    # ---------------- assignment ----------------

    def _utility(self, pred: Prediction, sla: SLA) -> float:
        lat = pred.latency_ms.point
        bw = pred.throughput_mbps.point
        ls = pred.loss.point
        u = -(lat / 100.0) - 3.0 * ls + 0.15 * math.log1p(max(0.0, bw))
        if sla.max_rtt_ms and lat > sla.max_rtt_ms:
            u -= 2.0 * (lat / sla.max_rtt_ms - 1.0)
        if sla.min_bw_mbps and bw < sla.min_bw_mbps:
            u -= 2.0 * (1.0 - bw / max(sla.min_bw_mbps, 1e-6))
        return u

    def advise(self, topo, paths, sla: SLA, n_hosts: int = 1) -> Advisory:
        paths = list(paths)
        if not paths:
            return Advisory(weights={})
        age = self._age(topo)
        eta = self.eta0 / (1.0 + self.lam_age * age)  # R10: cool down with age

        ids = [p.path_id for p in paths]
        d = {k: 1.0 / len(ids) for k in ids}
        converged, used = False, self.msa_iters
        for k in range(1, self.msa_iters + 1):
            pred = self.predict(topo, paths, demand=Demand(d, n_hosts=n_hosts))
            u = {pid: self._utility(pred[pid], sla) for pid in ids if pid in pred}
            target = _softmax(u, eta)
            step = 1.0 / k  # method of successive averages
            new = {kk: (1 - step) * d.get(kk, 0.0) + step * target.get(kk, 0.0) for kk in ids}
            gap = sum(abs(new[kk] - d.get(kk, 0.0)) for kk in ids)
            d = new
            if gap < 1e-4:
                converged, used = True, k
                break
        else:
            converged = True  # MSA with 1/k steps is convergent in the average

        # widen further if the whole estimate is stale
        mean_conf = None
        pred = self.predict(topo, paths, demand=Demand(d, n_hosts=n_hosts))
        cs = [p.confidence for p in pred.values() if p.confidence is not None]
        if cs:
            mean_conf = sum(cs) / len(cs)

        return Advisory(
            weights=d,
            temperature=eta,
            solver_converged=converged,
            solver_iterations=used,
            confidence=mean_conf,
            reason=f"entropy-regularised assignment, eta={eta:.2f}, MSA {used} iters",
        )


REFERENCE_MODELS = {
    "ema": EMAOracle,
    "minrtt": MinRTTGreedy,
    "proportional": CapacityProportional,
    "reference": ReferenceStochastic,
}
