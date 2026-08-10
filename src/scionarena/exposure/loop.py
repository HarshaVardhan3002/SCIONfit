"""The closed loop, run over many scopes at once.

``advisory -> host sampling -> per-link offered load -> link state ->
observations -> advisory``. M2 built every piece of that except the arrow from
the advisory back into the world; ADR 0009 put it in, and this is the driver
that turns the crank and writes down what happened.

Multi-scope is not a convenience here. One scope tells you whether a model
oscillates against itself; a hundred sharing bottleneck links tell you whether
it oscillates against the other ninety-nine, which is the deployment it is
being proposed for. A harness that only ever ran one scope would report the
easy half of the answer.

Two things about the sampling, because the detector depends on both.

*The series is sampled at the decision cadence.* A model gets a turn every
``decision_s`` simulated seconds whatever its calls cost it, and one sample of
every tracked link is taken per turn. Oscillation is a property of a control
loop relative to its own cadence -- "flaps every other decision" -- so a series
sampled at any other rate makes the fast band mean something else.

*The world keeps running underneath.* Between turns the substrate ticks at
``scenario.step_s``, hosts resample, and load moves. A model whose probes cost
it eight seconds does not get eight frozen seconds; it gets a turn that starts
later and an advisory that lands later.

Nothing here summarises anything for the model. The series are read off the
substrate for the report card; the model sees exactly what it asked for through
the tools, as before.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from scionarena.core.hosts import HostParams
from scionarena.core.scenario import Scenario, Substrate
from scionarena.exposure.budget import Budget
from scionarena.exposure.contracts import SLA, Advisory, Observation, PathModel, PathRef
from scionarena.exposure.session import Session, ToolResult, _observations
from scionarena.instrument.detectors import (
    dominant_period,
    fast_swing,
    flap_rate,
    oscillation_index,
)

__all__ = ["LoopConfig", "LoopResult", "run_loop", "busiest_scopes", "compare"]


@dataclass(frozen=True)
class LoopConfig:
    """How to run one episode of the closed loop.

    ``probes_per_cycle`` and ``limit`` are the model's information diet and they
    cost what M2 says they cost, so raising them raises the decision latency
    too. That coupling is deliberate: it is the mechanism behind "a slow model
    is measurably worse", and it is why the two knobs are here rather than
    buried in the driver.
    """

    #: Decision rounds. Also the length of every measured series.
    cycles: int = 240
    #: Simulated seconds per decision round, and the *floor* on the cadence the
    #: loop actually keeps. The samples have to sit on a uniform grid or the
    #: detectors are reading a series that does not exist, so if a round costs
    #: more than this -- and at forty scopes it does, because every scope's
    #: probes are charged -- the cadence is widened once, before the first
    #: sample, and held. See ``adaptive_cadence``.
    decision_s: float = 30.0
    #: Widen ``decision_s`` to fit the round if it does not. Turning this off
    #: does not make the loop faster; it makes it advance by whatever the turns
    #: happened to cost, count every round as an overrun, and hand the FFT an
    #: unevenly sampled series. It exists so that behaviour can be *tested*.
    adaptive_cadence: bool = True
    #: Unsampled rounds run before the episode to find out what a round costs
    #: here. They are thrown away rather than measured, which is the price of
    #: every sampled round sitting on one grid; the world they leave behind is
    #: the world the episode starts from.
    calibration_cycles: int = 3
    limit: int = 20
    probes_per_cycle: int = 2
    n_hosts: int = 100
    sla: SLA = field(default_factory=SLA)
    #: Offered load per scope as a multiple of the spare capacity on its best
    #: path's bottleneck. Set from the topology rather than in Mbps because
    #: link capacities span three orders of magnitude and a fixed figure is
    #: either invisible on a core link or saturating on a customer one, and
    #: expressed against *spare* capacity rather than total because what
    #: decides whether the loop closes is whether a scope can congest the path
    #: it picks. ``None`` uses the scenario's own ``mbps_per_host``.
    target_load: float | None = 0.9
    #: Injected on top of whatever the model's own calls cost it.
    extra_latency_s: float = 0.0
    #: Track this many of the most contested interfaces.
    n_tracked: int = 24
    seed: int = 0


@dataclass
class LoopResult:
    """What one episode did. Written to, never read by, the model."""

    model: str
    scenario: str
    config: LoopConfig
    #: Simulated seconds per decision round as actually kept, which is
    #: ``config.decision_s`` unless the rounds did not fit inside it.
    cadence_s: float = 0.0
    #: Interface index -> total utilisation, one sample per decision round.
    #: Background traffic included: this is what an operator's own graph would
    #: show, and it is what the figure plots.
    utilisation: dict[int, list[float]] = field(default_factory=dict)
    #: Interface index -> load from the advised populations alone, over
    #: capacity. What the model caused rather than what happened, and therefore
    #: what the detectors read. ADR 0010: the background resamples on a bucket
    #: comparable to the decision cadence, so leaving it in the detector's
    #: series measures the environment's noise as if it were the model's.
    advised_load: dict[int, list[float]] = field(default_factory=dict)
    #: Scope -> realised share of that scope's *first* path, one sample per
    #: round. A fixed reference path, not the busiest one: for any
    #: winner-take-all model the busiest path's share is 1.0 every round
    #: whichever path is winning, which hides the swapping this is here to see.
    path_share: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    #: Per round, over every scope: the largest gap between intended and realised.
    deviation: list[float] = field(default_factory=list)
    #: Per round: load-weighted mean path cost. What the model is nominally
    #: optimising, and what oscillation destroys.
    mean_cost_ms: list[float] = field(default_factory=list)
    #: Per round: mean decision latency of the advisories published in it.
    latency_s: list[float] = field(default_factory=list)
    #: Rounds whose turn overran ``decision_s``. Nonzero means the sampling
    #: grid has jitter in it and the spectral numbers are that much softer.
    overruns: int = 0
    wall_clock_s: float = 0.0
    session_summary: dict[str, Any] = field(default_factory=dict)
    hosts_summary: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- detectors

    def swing(self) -> float:
        """**The headline number.** Fast-band amplitude on the worst link.

        In units of link capacity: 0.30 means the busiest tracked interface
        gains and loses thirty points of its capacity from one decision round
        to the next, and keeps doing it. See ADR 0010 for why this and not
        :meth:`oscillation`.

        Worst rather than mean, because a model that flaps one bottleneck and
        leaves twenty-three quiet links alone has still flapped a bottleneck,
        and averaging would hide it behind the quiet ones.
        """
        return max((fast_swing(s) for s in self.advised_load.values()), default=0.0)

    def share_swing(self) -> float:
        """The same amplitude on the realised split rather than on the link.

        Reported alongside because the two answer different questions. The link
        series says whether the *network* is being shaken; the share series says
        whether the *advice* is. A model can move its advice violently and shake
        nothing, if the paths it swaps between do not share a bottleneck --  and
        the reverse, since a scope may be shaken by the other ninety-nine.
        """
        return max((fast_swing(s) for s in self.path_share.values()), default=0.0)

    def oscillation(self) -> float:
        """Spectral peak dominance on the worst link. Kept, but not the headline.

        This is the proposal's published measure and it is reported so that the
        finding recorded in ADR 0010 stays visible: at multi-scope scale it does
        not separate a herding model from a calm one, and has been observed to
        rank them backwards. Believe it at ``scopes == 1``; read :meth:`swing`
        otherwise.
        """
        return max((oscillation_index(s) for s in self.advised_load.values()), default=0.0)

    def share_oscillation(self) -> float:
        return max((oscillation_index(s) for s in self.path_share.values()), default=0.0)

    def swing_by_link(self) -> dict[int, float]:
        return {i: fast_swing(s) for i, s in self.advised_load.items()}

    def period(self) -> float:
        worst = self._worst_link()
        return dominant_period(self.advised_load[worst]) if worst is not None else float("inf")

    def flapping(self) -> float:
        worst = self._worst_link()
        return flap_rate(self.advised_load[worst]) if worst is not None else 0.0

    def _worst_link(self) -> int | None:
        """The interface the headline number came from."""
        by_link = self.swing_by_link()
        return max(by_link, key=lambda k: by_link[k]) if by_link else None

    def mean_deviation(self) -> float:
        return float(np.mean(self.deviation)) if self.deviation else 0.0

    def cost(self) -> float:
        """Mean path cost over the measured part of the run. Lower is better."""
        tail = self.mean_cost_ms[len(self.mean_cost_ms) // 4 :]
        return float(np.mean(tail)) if tail else float("nan")

    def report(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "scenario": self.scenario,
            "cycles": self.config.cycles,
            "decision_s": round(self.cadence_s, 1),
            "scopes": self.hosts_summary.get("scopes", 0),
            "hosts": self.hosts_summary.get("hosts", 0),
            "swing": round(self.swing(), 4),
            "share_swing": round(self.share_swing(), 4),
            "oscillation_index": round(self.oscillation(), 4),
            "share_oscillation": round(self.share_oscillation(), 4),
            "dominant_period": round(self.period(), 2),
            "flap_rate": round(self.flapping(), 4),
            "mean_cost_ms": round(self.cost(), 3),
            "mean_deviation": round(self.mean_deviation(), 4),
            "mean_latency_s": round(float(np.mean(self.latency_s)), 4) if self.latency_s else 0.0,
            "overruns": self.overruns,
            "calls": self.session_summary.get("calls", 0),
            "wall_clock_s": round(self.wall_clock_s, 3),
            "digest": self.session_summary.get("digest", ""),
        }


def busiest_scopes(world: Substrate, n: int, *, min_paths: int = 2) -> list[tuple[str, str]]:
    """Pick ``n`` (src, dst) pairs that actually have a choice to make.

    A scope with one path cannot oscillate and cannot be advised, so including
    them would dilute every measurement with pairs where no model could differ
    from any other. Pairs are drawn in a fixed seeded order, so the same
    scenario yields the same scopes every run.
    """
    topo = world.topology
    picked: list[tuple[str, str]] = []
    rng = np.random.default_rng([world.scenario.seed, 0x5C0])
    tried: set[tuple[int, int]] = set()
    budget = max(20 * n, 200)
    while len(picked) < n and len(tried) < budget:
        a, b = (int(x) for x in rng.integers(0, topo.n_ases, size=2))
        if a == b or (a, b) in tried:
            continue
        tried.add((a, b))
        if len(world.paths_for(a, b, limit=min_paths + 1)) >= min_paths:
            picked.append((topo.as_name(a), topo.as_name(b)))
    return picked


def run_loop(
    model: PathModel,
    scenario: Scenario,
    scopes: Sequence[tuple[str, str]],
    *,
    config: LoopConfig | None = None,
    budget: Budget | None = None,
    host_params: HostParams | None = None,
    world: Substrate | None = None,
    on_cycle: Callable[[int, int], None] | None = None,
) -> LoopResult:
    """Run one model over one scenario for ``cycles`` decision rounds.

    ``on_cycle`` is called with ``(rounds_done, rounds_total)`` after each round
    and is the only way to see inside a run that takes an hour. It is called on
    the driver's own thread and anything it raises propagates, which is how the
    UI cancels a run: there is no other safe point to stop at, because a round
    half-applied is a world nobody asked for.
    """
    cfg = config or LoopConfig()
    world = world if world is not None else scenario.build()
    session = Session(
        world,
        budget=budget if budget is not None else Budget.unlimited(),
        seed=cfg.seed,
        extra_latency_s=cfg.extra_latency_s,
        label=getattr(model.capabilities, "name", "model"),
    )

    indices = [(world.topology.as_index(a), world.topology.as_index(b)) for a, b in scopes]
    base = host_params or scenario.hosts
    for a, b in indices:
        world.add_scope(a, b, params=_params_for(world, a, b, base, cfg))

    result = LoopResult(model=session.label, scenario=scenario.name, config=cfg)
    tracked = _tracked_ifaces(world, indices, cfg.n_tracked)
    result.utilisation = {int(i): [] for i in tracked}
    result.advised_load = {int(i): [] for i in tracked}
    result.path_share = {scope: [] for scope in scopes}

    model.reset(session.view(), seed=cfg.seed)
    # One telemetry subscription for the whole episode. The records are raw and
    # are charged as they arrive, exactly as they would be for a model that
    # subscribed itself; the driver only forwards them to ``observe``.
    subscribed = session.call("subscribe", stream="telemetry")
    handle = str(subscribed.get("handle", "")) if subscribed.ok else ""

    started = time.perf_counter()
    cadence = _calibrate(model, session, scopes, indices, cfg, handle)
    result.cadence_s = cadence

    deadline = session.now + cadence
    for cycle in range(cfg.cycles):
        published = len(session.advisories)
        _turn(model, session, scopes, indices, cfg, cycle, handle)
        if session.now < deadline:
            session.advance(deadline - session.now)
        else:
            result.overruns += 1
        _sample(result, world, session, tracked, scopes, indices, published)
        deadline += cadence
        if on_cycle is not None:
            on_cycle(cycle + 1, cfg.cycles)
    result.wall_clock_s = time.perf_counter() - started
    result.session_summary = session.summary()
    result.hosts_summary = world.hosts.summary()
    return result


def _turn(
    model: PathModel,
    session: Session,
    scopes: Sequence[tuple[str, str]],
    indices: Sequence[tuple[int, int]],
    cfg: LoopConfig,
    cycle: int,
    handle: str,
) -> None:
    """One decision round: drain what arrived, then let the model act per scope."""
    feed = _drain_by_scope(session, handle)
    for (src, dst), _ in zip(scopes, indices, strict=True):
        _one_scope(model, session, src, dst, cycle, cfg, feed.get((src, dst), []))


def _calibrate(
    model: PathModel,
    session: Session,
    scopes: Sequence[tuple[str, str]],
    indices: Sequence[tuple[int, int]],
    cfg: LoopConfig,
    handle: str,
) -> float:
    """Find a cadence the episode can actually keep, by running a few rounds.

    Found at the dev tier with forty scopes, where every one of 240 rounds
    overran a 30 s cadence: probes are charged per scope, so the cost of a round
    grows with the number of scopes, and the world then advanced by whatever the
    turns happened to cost. Nothing about that looks wrong in the output -- the
    series is the right length and the numbers are plausible -- but it is not
    uniformly sampled, and every detector here assumes it is.

    Measuring beats predicting: the cost depends on the model's own diet, on how
    the rate limiter falls out across border routers, and on which tools the
    model chose. Rounds are timed rather than estimated, the worst is taken
    rather than the mean because one slow round in the episode is an overrun,
    and the headroom is 30%.
    """
    if not cfg.adaptive_cadence or cfg.calibration_cycles <= 0:
        return cfg.decision_s
    worst = 0.0
    for cycle in range(cfg.calibration_cycles):
        mark = session.now
        _turn(model, session, scopes, indices, cfg, cycle, handle)
        worst = max(worst, session.now - mark)
        if session.now < mark + cfg.decision_s:
            session.advance(mark + cfg.decision_s - session.now)
    return max(cfg.decision_s, float(math.ceil(worst * 1.3)))


def _params_for(
    world: Substrate, src: int, dst: int, base: HostParams, cfg: LoopConfig
) -> HostParams:
    """Size one scope's population against the capacity it is competing for.

    Link capacities in the synthetic topology span 1 Gbps to 400 Gbps, so a
    population expressed in Mbps is either invisible on a core path or
    saturating on a customer one, and which of the two decides whether the
    closed loop closes at all.

    The scale that matters is the *spare* capacity on the best path the scope
    has, because that is what decides whether the scope's own decision can
    change its own cost. Below it, every model looks alike: the ranking of the
    paths is a property of the topology and no advisory perturbs it. At around
    it, piling onto the best path is what makes it stop being the best path,
    which is precisely the feedback the milestone is here to measure. Sizing
    against total capacity instead would leave a scope invisible behind
    whatever background traffic already fills the link.
    """
    params = replace(base, n_hosts=cfg.n_hosts)
    if cfg.target_load is None or cfg.n_hosts <= 0:
        return params
    paths = world.paths_for(src, dst, limit=8)
    if not paths:
        return params
    headroom = world.links.available_mbps()
    # The widest opening the scope has: the spare capacity it would enjoy if it
    # used its roomiest path alone. Sizing against the narrowest instead would
    # make every scope's load a function of its worst option.
    widest = max(float(headroom[list(p.ifaces[::2])].min()) for p in paths)
    return replace(params, mbps_per_host=max(widest, 0.0) * cfg.target_load / cfg.n_hosts)


def _one_scope(
    model: PathModel,
    session: Session,
    src: str,
    dst: str,
    cycle: int,
    cfg: LoopConfig,
    feed: Sequence[Observation],
) -> None:
    """One model turn on one scope. Everything it learns, it pays for."""
    session.begin_turn()
    found: ToolResult = session.call("query_paths", src=src, dst=dst, limit=cfg.limit)
    paths: list[PathRef] = session.known_paths(src, dst)
    if not paths:
        return

    observations: list[Observation] = list(feed)
    if found.ok and cfg.probes_per_cycle > 0:
        for offset in range(cfg.probes_per_cycle):
            target = paths[(cycle * cfg.probes_per_cycle + offset) % len(paths)]
            probe = session.call("probe_path", path_id=target.path_id, kind="latency")
            observations.extend(_observations(probe, target.path_id, session.now))

    model.observe(observations, session.view())
    advisory: Advisory = model.advise(session.view(), paths, cfg.sla, cfg.n_hosts)
    offered = {p.path_id for p in paths}
    weights = {k: float(v) for k, v in advisory.normalised().items() if k in offered}
    if weights:
        session.call(
            "publish_advisory",
            src=src,
            dst=dst,
            weights=weights,
            meta={"cycle": cycle, "reason": advisory.reason[:120]},
        )


def _drain_by_scope(session: Session, handle: str) -> dict[tuple[str, str], list[Observation]]:
    """Telemetry since the last round, filed under the scope it came from.

    Raw records in, contract observations out, one for one. No averaging and no
    windowing: a scope that produced forty samples hands the model forty
    samples, and deciding what to do with them is the model's job.
    """
    out: dict[tuple[str, str], list[Observation]] = {}
    for record in session.drain(handle):
        pid, src, dst = record.get("path_id"), record.get("src"), record.get("dst")
        if pid is None or src is None or dst is None:
            continue
        out.setdefault((str(src), str(dst)), []).append(
            Observation(
                t=record.t,
                path_id=str(pid),
                latency_ms=record.get("latency_ms"),
                loss=record.get("loss"),
                source="idint",
            )
        )
    return out


def _tracked_ifaces(world: Substrate, indices: Sequence[tuple[int, int]], n: int) -> list[int]:
    """The most *contested* interfaces, not the most crossed ones.

    An interface every path of a scope goes through -- the access link out of
    the source, typically -- carries the same load whatever the advisory says,
    so it is exactly where a control loop's effect is invisible. What matters is
    an interface some paths use and others do not, and the score that says so is
    ``p * (1 - p)`` over the fraction of a scope's paths that use it, summed
    over scopes. Ranking by raw crossings instead measures the access link and
    reports that nothing ever happens.
    """
    score: dict[int, float] = {}
    for a, b in indices:
        paths = world.paths_for(a, b, limit=50)
        if not paths:
            continue
        counter: dict[int, int] = {}
        for path in paths:
            for iface in dict.fromkeys(int(i) for i in path.ifaces[::2]):
                counter[iface] = counter.get(iface, 0) + 1
        for iface, count in counter.items():
            p = count / len(paths)
            score[iface] = score.get(iface, 0.0) + p * (1.0 - p)
    ranked = sorted(score, key=lambda i: (-score[i], i))
    return ranked[:n]


def _sample(
    result: LoopResult,
    world: Substrate,
    session: Session,
    tracked: Sequence[int],
    scopes: Sequence[tuple[str, str]],
    indices: Sequence[tuple[int, int]],
    published: int,
) -> None:
    utilisation = world.links.utilisation()
    capacity = world.links.usable_capacity_mbps()
    advised = world.links.demand_mbps
    for iface in tracked:
        result.utilisation[int(iface)].append(float(utilisation[iface]))
        result.advised_load[int(iface)].append(float(advised[iface] / capacity[iface]))

    deviations: list[float] = []
    costs: list[float] = []
    cost = world.links.cost()
    for scope, (a, b) in zip(scopes, indices, strict=True):
        state = world.hosts.scope(a, b)
        if state is None or state.n_paths == 0 or state.counts.sum() <= 0:
            result.path_share[scope].append(0.0)
            continue
        shares = state.counts / int(state.counts.sum())
        result.path_share[scope].append(float(shares[0]))
        deviations.append(state.deviation())
        if state.ifaces.size:
            per_path = np.bincount(state.owner, weights=cost[state.ifaces], minlength=state.n_paths)
            costs.append(float((per_path * shares).sum()))
    result.deviation.append(max(deviations, default=0.0))
    result.mean_cost_ms.append(float(np.mean(costs)) if costs else float("nan"))
    fresh = [a["latency_s"] for a in session.advisories[published:]]
    result.latency_s.append(float(np.mean(fresh)) if fresh else 0.0)


def compare(results: Sequence[LoopResult]) -> list[Mapping[str, Any]]:
    """Report cards side by side, worst swing first."""
    return [r.report() for r in sorted(results, key=lambda r: -r.swing())]
