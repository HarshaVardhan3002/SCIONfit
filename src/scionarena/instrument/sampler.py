"""Series sampled on the world's own clock, not on the model's turn.

M3 sampled every tracked link once per decision round: run the turn, advance to
the round's deadline, write down what the world looked like. That is correct
exactly while every round fits inside the cadence, and it stops being correct the
moment one does not -- the sample then lands wherever the model's turn happened
to finish, the series is still the right length, the numbers are still
plausible, and every detector downstream is reading a frequency axis that does
not mean what it says. M3 papered over it by widening the cadence until the
rounds fit (``LoopConfig.adaptive_cadence``), which works until a model's rounds
get more expensive as the episode runs, which the stochastic reference model's
do. See ADR 0011.

The fix here is to stop asking the model's turn where the samples go. A
:class:`Sampler` registers as a substrate tap and fires when the *world's* clock
reaches a grid point, whoever moved it there and whatever it cost. A round that
overruns by four minutes is now eight samples of a network being neglected
rather than one sample taken late, and the grid survives either way.

Two properties make that work, and both are load-bearing:

*Grid points are always tick boundaries.* ``Substrate.step`` subdivides any
advance onto multiples of ``scenario.step_s`` rather than jumping to wherever the
caller stopped, so if ``interval_s`` is a multiple of ``step_s`` then passing a
grid point necessarily produces a tick exactly on it. The constructor enforces
the multiple; without it the sampler would quietly drift.

*A skipped grid point is counted, never interpolated.* If the clock somehow
crosses a grid point without a tick there, the sample is taken late and
:meth:`Series.uniform` reports the series as non-uniform for the rest of the run.
Nothing is invented to fill the hole: a detector reading a made-up sample is the
failure this module exists to remove, not a smaller version of it.

Nothing here is visible to the model. The sampler reads the substrate directly,
which is the one thing a model may never do (seam B), and that asymmetry is the
whole reason instrumentation is allowed to be this cheap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from scionarena.core.scenario import Substrate

__all__ = ["Series", "Sampler", "GRID_EPS", "path_name"]


def path_name(path_id: int) -> str:
    """A path identifier as the *model* sees it.

    ``instrument`` may not import ``exposure``, so the rendering the session
    uses is written down twice, and
    ``tests/test_metrics.py::test_the_truth_series_is_keyed_the_way_a_model_names_a_path``
    is the only thing keeping the copies equal. They have to match exactly: a
    truth series keyed on the raw integer and a forecast keyed on the hex string
    never join, and every accuracy metric silently reports ``None`` -- which is
    how this was found.
    """
    return f"{path_id & 0xFFFFFFFFFFFFFFFF:016x}"


#: Slack when deciding whether a tick landed on a grid point. Times are
#: accumulated in floating point over runs of thousands of seconds, so an exact
#: comparison would miss grid points by a few parts in 10^16 and report a
#: perfectly even series as ragged.
GRID_EPS: float = 1e-6


@dataclass
class Series:
    """What the world did, on a grid the world chose. Never read by the model."""

    #: Simulated seconds between grid points. The x axis every detector assumes.
    interval_s: float
    #: When each sample was actually taken. Carried rather than reconstructed
    #: from ``interval_s`` because the difference between the two is the only
    #: evidence that the grid held, and a series that reconstructs its own x
    #: axis can never report that it did not.
    times: list[float] = field(default_factory=list)
    #: Interface -> total utilisation, background included. What an operator's
    #: own graph would show, and what the figures plot.
    utilisation: dict[int, list[float]] = field(default_factory=dict)
    #: Interface -> load from the advised populations alone, over capacity. What
    #: the model caused rather than what happened, and so what the detectors
    #: read. ADR 0010 decision 3.
    advised_load: dict[int, list[float]] = field(default_factory=dict)
    #: Scope -> realised share of that scope's *first* path. A fixed reference
    #: path, not the busiest one: for a winner-take-all model the busiest path's
    #: share is 1.0 every round whichever path is winning, which hides the
    #: swapping this is here to see.
    path_share: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    #: Per sample, over every scope: the largest gap between intended and realised.
    deviation: list[float] = field(default_factory=list)
    #: Per sample: load-weighted mean path cost. What the model is nominally
    #: optimising, and what oscillation destroys.
    mean_cost_ms: list[float] = field(default_factory=list)
    #: Per sample: the cheapest path each scope had, averaged over scopes. The
    #: hindsight-optimal *fixed* assignment, and therefore a lower bound on
    #: achievable cost -- moving the whole scope onto it would have raised it.
    #: Regret measured against this is an upper bound on true regret, which is
    #: what ADR 0017 chose and what ``regret_ms`` is documented as.
    best_cost_ms: list[float] = field(default_factory=list)
    #: ``(src, dst, path_id)`` -> that path's true cost at each sample, in ms.
    #: The truth an accuracy metric scores a forecast against. Bounded by
    #: construction: only the scopes being driven, and only the paths the model
    #: was shown. A path that appears or vanishes mid-run is padded with NaN
    #: rather than dropped, so every series is the same length as ``times`` and
    #: "not present" stays distinguishable from "cost zero".
    path_cost: dict[tuple[str, str, str], list[float]] = field(default_factory=dict)
    #: Grid points the clock passed without a tick landing on them. Zero unless
    #: something advanced time behind the substrate's back.
    skipped: int = 0
    #: Topology-change events crossed. Each one renumbers interfaces, so the
    #: series before it and the series after it are not series of the same thing.
    severed: int = 0

    def __len__(self) -> int:
        return len(self.times)

    def uniform(self, *, tolerance: float = GRID_EPS) -> bool:
        """Did every sample land on the grid, and so is the spectrum real?

        Every detector assumes uniform sampling and none of them can tell when
        that is untrue. This is the check they cannot do for themselves, and it
        is answered from the recorded times rather than inferred from how many
        rounds overran -- which is what M3 did, and which was a proxy for the
        wrong thing once the sampler stopped depending on the rounds at all.
        """
        if len(self.times) < 2:
            return True
        gaps = np.diff(np.asarray(self.times, dtype=np.float64))
        return bool(np.all(np.abs(gaps - self.interval_s) <= tolerance))

    def jitter_s(self) -> float:
        """Worst deviation of a gap from ``interval_s``. 0.0 on a clean grid."""
        if len(self.times) < 2:
            return 0.0
        gaps = np.diff(np.asarray(self.times, dtype=np.float64))
        return float(np.max(np.abs(gaps - self.interval_s)))

    def continuous(self) -> bool:
        """Whether the whole series describes one topology."""
        return self.severed == 0


class Sampler:
    """Records :class:`Series` from a substrate tap, on a fixed simulated grid.

    The grid is multiples of ``interval_s`` in absolute simulated time, not
    offsets from wherever the sampler was attached. Two reasons, and the first
    one is a bug that this cost an hour of:

    *Only absolute multiples are guaranteed to be tick boundaries.* Attaching
    after a single tool call anchors the grid at something like ``t=0.13``, every
    grid point then falls between ticks, and every sample lands at the next tick
    after it -- a series with a second of jitter on a thirty-second grid, which is
    exactly the failure this module was written to remove.

    *Two runs that spent their setup differently sample the same instants.* Which
    is the same argument ``Substrate._tick`` already makes for the load grid.

    Attach after the world is in whatever state the episode should start from;
    the first sample is taken at the next grid point strictly after that, so
    nothing records the uniform split the loop begins with as though a model had
    chosen it.
    """

    def __init__(
        self,
        world: Substrate,
        *,
        interval_s: float,
        tracked: list[int],
        scopes: list[tuple[str, str]],
        indices: list[tuple[int, int]],
    ) -> None:
        step = world.scenario.step_s
        if not (interval_s > 0.0 and math.isfinite(interval_s)):
            raise ValueError(f"sample interval must be finite and positive, got {interval_s}")
        if abs(interval_s / step - round(interval_s / step)) > 1e-9:
            raise ValueError(
                f"sample interval {interval_s}s must be a whole multiple of the "
                f"scenario's step of {step}s, or grid points will fall between "
                "ticks and the samples will drift off the grid"
            )
        self.world = world
        self.interval_s = float(interval_s)
        self.tracked = [int(i) for i in tracked]
        self.scopes = list(scopes)
        self.indices = list(indices)
        self.series = Series(
            interval_s=self.interval_s,
            utilisation={int(i): [] for i in tracked},
            advised_load={int(i): [] for i in tracked},
            path_share={scope: [] for scope in scopes},
        )
        #: Index of the next grid point in absolute time, so the next sample is
        #: due at ``_n * interval_s``. Set on attach.
        self._n: int | None = None
        self._generation = world.generation

    # ---- attachment -----------------------------------------------------

    def attach(self) -> None:
        """Start sampling at the first absolute grid point after now."""
        if self._n is not None:
            return
        self._n = int(math.floor(self.world.now / self.interval_s + GRID_EPS)) + 1
        self.world.taps.append(self._on_tick)

    def detach(self) -> None:
        if self._on_tick in self.world.taps:
            self.world.taps.remove(self._on_tick)

    def __enter__(self) -> Sampler:
        self.attach()
        return self

    def __exit__(self, *exc: object) -> None:
        self.detach()

    # ---- the tap --------------------------------------------------------

    def _on_tick(self, world: Substrate) -> None:
        """Called at the end of every substrate tick. Cheap unless it is due.

        This runs on the hot path -- every tool call ticks the world -- so the
        common case has to be one subtraction and a comparison.
        """
        assert self._n is not None
        due = self._n * self.interval_s
        now = world.now
        if now + GRID_EPS < due:
            return
        # How many grid points this tick reached. More than one means the clock
        # crossed a grid point without stopping on it; the samples for those are
        # gone and the gap in ``times`` is what says so.
        passed = int(math.floor((now - due) / self.interval_s + GRID_EPS)) + 1
        self._n += passed
        self.series.skipped += passed - 1
        if world.generation != self._generation:
            # A topology change renumbers interfaces, so the series before the
            # break and the series after it are not series of the same thing.
            # Sampling continues -- the run continues -- and
            # ``Series.continuous`` is how a detector finds out that reading
            # across the break means reading two different networks. Rare and
            # planned, per ADR 0002.
            self.series.severed += 1
            self._generation = world.generation
        self._record(now, world)

    def _record(self, t: float, world: Substrate) -> None:
        links = world.links
        utilisation = links.utilisation()
        capacity = links.usable_capacity_mbps()
        advised = links.demand_mbps
        cost = links.cost()
        n_ifaces = int(links.n_ifaces)
        self.series.times.append(float(t))
        for iface in self.tracked:
            # An interface a topology change deleted samples as NaN rather than
            # as zero. Zero is a real reading -- an idle link -- and a detector
            # cannot tell the two apart, so recording one as the other would put
            # a fabricated quiet stretch into the middle of the series.
            if iface >= n_ifaces:
                self.series.utilisation[iface].append(float("nan"))
                self.series.advised_load[iface].append(float("nan"))
                continue
            self.series.utilisation[iface].append(float(utilisation[iface]))
            self.series.advised_load[iface].append(float(advised[iface] / capacity[iface]))

        deviations: list[float] = []
        costs: list[float] = []
        best: list[float] = []
        for scope, (a, b) in zip(self.scopes, self.indices, strict=True):
            state = world.hosts.scope(a, b)
            if state is None or state.n_paths == 0 or state.counts.sum() <= 0:
                self.series.path_share[scope].append(0.0)
                continue
            shares = state.counts / int(state.counts.sum())
            self.series.path_share[scope].append(float(shares[0]))
            deviations.append(state.deviation())
            if state.ifaces.size:
                per_path = np.bincount(
                    state.owner, weights=cost[state.ifaces], minlength=state.n_paths
                )
                costs.append(float((per_path * shares).sum()))
                best.append(float(per_path.min()))
                for index, path_id in enumerate(state.path_ids):
                    self._push((scope[0], scope[1], path_name(path_id)), float(per_path[index]))
        self.series.deviation.append(max(deviations, default=0.0))
        self.series.mean_cost_ms.append(float(np.mean(costs)) if costs else float("nan"))
        self.series.best_cost_ms.append(float(np.mean(best)) if best else float("nan"))
        self._pad()

    def _push(self, key: tuple[str, str, str], value: float) -> None:
        """Record one path's true cost, back-filling a path that appeared late."""
        track = self.series.path_cost.get(key)
        if track is None:
            # ``times`` already has this sample appended, so the back-fill is
            # everything before it.
            track = [float("nan")] * (len(self.series.times) - 1)
            self.series.path_cost[key] = track
        track.append(value)

    def _pad(self) -> None:
        """NaN for every path that did not exist this sample. Not zero: zero is
        a real cost and a detector cannot tell a fabricated one from a measured
        one."""
        want = len(self.series.times)
        for track in self.series.path_cost.values():
            if len(track) < want:
                track.extend([float("nan")] * (want - len(track)))
