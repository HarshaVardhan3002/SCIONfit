"""What a decision costs to make (ADR 0021).

Names the bug: invariant 3 says a decision taking 800 ms is applied 800 ms late,
and it was implemented only for time spent inside tools. Simulated time advanced
from exactly two places -- a tool's modelled cost, and an explicit ``advance`` --
so a model that spent ten seconds in ``predict`` and called nothing had a
decision latency of zero, and its advice landed as though it had answered
instantly. ``Session`` took ``charge_real_time`` and ``stopwatch`` for precisely
this, assigned both, and read neither.

That is the wrong measurement for the comparison this harness exists to make,
and it is wrong in the direction that flatters a model which thinks slowly.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from scionarena.core.clock import Stopwatch
from scionarena.core.scenario import Scenario, TopologySpec
from scionarena.exposure.budget import Budget
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
from scionarena.exposure.loop import LoopConfig, busiest_scopes, run_loop
from scionarena.exposure.session import Session
from scionarena.reference.models import ReferenceStochastic


def scenario(seed: int = 7) -> Scenario:
    return Scenario(name="t", seed=seed, topology=TopologySpec(tier="smoke"))


class Slow(ReferenceStochastic):
    """Deliberates for a fixed real interval and calls no tool while doing it."""

    def __init__(self, sleep_s: float = 0.02) -> None:
        super().__init__()
        self.sleep_s = sleep_s
        self.capabilities = Capabilities(name="Slow", architecture="toy", emits_assignment=True)

    def advise(
        self, topo: TopologySnapshot, paths: Sequence[PathRef], sla: SLA, n_hosts: int = 1
    ) -> Advisory:
        time.sleep(self.sleep_s)
        return super().advise(topo, paths, sla, n_hosts)


def _run(think: str, think_s: float = 0.0, model: Any = None, cycles: int = 8) -> Any:
    sc = scenario()
    scopes = busiest_scopes(sc.build(), 2)
    return run_loop(
        model if model is not None else ReferenceStochastic(),
        sc,
        scopes,
        config=LoopConfig(cycles=cycles, decision_s=30.0, n_hosts=40, think=think, think_s=think_s),
        world=sc.build(),
    )


# --------------------------------------------------------------------------
# the bug


def test_thinking_time_reaches_the_clock_at_all() -> None:
    """The whole finding in one assertion. Before ADR 0021 the two runs below
    were identical, because nothing the model spent in its own code could move
    the world."""
    free = _run("free")
    fixed = _run("fixed", 2.0)
    assert fixed.report()["mean_latency_s"] > free.report()["mean_latency_s"] * 3
    assert fixed.report()["digest"] != free.report()["digest"], (
        "if the digests match, the world did not move while the model was thinking"
    )


def test_a_slow_model_is_charged_and_a_fast_one_is_not() -> None:
    """The point of ``measured``: what is charged is what the model cost, so a
    model that deliberates pays and one that returns immediately does not.

    The margin is *one* sleep rather than two even though the run has two
    scopes, because each scope opens its own turn and an advisory's latency is
    measured from the turn it was decided in. That is the correct accounting --
    a scope does not wait for the other scope's deliberation -- and asserting
    two sleeps here was this test's own first bug.
    """
    fast = _run("measured", model=ReferenceStochastic(), cycles=6)
    slow = _run("measured", model=Slow(0.05), cycles=6)
    assert slow.report()["mean_latency_s"] > fast.report()["mean_latency_s"] + 0.03


def test_thinking_is_free_by_default() -> None:
    """Every number recorded before this ADR was measured with thinking free, so
    the default has to stay there or they all silently change meaning."""
    assert LoopConfig().think == "free"
    quiet = _run("free", model=Slow(0.03), cycles=4)
    busy = _run("free", model=ReferenceStochastic(), cycles=4)
    assert quiet.report()["digest"] == busy.report()["digest"], (
        "under 'free' the world must not notice how long the model took"
    )


# --------------------------------------------------------------------------
# the trade-off the policy exists to expose


def test_fixed_reproduces_and_measured_does_not() -> None:
    """The irreducible tension, made explicit rather than resolved by accident.
    Invariant 4 wants machine-independence; invariant 3 wants real cost. A run
    cannot have both, so the run says which one it took."""
    assert _run("fixed", 2.0).report()["digest"] == _run("fixed", 2.0).report()["digest"]
    assert _run("free").report()["digest"] == _run("free").report()["digest"]


def test_an_unknown_policy_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="'free', 'fixed' or 'measured'"):
        _run("charged")


def test_the_policy_is_recorded_on_the_result() -> None:
    """A ``measured`` cell and a ``free`` cell are different experiments. A
    result that does not say which cannot be compared with anything."""
    assert _run("fixed", 1.0).report()["think"] == "fixed"
    assert _run("free").report()["think"] == "free"


# --------------------------------------------------------------------------
# what is measured is the model, not us


def test_time_spent_inside_a_tool_is_not_charged_as_thinking() -> None:
    """Otherwise an agent is billed for our own handlers, and a slow handler
    reads as a slow model -- which would make the agentic half of a parity pair
    look expensive for reasons that have nothing to do with the agent."""
    world = scenario().build()
    session = Session(world, budget=Budget.unlimited(), seed=1, charge_real_time=True)
    src, dst = busiest_scopes(world, 1)[0]

    before = session.now
    with session.thinking("act"):
        session.call("query_paths", src=src, dst=dst, limit=8)
    charged = session.now - before

    # The query's own modelled cost is charged as a tool cost; the real seconds
    # spent inside the handler must not be charged a second time as thinking.
    calls = [r for r in session.log if r.kind == "call"]
    thinks = [r for r in session.log if r.kind == "think"]
    assert calls and thinks
    assert thinks[0].cost.wall_clock_s < 0.01, thinks[0].cost.wall_clock_s
    assert charged > 0.0


def test_a_thinking_window_is_logged_so_the_trace_explains_the_clock() -> None:
    """A world that jumps forward with nothing in the log to say why is a trace
    that does not replay to the run it describes."""
    world = scenario().build()
    session = Session(
        world,
        budget=Budget.unlimited(),
        seed=1,
        charge_real_time=True,
        stopwatch=Stopwatch(fixed_s=1.5),
    )
    with session.thinking("advise"):
        pass
    thinks = [r for r in session.log if r.kind == "think"]
    assert len(thinks) == 1
    assert thinks[0].cost.wall_clock_s == pytest.approx(1.5)
    assert session.now == pytest.approx(1.5)


def test_the_default_session_charges_nothing_and_measures_nothing() -> None:
    world = scenario().build()
    session = Session(world, budget=Budget.unlimited(), seed=1)
    before = session.now
    with session.thinking():
        time.sleep(0.01)
    assert session.now == before
    assert not [r for r in session.log if r.kind == "think"]


# --------------------------------------------------------------------------
# through the sweep


def test_the_suite_digest_separates_the_charging_policies() -> None:
    """A resumed sweep must not reuse cells charged the other way."""
    from scionarena.bench import SweepSpec

    base = dict(
        name="s", models=("ema",), include_baselines=False, tier="smoke", cycles=6, repeats=1
    )
    assert SweepSpec(think="free", **base).digest() != SweepSpec(think="measured", **base).digest()


def test_a_cell_records_how_its_thinking_was_charged() -> None:
    from scionarena.bench import SweepSpec, plan, run_cell

    spec = SweepSpec(
        name="s",
        models=("ema",),
        include_baselines=False,
        tier="smoke",
        cycles=6,
        decision_s=10.0,
        scopes=3,
        repeats=1,
        think="fixed",
        think_s=0.5,
    )
    result = run_cell(spec, plan(spec)[0])
    assert result.error is None, result.error
    assert result.think == "fixed"


def test_the_report_says_when_thinking_was_free() -> None:
    """Silence would let a reader take a decision-latency column at face value
    for a model that was never charged for deliberating."""
    from scionarena.bench.report import _limits, _styles, gather
    from scionarena.bench.results import CellResult

    def cell(think: str) -> CellResult:
        return CellResult(
            cell_id=f"c-{think}",
            suite="t",
            suite_digest="d",
            model="pkg:M",
            label="M",
            mandatory=False,
            axes={
                "population": "1k",
                "defectors": "none",
                "discipline": "none",
                "paths": "all",
                "staleness": "fresh",
                "probes": "audited",
            },
            repeat=0,
            seed=1,
            scenario="t/x",
            tier="smoke",
            drive="fixed",
            think=think,
            metrics={"swing": 1.0},
        )

    story: list[Any] = []
    _limits(story, _styles(), gather([cell("free")]))
    assert "Thinking was free here" in _text(story)

    story = []
    _limits(story, _styles(), gather([cell("measured")]))
    assert "this machine's" in _text(story)

    story = []
    _limits(story, _styles(), gather([cell("free"), cell("measured")]))
    assert "not all charged the same way" in _text(story)


def _text(story: list[Any]) -> str:
    out = []
    for item in story:
        getter = getattr(item, "getPlainText", None)
        if getter is not None:
            out.append(str(getter()))
        cells = getattr(item, "_cellvalues", None)
        if cells is not None:
            out.extend(str(c) for row in cells for c in row)
    return "\n".join(out)


# --------------------------------------------------------------------------
# a model that pays for what it emits


def test_a_distributional_model_pays_for_its_intervals_in_simulated_time() -> None:
    """ADR 0017 charged the forecast call as decision latency; before ADR 0021
    that charge was zero for anything that did not call a tool. A model whose
    quantiles are expensive to compute now pays for them where it matters."""

    class Costly(ReferenceStochastic):
        def predict(
            self,
            topo: TopologySnapshot,
            paths: Sequence[PathRef],
            horizon_s: float = 0.0,
            demand: Demand | None = None,
        ) -> Mapping[str, Prediction]:
            time.sleep(0.01)
            return {p.path_id: Prediction(Dist.point_estimate(10.0), Dist(), Dist()) for p in paths}

        def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
            return None

    sc = scenario()
    scopes = busiest_scopes(sc.build(), 1)
    cheap = run_loop(
        ReferenceStochastic(),
        sc,
        scopes,
        config=LoopConfig(
            cycles=4, decision_s=30.0, n_hosts=40, think="measured", record_forecasts=True
        ),
        world=sc.build(),
    )
    dear = run_loop(
        Costly(),
        sc,
        scopes,
        config=LoopConfig(
            cycles=4, decision_s=30.0, n_hosts=40, think="measured", record_forecasts=True
        ),
        world=sc.build(),
    )
    assert dear.report()["mean_latency_s"] > cheap.report()["mean_latency_s"] + 0.02
