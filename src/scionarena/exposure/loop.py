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

*The series is sampled on the world's clock, not on the model's turn.* Samples
land every ``sample_s`` simulated seconds whatever the model's rounds cost, taken
by a substrate tap rather than by this driver -- see ``instrument/sampler.py``
and ADR 0011. The default rate is one sample per decision round, because
oscillation is a property of a control loop relative to its own cadence, and the
fast band is defined in rounds and converted to the series' axis so that the two
stay comparable when they differ.

*The world keeps running underneath.* Between turns the substrate ticks at
``scenario.step_s``, hosts resample, and load moves. A model whose probes cost
it eight seconds does not get eight frozen seconds; it gets a turn that starts
later and an advisory that lands later -- and, now, eight seconds of samples of a
network nobody is advising, which is what the delay actually looks like.

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
    band_min,
    dominant_period,
    fast_swing,
    flap_rate,
    oscillation_index,
    warmup_samples,
)
from scionarena.instrument.metrics import Forecast, MetricInput, compute
from scionarena.instrument.sampler import Sampler, Series

__all__ = ["LoopConfig", "LoopResult", "run_loop", "busiest_scopes", "compare", "resolve_drive"]


@dataclass(frozen=True)
class LoopConfig:
    """How to run one episode of the closed loop.

    ``probes_per_cycle`` and ``limit`` are the model's information diet and they
    cost what M2 says they cost, so raising them raises the decision latency
    too. That coupling is deliberate: it is the mechanism behind "a slow model
    is measurably worse", and it is why the two knobs are here rather than
    buried in the driver.
    """

    #: Decision rounds. The length of the episode; no longer the length of the
    #: measured series, which is set by ``sample_s`` and by what the rounds cost.
    cycles: int = 240
    #: Simulated seconds a decision round is *given*. A round that costs more
    #: than this overruns, its advice lands late, and the next round starts late;
    #: the sample grid is unaffected either way (ADR 0011).
    decision_s: float = 30.0
    #: Simulated seconds between samples. ``None`` means one per decision round,
    #: which reproduces M3's axis exactly and is what every threshold in
    #: ``docs/milestones/`` was measured against. A multiple of
    #: ``scenario.step_s`` is required, or grid points fall between ticks.
    sample_s: float | None = None
    #: Widen ``decision_s`` until the rounds fit, measured over
    #: ``calibration_cycles`` throwaway rounds. **Off by default since M4.** It
    #: existed to protect the sample grid, the sampler now protects itself, and
    #: what remains of it is a harness that changes the scenario's cadence in
    #: response to how slow the model is -- which hides the finding instead of
    #: reporting it. Kept because it makes "the same model at a cadence it can
    #: keep" a controlled comparison rather than a rerun.
    adaptive_cadence: bool = False
    #: Unsampled rounds run before the episode to find out what a round costs
    #: here, when ``adaptive_cadence`` is on. Thrown away rather than measured;
    #: the world they leave behind is the world the episode starts from.
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
    #: The staleness axis. How long telemetry waits before the model may read
    #: it -- information delay, not decision delay. A model that is infinitely
    #: fast still decides about a world it last saw this long ago.
    telemetry_delay_s: float = 0.0
    #: Ask the model to ``predict`` each round and record what it said, so the
    #: accuracy family has something to score (ADR 0017). **Off by default**:
    #: the call happens inside the turn and is charged as decision latency, so
    #: turning it on changes what a round costs and therefore what the world
    #: does while the round runs. ``bench`` turns it on always, because §28
    #: makes accuracy mandatory; ``demo`` and ``conformance`` leave it off, and
    #: every M3 and M4 number was recorded with it off.
    record_forecasts: bool = False
    #: Horizons asked for, in simulated seconds. Zero is the nowcast. §28's
    #: forecast question is "does anything beat persistence at +60 s / +300 s",
    #: which is why those two and not a round number.
    horizons_s: tuple[float, ...] = (0.0, 60.0, 300.0)
    #: Track this many of the most contested interfaces.
    n_tracked: int = 24
    seed: int = 0
    #: Which way to drive the model (ADR 0019).
    #:
    #: ``auto`` honours the model's own ``uses_tools`` declaration, ``fixed``
    #: forces the observe/predict/advise cycle, and ``agentic`` hands the
    #: model the session and its deadline and leaves it alone. ``agentic`` on a
    #: model with no ``act`` raises rather than falling back: a silent fallback
    #: is the bug this parameter exists to fix, and reintroducing it as an error
    #: path would be worse than leaving it where it was.
    #:
    #: The point of ``fixed`` is that the probing policy in ``_one_scope`` is
    #: *ours* -- dumb on purpose and identical for every model -- so forcing a
    #: tool-using model through it compares architectures with information
    #: acquisition held equal. The difference between the two on one model
    #: is the value of choosing your own probes, which nothing else measures.
    drive: str = "auto"


@dataclass
class LoopResult:
    """What one episode did. Written to, never read by, the model."""

    model: str
    scenario: str
    config: LoopConfig
    #: How the model was actually driven: ``fixed`` or ``agentic``. The
    #: *resolved* value, never the requested one, so a result says which way it
    #: ran rather than which way it was asked to (ADR 0019).
    drive: str = "fixed"
    #: Simulated seconds per decision round as actually kept, which is
    #: ``config.decision_s`` unless ``adaptive_cadence`` widened it.
    cadence_s: float = 0.0
    #: Simulated seconds between samples, as the sampler actually kept them.
    sample_s: float = 0.0
    #: Everything the world did, on the world's own grid. Written by a substrate
    #: tap rather than by this driver, so its length is set by the episode's
    #: duration and not by what the model's rounds cost.
    series: Series = field(default_factory=lambda: Series(interval_s=1.0))
    #: What the model forecast, and when. Empty unless
    #: ``LoopConfig.record_forecasts`` was on; the accuracy metrics return
    #: ``None`` rather than zero when it is empty.
    forecasts: list[Forecast] = field(default_factory=list)
    #: Per round, not per sample: mean decision latency of the advisories
    #: published in it. A property of a decision, so it is recorded where
    #: decisions happen rather than on the sample grid.
    latency_s: list[float] = field(default_factory=list)
    #: Rounds whose turn overran ``decision_s``. Since M4 this no longer says
    #: anything about the sample grid -- see ``grid_uniform`` -- and instead says
    #: what it looks like it says: the model missed its slot this many times.
    overruns: int = 0
    wall_clock_s: float = 0.0
    session_summary: dict[str, Any] = field(default_factory=dict)
    hosts_summary: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------- the series, named

    @property
    def utilisation(self) -> dict[int, list[float]]:
        """Interface -> total utilisation, background included. What an operator sees."""
        return self.series.utilisation

    @property
    def advised_load(self) -> dict[int, list[float]]:
        """Interface -> load from the advised populations alone, over capacity.

        What the model caused rather than what happened, and therefore what the
        detectors read. ADR 0010: the background resamples on a bucket comparable
        to the decision cadence, so leaving it in the detector's series measures
        the environment's noise as if it were the model's.
        """
        return self.series.advised_load

    @property
    def path_share(self) -> dict[tuple[str, str], list[float]]:
        return self.series.path_share

    @property
    def deviation(self) -> list[float]:
        return self.series.deviation

    @property
    def mean_cost_ms(self) -> list[float]:
        return self.series.mean_cost_ms

    # ------------------------------------------------------------- detectors

    def _band(self) -> dict[str, Any]:
        """Warmup and band edge for this run's axis, in the series' own units.

        The detectors take samples and cycles-per-sample; the milestone's numbers
        are in rounds. One conversion, in one place, from the two rates this run
        actually kept -- so a run sampled four times per round is measured over
        the same span of *world* as one sampled once per round, rather than over
        a quarter of it.
        """
        sample_s = self.sample_s or self.config.decision_s
        cadence_s = self.cadence_s or self.config.decision_s
        return {
            "warmup": warmup_samples(sample_s, cadence_s),
            "f_min": band_min(sample_s, cadence_s),
        }

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
        band = self._band()
        return max((fast_swing(s, **band) for s in self.advised_load.values()), default=0.0)

    def share_swing(self) -> float:
        """The same amplitude on the realised split rather than on the link.

        Reported alongside because the two answer different questions. The link
        series says whether the *network* is being shaken; the share series says
        whether the *advice* is. A model can move its advice violently and shake
        nothing, if the paths it swaps between do not share a bottleneck --  and
        the reverse, since a scope may be shaken by the other ninety-nine.
        """
        band = self._band()
        return max((fast_swing(s, **band) for s in self.path_share.values()), default=0.0)

    def oscillation(self) -> float:
        """Spectral peak dominance on the worst link. Kept, but not the headline.

        This is the proposal's published measure and it is reported so that the
        finding recorded in ADR 0010 stays visible: at multi-scope scale it does
        not separate a herding model from a calm one, and has been observed to
        rank them backwards. Believe it at ``scopes == 1``; read :meth:`swing`
        otherwise.
        """
        band = self._band()
        return max((oscillation_index(s, **band) for s in self.advised_load.values()), default=0.0)

    def share_oscillation(self) -> float:
        band = self._band()
        return max((oscillation_index(s, **band) for s in self.path_share.values()), default=0.0)

    def swing_by_link(self) -> dict[int, float]:
        band = self._band()
        return {i: fast_swing(s, **band) for i, s in self.advised_load.items()}

    def period(self) -> float:
        """Samples per cycle of the worst link's peak. Divide by ``sample_s`` for seconds."""
        worst = self._worst_link()
        if worst is None:
            return float("inf")
        return dominant_period(self.advised_load[worst], **self._band())

    def flapping(self) -> float:
        worst = self._worst_link()
        if worst is None:
            return 0.0
        return flap_rate(self.advised_load[worst], warmup=self._band()["warmup"])

    def _worst_link(self) -> int | None:
        """The interface the headline number came from."""
        by_link = self.swing_by_link()
        return max(by_link, key=lambda k: by_link[k]) if by_link else None

    def grid_uniform(self) -> bool:
        """Were the samples taken on an even grid, and so is the spectrum real?

        Every detector here assumes uniform sampling and none of them can tell
        when that is untrue -- the series is the right length and the numbers
        look plausible either way. Carried on the report card next to the numbers
        it would invalidate.

        Since M4 this is a measurement rather than an inference. M3 answered it
        by counting rounds that overran, because a round that overran was a
        sample taken late; the sampler now takes its samples off the world's
        clock, so an overrun costs the model a slot and costs the grid nothing,
        and the two questions have come apart. Read ``overruns`` for the first.
        """
        return self.series.uniform()

    def mean_deviation(self) -> float:
        return float(np.mean(self.deviation)) if self.deviation else 0.0

    def cost(self) -> float:
        """Mean path cost over the measured part of the run. Lower is better."""
        tail = self.mean_cost_ms[len(self.mean_cost_ms) // 4 :]
        return float(np.mean(tail)) if tail else float("nan")

    def metric_input(self) -> MetricInput:
        """The bundle every registered metric reads. Plain data, no substrate."""
        return MetricInput(
            series=self.series,
            latency_s=list(self.latency_s),
            forecasts=list(self.forecasts),
            cadence_s=self.cadence_s or self.config.decision_s,
            sample_s=self.sample_s or self.cadence_s or self.config.decision_s,
            overruns=self.overruns,
            wall_clock_s=self.wall_clock_s,
            session=dict(self.session_summary),
            hosts=dict(self.hosts_summary),
        )

    def metrics(self, *, families: Sequence[str] = ()) -> dict[str, float | None]:
        """Every registered metric (ADR 0017). Not the report card: that is the
        fixed set M3 was scored on and it stays fixed, so a run recorded before
        the registry existed still renders."""
        return compute(self.metric_input(), families=families)

    def report(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "scenario": self.scenario,
            "drive": self.drive,
            "cycles": self.config.cycles,
            "decision_s": round(self.cadence_s, 1),
            "sample_s": round(self.sample_s, 1),
            "samples": len(self.series),
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
            "grid_uniform": self.grid_uniform(),
            "grid_jitter_s": round(self.series.jitter_s(), 6),
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
        telemetry_delay_s=cfg.telemetry_delay_s,
        label=getattr(model.capabilities, "name", "model"),
    )

    indices = [(world.topology.as_index(a), world.topology.as_index(b)) for a, b in scopes]
    base = host_params or scenario.hosts
    for a, b in indices:
        world.add_scope(a, b, params=_params_for(world, a, b, base, cfg))

    drive = resolve_drive(model, cfg.drive)
    result = LoopResult(model=session.label, scenario=scenario.name, config=cfg, drive=drive)
    tracked = _tracked_ifaces(world, indices, cfg.n_tracked)

    model.reset(session.view(), seed=cfg.seed)
    # One telemetry subscription for the whole episode. The records are raw and
    # are charged as they arrive, exactly as they would be for a model that
    # subscribed itself; the driver only forwards them to ``observe``.
    #
    # Not in agentic drive. There the model subscribes for itself if it wants a
    # stream, and a driver-held subscription it has no handle for would charge
    # it for records it cannot read.
    handle = ""
    if drive == "fixed":
        subscribed = session.call("subscribe", stream="telemetry")
        handle = str(subscribed.get("handle", "")) if subscribed.ok else ""

    started = time.perf_counter()
    cadence = _calibrate(model, session, scopes, indices, cfg, handle, drive)
    result.cadence_s = cadence
    result.sample_s = cfg.sample_s if cfg.sample_s is not None else cadence
    if result.sample_s > cadence + 1e-9:
        # Nyquist, and it is not theoretical: measured at the smoke tier, sampling
        # a 30 s cadence every 60 s dropped the greedy model's amplitude from 3.37
        # to 2.00 and put its peak dominance *below* the calm model's -- the same
        # inversion ADR 0010 originally reported and withdrew, reproduced on
        # purpose by undersampling. A model cannot change its advice faster than
        # once a round, so sampling faster than the cadence only ever adds detail;
        # sampling slower aliases the pathology into the slow band and hides it.
        raise ValueError(
            f"sample_s={result.sample_s}s is slower than the {cadence}s decision "
            "cadence, which aliases exactly what the detectors are looking for; "
            "sample at or faster than the cadence"
        )

    sampler = Sampler(
        world,
        interval_s=result.sample_s,
        tracked=tracked,
        scopes=list(scopes),
        indices=indices,
    )
    result.series = sampler.series
    # Attached after calibration, so the throwaway rounds are not in the series,
    # and the grid is anchored where the episode starts.
    with sampler:
        deadline = session.now + cadence
        for cycle in range(cfg.cycles):
            published = len(session.advisories)
            _turn(
                model,
                session,
                scopes,
                indices,
                cfg,
                cycle,
                handle,
                result.forecasts,
                drive=drive,
                deadline_s=deadline,
            )
            if session.now < deadline:
                session.advance(deadline - session.now)
            else:
                result.overruns += 1
            # The samples were taken by the tap while the above ran. All that is
            # left per round is the latency of the advice this round published,
            # which is a property of the decision and not of the world.
            fresh = [a["latency_s"] for a in session.advisories[published:]]
            result.latency_s.append(float(np.mean(fresh)) if fresh else 0.0)
            deadline += cadence
            if on_cycle is not None:
                on_cycle(cycle + 1, cfg.cycles)
    result.wall_clock_s = time.perf_counter() - started
    result.session_summary = session.summary()
    result.hosts_summary = world.hosts.summary()
    return result


def resolve_drive(model: PathModel, requested: str = "auto") -> str:
    """Which way this model will actually be driven. See ADR 0019.

    ``auto`` believes the model's declaration, which is what an unchanged caller
    gets. ``fixed`` always succeeds -- every model implements the four
    ``PathModel`` methods, including a tool-using one, because
    :class:`~scionarena.exposure.contracts.ToolUsingModel` inherits from
    ``PathModel``. ``agentic`` is the only one that can refuse, and it refuses
    loudly: a fallback to the fixed cycle here would score a model on a code path
    its author never intended anyone to time, silently, which is exactly what
    ``run_loop`` did before this function existed.
    """
    can_act = callable(getattr(model, "act", None))
    if requested == "auto":
        declared = bool(getattr(model.capabilities, "uses_tools", False))
        return "agentic" if (declared and can_act) else "fixed"
    if requested == "fixed":
        return "fixed"
    if requested == "agentic":
        if not can_act:
            name = getattr(model.capabilities, "name", type(model).__name__)
            raise ValueError(
                f"drive='agentic' was asked for but {name!r} has no act(); running it "
                "through the fixed cycle instead would report a decision latency the "
                "model never intended, so this is a refusal rather than a fallback"
            )
        return "agentic"
    raise ValueError(f"drive must be 'auto', 'fixed' or 'agentic', not {requested!r}")


def _turn(
    model: PathModel,
    session: Session,
    scopes: Sequence[tuple[str, str]],
    indices: Sequence[tuple[int, int]],
    cfg: LoopConfig,
    cycle: int,
    handle: str,
    forecasts: list[Forecast] | None = None,
    *,
    drive: str = "fixed",
    deadline_s: float | None = None,
) -> None:
    """One decision round: drain what arrived, then let the model act per scope."""
    if drive == "agentic":
        _agentic_turn(model, session, scopes, cfg, deadline_s, forecasts)
        return
    feed = _drain_by_scope(session, handle)
    for (src, dst), _ in zip(scopes, indices, strict=True):
        _one_scope(model, session, src, dst, cycle, cfg, feed.get((src, dst), []), forecasts)


def _agentic_turn(
    model: PathModel,
    session: Session,
    scopes: Sequence[tuple[str, str]],
    cfg: LoopConfig,
    deadline_s: float | None,
    forecasts: list[Forecast] | None,
) -> None:
    """One decision round the model runs itself.

    The driver opens the turn, sets the deadline and gets out of the way. It
    does not query, does not probe, does not call ``observe`` and does not call
    ``advise``: the model publishes through ``publish_advisory`` like any other
    caller, so the latency accounting in ``run_loop`` needs no special case.

    Forecasts are still recorded, after ``act`` and inside the same turn, over
    whatever paths the model chose to learn about. A model that never queried a
    scope has no paths there and records nothing for it -- a finding about the
    model rather than a gap in the ledger.
    """
    act = model.act  # type: ignore[attr-defined]  # resolved by ``resolve_drive``
    session.begin_turn()
    session.deadline_s = deadline_s
    try:
        slot = cfg.decision_s if deadline_s is None else max(0.0, deadline_s - session.now)
        act(session, slot)
    finally:
        session.deadline_s = None
    if not cfg.record_forecasts or forecasts is None:
        return
    for src, dst in scopes:
        paths = session.known_paths(src, dst)
        if paths:
            _forecast(model, session, src, dst, paths, cfg, forecasts)


def _calibrate(
    model: PathModel,
    session: Session,
    scopes: Sequence[tuple[str, str]],
    indices: Sequence[tuple[int, int]],
    cfg: LoopConfig,
    handle: str,
    drive: str = "fixed",
) -> float:
    """Find a cadence the episode can actually keep, by running a few rounds.

    Off by default since M4. It was introduced to protect the sample grid at the
    dev tier, where every one of 240 rounds overran a 30 s cadence because probes
    are charged per scope -- and it only half worked, because a model whose rounds
    get more expensive as the episode runs outgrows a cadence fixed from its
    first three. The grid is now the sampler's problem (ADR 0011) and this is
    back to being what it reads like: a way to ask what the same model does at a
    cadence it can keep, as a controlled change rather than a rerun.

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
        _turn(
            model,
            session,
            scopes,
            indices,
            cfg,
            cycle,
            handle,
            drive=drive,
            deadline_s=mark + cfg.decision_s,
        )
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
    out: list[Forecast] | None = None,
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
    if cfg.record_forecasts:
        # Inside the turn, so the time it takes is charged as decision latency
        # exactly as ``advise`` is. A model that ships intervals pays for
        # shipping them (ADR 0017).
        _forecast(model, session, src, dst, paths, cfg, out if out is not None else [])
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


def _forecast(
    model: PathModel,
    session: Session,
    src: str,
    dst: str,
    paths: Sequence[PathRef],
    cfg: LoopConfig,
    out: list[Forecast],
) -> None:
    """Record what the model says will happen, at every horizon it is asked for.

    A model that raises here is a model that cannot forecast, and that is a
    finding rather than a crash: the ledger simply gains nothing for this round
    and the accuracy metrics report on what there is.
    """
    view = session.view()
    for horizon in cfg.horizons_s:
        try:
            predicted = model.predict(view, list(paths), horizon_s=horizon)
        except Exception:  # noqa: BLE001 -- a model that cannot forecast is a result
            return
        now = session.now
        for path_id, prediction in predicted.items():
            dist = prediction.latency_ms
            out.append(
                Forecast(
                    t=now,
                    src=src,
                    dst=dst,
                    path_id=str(path_id),
                    horizon_s=float(horizon),
                    point=float(dist.point),
                    quantiles={float(k): float(v) for k, v in (dist.quantiles or {}).items()},
                )
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


def compare(results: Sequence[LoopResult]) -> list[Mapping[str, Any]]:
    """Report cards side by side, worst swing first."""
    return [r.report() for r in sorted(results, key=lambda r: -r.swing())]
