"""The gradient-boosted reference model, and what it costs to be one.

The fifth architecture and the first shipped model whose compute is worth
charging for. Most of what is checked here is that it earns the declarations it
makes -- the structural features that let it score a path it has never seen, the
sorted quantiles without which every coverage figure is meaningless, and the
bounded memory that keeps it an *online* model rather than one that has seen the
whole of its own episode.
"""

from __future__ import annotations

from typing import Any

import pytest

from scionarena.core.scenario import Scenario, TopologySpec
from scionarena.exposure.contracts import Observation, PathRef, TopologySnapshot
from scionarena.exposure.loop import LoopConfig, busiest_scopes, run_loop
from scionarena.exposure.precheck import synthetic_topology

pytest.importorskip("sklearn", reason="the gradient-boosted model needs the [trees] extra")

from scionarena.reference.trees import (  # noqa: E402
    MIN_ROWS,
    QUANTILES,
    GradientBoosted,
)


def _feed(model: GradientBoosted, topo: TopologySnapshot, rounds: int, step: float = 5.0) -> None:
    """Enough paired observations that the model has something to fit."""
    for i in range(rounds):
        t = float(i * step)
        moving = TopologySnapshot(t=t, interfaces=topo.interfaces, paths=topo.paths)
        model.observe(
            [
                Observation(
                    t=t,
                    path_id=p.path_id,
                    latency_ms=10.0 * (n + 1) + (i % 7),
                    throughput_mbps=100.0,
                    loss=0.001,
                    source="scmp",
                )
                for n, p in enumerate(topo.paths)
            ],
            moving,
        )


# --------------------------------------------------------------------------
# it earns what it declares


def test_it_scores_a_path_over_an_interface_it_has_never_seen() -> None:
    """Names the failure probe R4 exists for. Every feature is computed from the
    snapshot handed over, so a path built from interfaces that appeared a second
    ago produces a row like any other. A model keyed on path identity instead
    would silently lose its history whenever a segment is re-signed."""
    model = GradientBoosted(refit_every=1)
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=40)

    wider = synthetic_topology(extra=True)
    novel = [p for p in wider.paths if p.path_id == "p4"]
    out = model.predict(wider, novel, horizon_s=60.0)
    assert set(out) == {"p4"}
    assert out["p4"].latency_ms.point > 0.0


def test_a_path_recombined_from_seen_hops_is_scored() -> None:
    model = GradientBoosted(refit_every=1)
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=40)
    recombined = [PathRef("pX", "1-ff00:0:1", "1-ff00:0:9", ("if2", "if5"))]
    assert model.predict(topo, recombined)["pX"].latency_ms.point >= 0.0


def test_the_quantiles_never_cross() -> None:
    """Names the bug: the three quantile regressors are fitted independently and
    nothing makes them monotone, so the 0.9 fit can land below the 0.1 fit on
    some rows. A crossed interval fails R5 and makes every coverage figure in
    the accuracy family meaningless while still rendering as a number."""
    model = GradientBoosted(refit_every=1)
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=60)
    for prediction in model.predict(topo, list(topo.paths), horizon_s=300.0).values():
        assert prediction.latency_ms.quantiles_monotone()
        assert prediction.latency_ms.is_distributional


def test_a_point_estimator_variant_says_so_and_ships_no_quantiles() -> None:
    """Two variants of one architecture, which is the comparison the report
    groups for: the same trees with and without the interval."""
    model = GradientBoosted(distributional=False, refit_every=1)
    assert model.capabilities.distributional is False
    assert model.capabilities.architecture == "gbdt"
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=60)
    sample = next(iter(model.predict(topo, list(topo.paths)).values()))
    assert not sample.latency_ms.is_distributional


def test_before_the_first_fit_it_advises_rather_than_refusing() -> None:
    """A model with nothing learned still has to advise. What it must not do is
    claim certainty, so the prior is bracketed rather than a bare point."""
    model = GradientBoosted()
    topo = synthetic_topology()
    model.reset(topo)
    out = model.predict(topo, list(topo.paths))
    assert len(out) == len(topo.paths)
    sample = next(iter(out.values()))
    assert sample.latency_ms.spread > 0.0, (
        "a zero-width prior scores coverage 0 and reads as over-confidence"
    )
    assert sample.confidence is not None and sample.confidence < 0.5


def test_the_advisory_spreads_rather_than_ranking() -> None:
    """It declares ``emits_assignment``. A softmax at an absolute temperature
    against a cost spread of hundreds returns a numerically one-hot vector,
    which is a ranking however it was computed."""
    model = GradientBoosted(refit_every=1)
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=60)
    advisory = model.advise(
        topo,
        list(topo.paths),
        sla=__import__("scionarena.exposure.contracts", fromlist=["SLA"]).SLA(),
    )
    assert advisory.max_weight < 0.99
    assert set(advisory.weights) == {p.path_id for p in topo.paths}


# --------------------------------------------------------------------------
# it stays an online model


def test_the_training_set_is_bounded() -> None:
    """An hour at the realistic tier would otherwise grow an unbounded set, and
    trees fitted on the whole run are no longer online -- they have seen the
    future of their own episode."""
    model = GradientBoosted(memory=MIN_ROWS, refit_every=10_000)
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=400)
    assert len(model._rows) == MIN_ROWS


def test_reset_clears_the_fit_not_only_the_rows() -> None:
    """A sweep reuses one object over many worlds. A fit that survives a reset
    is one episode's model scoring the next episode's network, which looks like
    a model that generalises and is a leak."""
    model = GradientBoosted(refit_every=1)
    topo = synthetic_topology()
    model.reset(topo)
    _feed(model, topo, rounds=60)
    assert model._fitted, "should have fitted by now"
    model.reset(topo)
    assert not model._fitted
    assert not model._rows


def test_an_unpaired_observation_makes_no_training_row() -> None:
    """The target is what happened *after* what was known. A single reading is
    not a supervised example, and counting it as one would train the model to
    predict the number it was just handed."""
    model = GradientBoosted()
    topo = synthetic_topology()
    model.reset(topo)
    model.observe(
        [Observation(t=1.0, path_id=p.path_id, latency_ms=12.0) for p in topo.paths], topo
    )
    assert len(model._rows) == 0


def test_a_latency_of_none_is_not_a_latency_of_zero() -> None:
    """Probe R3. ``None`` means not measured, and training on it as zero teaches
    the model that unobserved paths are instant."""
    model = GradientBoosted()
    topo = synthetic_topology()
    model.reset(topo)
    for t in (1.0, 2.0):
        model.observe(
            [Observation(t=t, path_id=p.path_id, latency_ms=None, loss=0.02) for p in topo.paths],
            topo,
        )
    assert len(model._rows) == 0


# --------------------------------------------------------------------------
# what it costs


def test_the_refit_is_charged_to_the_clock() -> None:
    """The ADR 0021 payoff, and the reason this model was worth adding: it is the
    first shipped model whose compute is not free.

    Asserted on the session's own ledger rather than by comparing two runs. The
    comparison was this test's first version and it is confounded: under
    ``measured`` the world advances *during* thinking, so the two runs face
    different networks and their mean latencies differ for reasons that have
    nothing to do with the fit.
    """
    from scionarena.core.scenario import Scenario as _Scenario
    from scionarena.exposure.budget import Budget
    from scionarena.exposure.session import Session

    world = _Scenario(name="t", seed=7, topology=TopologySpec(tier="smoke")).build()
    topo = synthetic_topology()
    model = GradientBoosted(refit_every=1, max_iter=40)
    model.reset(topo)
    _feed(model, topo, rounds=30)  # enough rows that the next observe refits

    session = Session(world, budget=Budget.unlimited(), seed=1, charge_real_time=True)
    before = session.now
    with session.thinking("observe"):
        _feed(model, topo, rounds=4)
    charged = session.now - before

    thinks = [r for r in session.log if r.kind == "think"]
    assert thinks, "the fit produced no entry in the ledger"
    assert charged > 0.0, "fitting trees cost the world nothing"


def test_fitting_costs_more_than_predicting() -> None:
    """Why the refit cadence is a variant knob rather than a constant: it trades
    the freshness of the ensemble against the length of the decision slot, and
    that trade is the whole operational story of this architecture."""
    import time

    topo = synthetic_topology()
    model = GradientBoosted(refit_every=10_000, max_iter=40)
    model.reset(topo)
    _feed(model, topo, rounds=60)

    mark = time.perf_counter()
    model._fit()
    fit_s = time.perf_counter() - mark

    mark = time.perf_counter()
    model.predict(topo, list(topo.paths), horizon_s=60.0)
    predict_s = time.perf_counter() - mark

    assert fit_s > predict_s, (fit_s, predict_s)


def test_it_closes_the_loop_and_produces_something_to_score() -> None:
    """End to end through the real driver. A reference model that only works on
    a hand-built topology is a fixture, not a baseline."""
    sc = Scenario(name="t", seed=7, topology=TopologySpec(tier="smoke"))
    scopes = busiest_scopes(sc.build(), 2)
    result = run_loop(
        GradientBoosted(refit_every=6, max_iter=30),
        sc,
        scopes,
        config=LoopConfig(cycles=24, decision_s=30.0, n_hosts=40, record_forecasts=True),
        world=sc.build(),
    )
    assert result.session_summary["advisories"] > 0
    assert result.forecasts, "nothing was recorded for the accuracy family to score"
    assert result.report()["mean_cost_ms"] > 0.0


def test_a_missing_extra_is_named_at_construction_not_mid_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cell that dies an hour into a sweep on an ImportError blames the model
    for a packaging problem."""
    import builtins

    from scionarena.reference.trees import MissingTrees

    real = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sklearn":
            raise ImportError("no sklearn")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(MissingTrees, match=r"scionarena\[trees\]"):
        GradientBoosted()


def test_it_loads_from_a_bare_name_with_its_variant_argument() -> None:
    from scionarena.exposure.loading import load_model

    model = load_model("gbdt", args={"distributional": False, "refit_every": 4})
    assert model.capabilities.architecture == "gbdt"
    assert model.capabilities.distributional is False


def test_the_quantile_set_is_what_the_intervals_are_scored_against() -> None:
    """0.1 and 0.9 bracket 80%, which is the nominal coverage the accuracy family
    scores against. If these two drift apart the coverage number silently starts
    measuring a different claim."""
    from scionarena.reference.trees import NOMINAL

    assert QUANTILES[-1] - QUANTILES[0] == pytest.approx(NOMINAL)
