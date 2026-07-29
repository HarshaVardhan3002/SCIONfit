"""Tier-1 environment: a small analytical SCION-shaped world.

This is *not* a SCION simulator and does not pretend to be one.  It exists so
the conformance probes have something to interrogate a model against, cheaply
and deterministically.  For control-plane realism use the tier-2 adapter
(scion-dqn-sim); for a real SCION stack use tier-3 (ietf-scion-testbed).

The one thing this world models carefully is the property the probes care
about: **cost rises with load, and paths sharing a link are coupled.**
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from ..interface import (
    Demand,
    InterfaceAttrs,
    Observation,
    PathRef,
    TopologySnapshot,
)

LINK_TYPES = ("core", "parent_child", "peering")


@dataclass
class LinkState:
    """Ground truth for one inter-AS link."""
    capacity_mbps: float
    base_latency_ms: float
    base_loss: float
    background: float = 0.0     # exogenous load as a fraction of capacity


class World:
    """A deterministic, seeded, analytical world.

    ``cost``/``metrics`` implement a Bureau-of-Public-Roads style link
    performance function: latency rises superlinearly with utilisation and
    diverges as utilisation approaches one.  This is the ground truth that
    probe R7 checks a model against.
    """

    def __init__(
        self,
        n_ases: int = 8,
        n_paths: int = 6,
        hops: tuple[int, int] = (2, 5),
        seed: int = 0,
        drift: float = 0.0,
    ):
        self.rng = random.Random(seed)
        self.seed = seed
        self.drift = drift
        self.t = 0.0
        self._build(n_ases, n_paths, hops)

    # ---------------- construction ----------------

    def _build(self, n_ases: int, n_paths: int, hops: tuple[int, int]) -> None:
        r = self.rng
        self.interfaces: dict[str, InterfaceAttrs] = {}
        self.links: dict[str, LinkState] = {}

        n_ifaces = max(n_ases * 2, n_paths * 3)
        for i in range(n_ifaces):
            iid = f"if{i:03d}"
            as_id = f"1-ff00:0:{100 + (i // 2):x}"
            lt = LINK_TYPES[i % 3]
            cap = r.choice([100.0, 200.0, 400.0, 1000.0])
            lat = r.uniform(2.0, 25.0)
            self.interfaces[iid] = InterfaceAttrs(
                iface_id=iid, as_id=as_id, isd=1, link_type=lt,
                declared_bw_mbps=cap, declared_latency_ms=lat, mtu=1472,
            )
            self.links[iid] = LinkState(
                capacity_mbps=cap,
                base_latency_ms=lat,
                base_loss=r.uniform(0.0, 0.004),
                background=r.uniform(0.05, 0.35),
            )

        # Paths deliberately share interfaces -- that coupling is what probe R1
        # detects -- but no interface may appear on more than ``max_share`` of
        # the paths.
        #
        # That cap matters more than it looks.  If one interface sits on *every*
        # path then the total load on it is 1.0 no matter how traffic is split,
        # so no allocation can change it and the whole path-selection problem is
        # degenerate.  A benchmark world must offer genuine alternatives or the
        # demand-conditioning probes measure nothing.
        max_share = 0.6
        cap = max(1, int(n_paths * max_share))
        ids = list(self.interfaces)
        use_count: dict[str, int] = dict.fromkeys(ids, 0)
        self.paths: list[PathRef] = []

        for p in range(n_paths):
            k = r.randint(*hops)
            avail = [i for i in ids if use_count[i] < cap]
            if len(avail) < k:
                avail = ids[:]
            if p == 0:
                chosen = r.sample(avail, min(k, len(avail)))
            else:
                # reuse exactly one interface from an earlier path, if one is
                # still under the cap, so paths are coupled but not identical
                donor_pool = [i for q in self.paths for i in q.interfaces
                              if use_count[i] < cap]
                pool = list(avail)
                if donor_pool:
                    shared = r.choice(donor_pool)
                    rest = [i for i in pool if i != shared]
                    chosen = [shared] + r.sample(rest, min(k - 1, len(rest)))
                else:
                    chosen = r.sample(pool, min(k, len(pool)))
                r.shuffle(chosen)
            for i in chosen:
                use_count[i] += 1
            self.paths.append(PathRef(
                path_id=f"p{p}", src="1-ff00:0:110", dst="1-ff00:0:120",
                interfaces=tuple(chosen),
                expiry_s=r.uniform(300.0, 2400.0), mtu=1472,
            ))

        self._use_count = use_count

    def universal_interfaces(self) -> list[str]:
        """Interfaces that lie on every path. Should be empty by construction;
        exposed so tests can assert that."""
        n = len(self.paths)
        return [i for i in self.interfaces
                if n and all(i in p.interfaces for p in self.paths)]

    # ---------------- dynamics ----------------

    def _seasonal(self, iid: str) -> float:
        if self.drift <= 0:
            return 1.0
        h = (hash(iid) % 997) / 997.0
        return 1.0 + self.drift * math.sin(2 * math.pi * (self.t / 37.0 + h))

    def link_metrics(self, iid: str, extra_load: float) -> tuple[float, float, float]:
        """(latency_ms, available_bw_mbps, loss) for one link under load.

        ``extra_load`` is the PAS-attributable fraction of capacity offered on
        top of the exogenous background.
        """
        ls = self.links[iid]
        util = min(0.985, max(0.0, ls.background + extra_load))
        season = self._seasonal(iid)
        lat = ls.base_latency_ms * season * (1.0 + 0.9 * (util / 0.85) ** 3)
        avail = ls.capacity_mbps * max(0.02, 1.0 - util)
        loss = min(0.5, ls.base_loss + 0.06 * max(0.0, util - 0.75) ** 2 * 40.0)
        return lat, avail, loss

    def path_metrics(self, path: PathRef, demand: Demand | None = None) -> tuple[float, float, float]:
        """Compose link metrics along a path.

        latency = sum, bandwidth = min, loss = 1 - prod(1 - p).
        This is the ground truth that probe R2's composition test refers to.
        """
        # traffic on a link is the sum over paths using it
        lat_tot, bw_min, surv = 0.0, float("inf"), 1.0
        for iid in path.interfaces:
            load = 0.0
            if demand is not None:
                nd = demand.normalised()
                for p in self.paths:
                    if iid in p.interfaces:
                        load += nd.get(p.path_id, 0.0)
                load *= 0.9   # scale offered load into a fraction of capacity
            lat, bw, ls = self.link_metrics(iid, load)
            lat_tot += lat
            bw_min = min(bw_min, bw)
            surv *= (1.0 - ls)
        return lat_tot, (0.0 if bw_min == float("inf") else bw_min), 1.0 - surv

    # ---------------- harness-facing API ----------------

    def snapshot(self) -> TopologySnapshot:
        return TopologySnapshot(
            t=self.t,
            interfaces=dict(self.interfaces),
            paths=tuple(self.paths),
        )

    def observe(
        self,
        paths: Sequence[PathRef] | None = None,
        demand: Demand | None = None,
        coverage: float = 1.0,
        noise: float = 0.02,
        source: str = "bbr",
    ) -> list[Observation]:
        """Emit measurements.

        ``coverage`` < 1 drops paths at random, reproducing the sparsity of
        real telemetry (you only measure paths somebody is using).
        """
        paths = list(paths if paths is not None else self.paths)
        out: list[Observation] = []
        for p in paths:
            if self.rng.random() > coverage:
                continue
            lat, bw, ls = self.path_metrics(p, demand)

            def jitter(v: float) -> float:
                return v * (1.0 + self.rng.gauss(0.0, noise))

            out.append(Observation(
                t=self.t, path_id=p.path_id,
                latency_ms=jitter(lat), throughput_mbps=jitter(bw),
                loss=max(0.0, jitter(ls)),
                source=source,
            ))
        return out

    def step(self, dt: float = 1.0) -> None:
        self.t += dt

    # ---------------- mutation helpers used by probes ----------------

    def perturb_link(self, iid: str, latency_factor: float = 3.0) -> None:
        """Make one link much worse.  Probe R1 checks the model notices on
        exactly the paths that use it and not on the ones that do not."""
        ls = self.links[iid]
        self.links[iid] = LinkState(
            capacity_mbps=ls.capacity_mbps,
            base_latency_ms=ls.base_latency_ms * latency_factor,
            base_loss=ls.base_loss,
            background=min(0.95, ls.background * 1.6),
        )

    def add_new_interfaces(self, n: int = 3) -> list[str]:
        """Introduce interfaces that did not exist at reset time.

        This is the churn that probe R4 tests against.
        """
        r = self.rng
        new: list[str] = []
        start = len(self.interfaces)
        for i in range(n):
            iid = f"ifNEW{start + i:03d}"
            cap = r.choice([100.0, 400.0, 1000.0])
            self.interfaces[iid] = InterfaceAttrs(
                iface_id=iid, as_id=f"1-ff00:0:{900 + i:x}", isd=1,
                link_type=LINK_TYPES[i % 3],
                declared_bw_mbps=cap, declared_latency_ms=r.uniform(2.0, 25.0),
                mtu=1472,
            )
            self.links[iid] = LinkState(cap, r.uniform(2.0, 25.0),
                                        r.uniform(0.0, 0.004), r.uniform(0.05, 0.3))
            new.append(iid)
        return new

    def add_path_using(self, ifaces: Sequence[str], path_id: str) -> PathRef:
        p = PathRef(path_id=path_id, src="1-ff00:0:110", dst="1-ff00:0:120",
                    interfaces=tuple(ifaces), expiry_s=1200.0, mtu=1472)
        self.paths.append(p)
        return p

    def uniform_demand(self) -> Demand:
        n = len(self.paths)
        return Demand({p.path_id: 1.0 / n for p in self.paths}, n_hosts=1)

    def concentrated_demand(self, path_id: str, mass: float = 0.9) -> Demand:
        n = len(self.paths)
        rest = (1.0 - mass) / max(1, n - 1)
        return Demand(
            {p.path_id: (mass if p.path_id == path_id else rest) for p in self.paths},
            n_hosts=1,
        )
