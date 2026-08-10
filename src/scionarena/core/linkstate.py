"""Offered load to observable metrics, per link direction.

The one property that is not negotiable: **cost is non-decreasing in offered
load**. Probe R7 tests models against it, so a substrate that violated it would
make the probe meaningless -- a model could pass by being wrong in the same way
the world was. There is a property test over 10,000 random states.

Everything is indexed by **interface id**, not link id, because forward and
reverse availability differ and an interface is exactly one direction of one
link. Interface ``2k`` is link ``k`` leaving its ``link_a`` end; ``2k+1`` is the
same link leaving ``link_b``. So ``state[iface]`` is the state a packet entering
at that interface will experience.

Background traffic is exogenous, diurnal, weekly and seeded, and **the model
cannot read it**. It is folded into offered load before any metric is computed
and is never exposed as a field. A model that could subtract the background out
would be solving a different, much easier problem than the deployed one.

The functional form -- BPR with a queueing tail -- is a modelling choice, not a
fact about networks. ADR 0006 records why this one and what it costs. Every
parameter is in :class:`LinkParams` so M9 can fit them against tier-0 traces
rather than arguing about them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Final

import numpy as np
from numpy.typing import NDArray

from .topology import Topology

__all__ = [
    "LinkParams",
    "BackgroundParams",
    "LinkState",
    "PathMetrics",
    "fit_bpr",
    "SECONDS_PER_DAY",
    "SECONDS_PER_WEEK",
]

SECONDS_PER_DAY: Final = 86_400.0
SECONDS_PER_WEEK: Final = 7 * SECONDS_PER_DAY

#: Utilisation is clipped here before any metric is computed. At exactly 1.0 the
#: queueing term is a division by zero, and past it the BPR form has no meaning
#: anyway: a link offered twice its capacity is not twice as slow, it is
#: dropping packets, which the loss term handles.
UTILISATION_CEILING: Final = 0.995

#: Standard deviation of a link's diurnal phase, in days. Roughly +/- 2 hours,
#: which is a plausible spread of time zones and traffic mixes and still leaves
#: a network-wide day.
PHASE_SPREAD: Final = 0.09


@dataclass(frozen=True)
class LinkParams:
    """The load-to-metrics form. Every number here is a calibration target.

    ``ASSUMPTION(Q2)``: the defaults are plausible for a congested backbone link
    and are not fitted to anything. M9 replaces them via :func:`fit_bpr` and the
    tier-0 traces; until then, absolute latencies are indicative and only the
    *shape* -- monotone, convex, blowing up near capacity -- is load-bearing.
    """

    #: BPR: latency = free_flow * (1 + alpha * u**beta)
    alpha: float = 0.15
    beta: float = 4.0
    #: Queueing tail, in ms at the ceiling. BPR alone is too gentle near
    #: capacity: it is a road-traffic model and roads do not have buffers.
    queue_ms: float = 8.0
    queue_exponent: float = 2.0
    #: Loss is zero below onset, then rises with the exponent to max_loss.
    loss_onset: float = 0.85
    loss_exponent: float = 2.0
    max_loss: float = 0.4
    #: Loss that has nothing to do with load. Real links are not perfect.
    base_loss: float = 1e-5
    #: How much a lost packet is worth, in milliseconds, when metrics are
    #: collapsed to one number. Only used by :meth:`LinkState.cost`.
    loss_penalty_ms: float = 500.0

    def __post_init__(self) -> None:
        if self.alpha < 0 or self.queue_ms < 0:
            raise ValueError("congestion terms must not be negative, or cost could fall with load")
        if self.beta <= 0 or self.queue_exponent <= 0 or self.loss_exponent <= 0:
            raise ValueError("exponents must be positive")
        if not 0.0 <= self.loss_onset < 1.0:
            raise ValueError("loss onset is a utilisation, in [0, 1)")
        if not 0.0 <= self.base_loss <= self.max_loss <= 1.0:
            raise ValueError("losses are probabilities and base_loss must not exceed max_loss")


@dataclass(frozen=True)
class BackgroundParams:
    """Exogenous traffic. Seeded, diurnal, weekly, and invisible to the model."""

    #: Mean utilisation from traffic nobody in the scenario controls.
    mean_utilisation: float = 0.35
    #: Peak-to-mean swing over a day, as a fraction of the mean.
    diurnal_amplitude: float = 0.5
    #: Weekend traffic relative to a weekday.
    weekend_factor: float = 0.75
    #: Multiplicative noise, resampled each bucket.
    noise_sigma: float = 0.08
    #: How long the background holds still. Shorter is not more realistic, it
    #: just costs more: real background traffic is autocorrelated over minutes.
    bucket_s: float = 60.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.mean_utilisation < 1.0:
            raise ValueError("mean background utilisation is in [0, 1)")
        if self.bucket_s <= 0:
            raise ValueError("bucket length must be positive")


@dataclass(frozen=True)
class PathMetrics:
    """What a path looks like end to end. Composition rules, in one place:
    latency sums, bandwidth minimises, loss compounds."""

    latency_ms: float
    bandwidth_mbps: float
    loss: float
    mtu: int
    hop_count: int


def fit_bpr(
    utilisation: NDArray[np.float64],
    latency_ratio: NDArray[np.float64],
    *,
    beta_grid: NDArray[np.float64] | None = None,
) -> tuple[float, float]:
    """Least-squares fit of ``(alpha, beta)`` to measured latency inflation.

    ``latency_ratio`` is measured latency over free-flow latency. This is the
    calibration hook M9 needs: given tier-0 traces it returns parameters that
    can go straight into :class:`LinkParams`. Grid over beta, closed form for
    alpha at each -- the problem is one-dimensional once beta is fixed and this
    avoids a scipy dependency in ``core/``.
    """
    u = np.clip(np.asarray(utilisation, dtype=np.float64), 0.0, UTILISATION_CEILING)
    y = np.asarray(latency_ratio, dtype=np.float64) - 1.0
    if u.shape != y.shape or u.size == 0:
        raise ValueError("utilisation and latency_ratio must be the same non-empty shape")
    grid = np.linspace(1.0, 8.0, 71) if beta_grid is None else np.asarray(beta_grid, np.float64)
    best = (float("inf"), 0.0, grid[0])
    for beta in grid:
        x = u**beta
        denominator = float(x @ x)
        alpha = 0.0 if denominator == 0.0 else max(0.0, float(x @ y) / denominator)
        residual = float(np.sum((y - alpha * x) ** 2))
        if residual < best[0]:
            best = (residual, alpha, float(beta))
    return best[1], best[2]


class LinkState:
    """Per-direction link state: capacity, load, background, and what they mean.

    Array-backed and indexed by interface id. Nothing here holds a Python object
    per link; at the stress tier that would be 200,000 of them on the hot path.
    """

    def __init__(
        self,
        topology: Topology,
        *,
        seed: int = 0,
        params: LinkParams | None = None,
        background: BackgroundParams | None = None,
        t0: float = 0.0,
    ) -> None:
        self.topology = topology
        self.params = params or LinkParams()
        self.background_params = background or BackgroundParams()
        self.seed = seed
        self.t = t0

        n_ifaces = topology.n_ifaces
        #: Capacity per direction. Symmetric to start with; scenarios may make
        #: it asymmetric, which is why it is not just the link array.
        self.capacity_mbps = np.repeat(np.asarray(topology.link_capacity_mbps, np.float64), 2)
        self.free_flow_ms = np.repeat(np.asarray(topology.link_latency_ms, np.float64), 2)
        self.mtu = np.repeat(np.asarray(topology.link_mtu, np.int32), 2)
        #: Scenario degradation: a multiplier on usable capacity. 1.0 is healthy,
        #: 0.0 is down. Separate from load so "congested" and "damaged" are
        #: distinguishable, which they are not from the outside and have to be
        #: from the inside.
        self.health = np.ones(n_ifaces, dtype=np.float64)
        #: Load the scenario's hosts are offering. The model's recommendations
        #: land here, through the closed loop in M3.
        self.demand_mbps = np.zeros(n_ifaces, dtype=np.float64)
        #: Exogenous traffic. Deliberately private: see the module docstring.
        self._background_mbps = np.zeros(n_ifaces, dtype=np.float64)
        self._bucket = -1
        #: Metric arrays, dropped whenever load, health or time moves.
        self._metric_cache: dict[str, NDArray[np.float64]] = {}
        #: Per-interface diurnal phase and weight, drawn once. Two links do not
        #: peak at the same moment; a world where they did would let a model
        #: infer the whole background from one observation.
        #:
        #: The spread is deliberately narrow around a common phase rather than
        #: uniform over the day. Uniform phases cancel in aggregate and the
        #: network as a whole has no day at all, which is both unrealistic and
        #: convenient in the wrong direction: a model that could average over
        #: links to recover a flat background would never have to learn that
        #: load has a time of day.
        rng = np.random.default_rng([seed, 0xB4CE])
        self._phase = rng.normal(0.0, PHASE_SPREAD, size=n_ifaces)
        self._weight = rng.gamma(shape=4.0, scale=0.25, size=n_ifaces)
        self._refresh_background(t0)

    # ------------------------------------------------------------------ sizes

    @property
    def n_ifaces(self) -> int:
        return int(self.capacity_mbps.size)

    @property
    def nbytes(self) -> int:
        return int(
            sum(
                array.nbytes
                for array in (
                    self.capacity_mbps,
                    self.free_flow_ms,
                    self.mtu,
                    self.health,
                    self.demand_mbps,
                    self._background_mbps,
                    self._phase,
                    self._weight,
                )
            )
        )

    def __repr__(self) -> str:
        return (
            f"LinkState({self.n_ifaces} directions, t={self.t:.1f}s, "
            f"mean utilisation {float(self.utilisation().mean()):.2f})"
        )

    # ------------------------------------------------------------------- time

    def advance_to(self, t: float) -> None:
        if t < self.t:
            raise ValueError("time does not run backwards")
        self.t = t
        self._refresh_background(t)

    def _refresh_background(self, t: float) -> None:
        """Resample the exogenous load if the bucket has turned over.

        Counter-based, not stateful: the draw depends on ``(seed, bucket)`` and
        nothing else, so two runs that step through time differently still see
        the same background at the same instant. Threading one RNG through the
        steps would make the world depend on how often it was asked, and
        invariant 4 would hold only for identical step sequences.
        """
        bucket = int(t // self.background_params.bucket_s)
        if bucket == self._bucket:
            return
        self._bucket = bucket
        self._invalidate()
        bp = self.background_params
        centre = bucket * bp.bucket_s + bp.bucket_s / 2

        day_fraction = (centre % SECONDS_PER_DAY) / SECONDS_PER_DAY
        diurnal = 1.0 + bp.diurnal_amplitude * np.sin(2 * np.pi * (day_fraction - self._phase))
        weekday = int(centre // SECONDS_PER_DAY) % 7
        weekly = bp.weekend_factor if weekday >= 5 else 1.0

        rng = np.random.default_rng([self.seed, 0xBACC, bucket])
        noise = 1.0 + bp.noise_sigma * rng.standard_normal(self.n_ifaces)

        utilisation = bp.mean_utilisation * self._weight * diurnal * weekly * noise
        np.clip(utilisation, 0.0, UTILISATION_CEILING, out=utilisation)
        self._background_mbps = utilisation * self.capacity_mbps

    # ------------------------------------------------------------------- load

    def set_demand(self, iface: int | NDArray[np.int_], mbps: float | NDArray[np.float64]) -> None:
        self.demand_mbps[iface] = mbps
        self._invalidate()

    def add_demand(self, iface: int | NDArray[np.int_], mbps: float | NDArray[np.float64]) -> None:
        np.add.at(self.demand_mbps, iface, mbps)
        self._invalidate()

    def set_all_demand(self, mbps: NDArray[np.float64]) -> None:
        """Replace the whole demand array at once.

        What the closed loop writes every step: the host population recomputes
        its offered load from scratch rather than adjusting it, so the array
        arrives complete and an incremental setter would only invite drift.
        """
        if mbps.shape != self.demand_mbps.shape:
            raise ValueError(
                f"demand array has shape {mbps.shape}, and this link state has "
                f"{self.n_ifaces} directions"
            )
        self.demand_mbps = np.asarray(mbps, dtype=np.float64)
        self._invalidate()

    def clear_demand(self) -> None:
        self.demand_mbps.fill(0.0)
        self._invalidate()

    def offered_mbps(self) -> NDArray[np.float64]:
        """What the link is actually carrying. Demand plus the background the
        model cannot see."""
        return self.demand_mbps + self._background_mbps

    def usable_capacity_mbps(self) -> NDArray[np.float64]:
        return self.capacity_mbps * self.health

    def utilisation(self) -> NDArray[np.float64]:
        cached = self._metric_cache.get("utilisation")
        if cached is not None:
            return cached
        usable = self.usable_capacity_mbps()
        with np.errstate(divide="ignore", invalid="ignore"):
            u = np.where(usable > 0.0, self.offered_mbps() / usable, np.inf)
        return self._remember("utilisation", np.clip(u, 0.0, UTILISATION_CEILING))

    # ---------------------------------------------------------------- metrics

    def _invalidate(self) -> None:
        """Anything that changes load, health or time drops the metric arrays.

        They are memoised because composing a scope's paths asks for them once
        per scope, and recomputing 40,000 directions per scope per step is the
        difference between the step budget and twenty times over it.
        """
        self._metric_cache.clear()

    def _remember(self, name: str, value: NDArray[np.float64]) -> NDArray[np.float64]:
        value.flags.writeable = False  # a caller mutating a cached array is a bug
        self._metric_cache[name] = value
        return value

    def latency_ms(self) -> NDArray[np.float64]:
        """Free-flow plus BPR congestion plus a queueing tail.

        Monotone non-decreasing in offered load by construction: both added
        terms are non-negative powers of a utilisation that only rises with
        load, and the tail's denominator only shrinks.
        """
        cached = self._metric_cache.get("latency")
        if cached is not None:
            return cached
        p = self.params
        u = self.utilisation()
        bpr = 1.0 + p.alpha * u**p.beta
        tail = p.queue_ms * u**p.queue_exponent / np.maximum(1.0 - u, 1.0 - UTILISATION_CEILING)
        return self._remember("latency", self.free_flow_ms * bpr + tail)

    def loss(self) -> NDArray[np.float64]:
        """Zero-ish until the onset, then convex to ``max_loss``."""
        cached = self._metric_cache.get("loss")
        if cached is not None:
            return cached
        p = self.params
        u = self.utilisation()
        over = np.maximum(u - p.loss_onset, 0.0) / max(1.0 - p.loss_onset, 1e-9)
        congestion_loss = p.max_loss * np.minimum(over, 1.0) ** p.loss_exponent
        return self._remember("loss", np.minimum(p.base_loss + congestion_loss, 1.0))

    def available_mbps(self) -> NDArray[np.float64]:
        cached = self._metric_cache.get("available")
        if cached is not None:
            return cached
        available = np.maximum(self.usable_capacity_mbps() - self.offered_mbps(), 0.0)
        return self._remember("available", available)

    def cost(self) -> NDArray[np.float64]:
        """Metrics collapsed to one number, for the monotonicity property.

        Not what a model is shown -- summarising is invariant 1's business and
        the answer there is no. This exists so "cost is non-decreasing in
        offered load" is a statement about something, and so scenario code has a
        scalar to sort on.
        """
        return self.latency_ms() + self.params.loss_penalty_ms * self.loss()

    # ------------------------------------------------------------ composition

    def path_metrics(self, ifaces: tuple[int, ...] | NDArray[np.int_]) -> PathMetrics:
        """Compose one path's hops. Latency sums, bandwidth minimises, loss
        compounds -- ``1 - prod(1 - loss)``, not a sum, because two 10% hops lose
        19%, not 20%, and the difference compounds over long paths."""
        egress = np.asarray(ifaces, dtype=np.int64)[::2] if len(ifaces) else np.zeros(0, np.int64)
        if egress.size == 0:
            return PathMetrics(0.0, float("inf"), 0.0, int(self.mtu.max(initial=0)), 0)
        latency = self.latency_ms()[egress]
        losses = self.loss()[egress]
        return PathMetrics(
            latency_ms=float(latency.sum()),
            bandwidth_mbps=float(self.available_mbps()[egress].min()),
            loss=float(1.0 - np.prod(1.0 - losses)),
            mtu=int(self.mtu[egress].min()),
            hop_count=int(egress.size),
        )

    def path_metrics_batch(
        self, paths: list[tuple[int, ...]]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
        """The same thing for many paths at once, which is the normal case: a
        scope has hundreds of paths and each is asked about every step.

        Flattened with an index array rather than looped, because a Python loop
        over 300 paths per scope per step is the difference between the step
        budget and twenty times the step budget.
        """
        if not paths:
            empty = np.zeros(0, dtype=np.float64)
            return empty, empty.copy(), empty.copy()
        egress = [np.asarray(p, dtype=np.int64)[::2] for p in paths]
        lengths = np.array([e.size for e in egress], dtype=np.int64)
        flat = np.concatenate(egress) if lengths.sum() else np.zeros(0, dtype=np.int64)
        owner = np.repeat(np.arange(len(paths)), lengths)

        latency = self.latency_ms()[flat]
        survival = 1.0 - self.loss()[flat]
        available = self.available_mbps()[flat]

        n = len(paths)
        total_latency = np.bincount(owner, weights=latency, minlength=n).astype(np.float64)
        log_survival = np.bincount(
            owner, weights=np.log(np.maximum(survival, 1e-12)), minlength=n
        ).astype(np.float64)
        bandwidth = np.full(n, np.inf, dtype=np.float64)
        np.minimum.at(bandwidth, owner, available)
        loss = (1.0 - np.exp(log_survival)).astype(np.float64)
        return total_latency, bandwidth, loss

    # ------------------------------------------------------- scenario control

    def degrade(self, link: int, factor: float, *, direction: int | None = None) -> None:
        """A declared scenario event: a link loses capacity.

        Directional by default in the sense that it can be: real degradation is
        often one-way, and a substrate that could only fail links symmetrically
        would never produce the case a model most needs to handle.
        """
        if not 0.0 <= factor <= 1.0:
            raise ValueError("health factor is in [0, 1]")
        if direction is None:
            self.health[2 * link : 2 * link + 2] = factor
        else:
            self.health[2 * link + direction] = factor
        self._invalidate()

    def restore(self, link: int | None = None) -> None:
        if link is None:
            self.health.fill(1.0)
        else:
            self.health[2 * link : 2 * link + 2] = 1.0
        self._invalidate()

    def with_params(self, **changes: float) -> LinkState:
        """A copy under different parameters, for calibration and sensitivity.

        Copy rather than mutate: a run whose link model changed underneath it is
        not reproducible from its seed.
        """
        clone = LinkState(
            self.topology,
            seed=self.seed,
            params=replace(self.params, **changes),
            background=self.background_params,
            t0=self.t,
        )
        clone.demand_mbps[:] = self.demand_mbps
        clone.health[:] = self.health
        return clone

    # ------------------------------------------------------------- inspection

    def digest(self) -> str:
        """Content hash of the observable state. Excludes nothing: the
        background is part of the world even though the model cannot read it."""
        h = hashlib.sha256()
        for array in (self.health, self.demand_mbps, self._background_mbps):
            h.update(np.ascontiguousarray(array, dtype=np.float64).tobytes())
        return h.hexdigest()[:16]
