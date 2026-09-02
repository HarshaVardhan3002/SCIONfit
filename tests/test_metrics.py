"""Phase 3: the metric registry (ADR 0017).

Two things are under test. The registry's *contract* -- a new metric registers
without editing a runner, a metric that cannot be measured is ``None`` and never
zero, and an accuracy metric cannot return a mean over its strata -- and the
metrics themselves, each against a case constructed so that the right answer is
known independently of the implementation.

The one test here that is not about either is
``test_the_truth_series_is_keyed_the_way_a_model_names_a_path``. It guards a
duplicated rendering, and it is the test that would have caught the bug that
made every accuracy metric silently report nothing.
"""

from __future__ import annotations

import math

import pytest

from scionarena.instrument import (
    REGISTRY,
    Forecast,
    MetricInput,
    Series,
    compute,
    metric,
    path_name,
)
from scionarena.instrument.metrics import FAMILIES, NOMINAL

# --------------------------------------------------------------------------
# fixtures: a series whose right answers are known by construction


def a_series(costs: list[float], best: list[float] | None = None) -> Series:
    n = len(costs)
    return Series(
        interval_s=1.0,
        times=[float(i) for i in range(n)],
        advised_load={0: list(costs)},
        path_share={("a", "b"): list(costs)},
        deviation=[0.0] * n,
        mean_cost_ms=list(costs),
        best_cost_ms=list(best if best is not None else [0.0] * n),
    )


def an_input(**changes) -> MetricInput:
    base = {
        "series": a_series([10.0] * 200, best=[5.0] * 200),
        "latency_s": [1.0] * 200,
        "cadence_s": 1.0,
        "sample_s": 1.0,
        "session": {"calls": 400, "by_tool": {"probe": {"ok": 3, "failed": 2, "late": 1}}},
    }
    return MetricInput(**{**base, **changes})


def forecasts_at(truth: float, point: float, spread: float, n: int = 60) -> MetricInput:
    """A run where one path costs ``truth`` throughout and the model says
    ``point`` with an interval of ``+-spread``."""
    series = a_series([truth] * n, best=[truth] * n)
    series.path_cost[("a", "b", "p1")] = [truth] * n
    return an_input(
        series=series,
        forecasts=[
            Forecast(
                t=float(i),
                src="a",
                dst="b",
                path_id="p1",
                horizon_s=0.0,
                point=point,
                quantiles={0.1: point - spread, 0.5: point, 0.9: point + spread},
            )
            for i in range(n - 1)
        ],
    )


# --------------------------------------------------------------------------
# the registry's contract


def test_a_metric_registers_without_the_runner_knowing_its_name() -> None:
    """M4's acceptance criterion, and what lets the registry grow through Phases
    5 and 6 without the churn that welded the last four metrics into
    ``LoopResult``."""

    @metric("a_brand_new_thing", "operational")
    def _new(data: MetricInput) -> float:
        """Doc line."""
        return 42.0

    try:
        assert compute(an_input())["a_brand_new_thing"] == 42.0
        assert REGISTRY["a_brand_new_thing"].doc == "Doc line."
    finally:
        del REGISTRY["a_brand_new_thing"]


def test_a_metric_in_no_family_is_refused_rather_than_filed_under_other() -> None:
    with pytest.raises(ValueError, match="accuracy"):

        @metric("nowhere", "vibes")
        def _bad(data: MetricInput) -> float:
            return 0.0


def test_a_name_cannot_be_registered_twice() -> None:
    with pytest.raises(ValueError, match="already registered"):

        @metric("swing", "stability")
        def _clash(data: MetricInput) -> float:
            return 0.0


def test_every_registered_metric_declares_a_family_the_report_knows() -> None:
    assert {m.family for m in REGISTRY.values()} <= set(FAMILIES)
    assert all(m.doc for m in REGISTRY.values()), "a metric with no doc renders as a bare name"


def test_a_metric_that_cannot_be_measured_is_none_and_never_zero() -> None:
    """A report cannot tell a model that scored zero from a run that never
    measured it, so the distinction is kept rather than flattened."""
    scored = compute(an_input(forecasts=[]))

    assert scored["pinball"] is None
    assert scored["coverage"] is None
    assert "pinball.h0" not in scored


def test_one_broken_metric_does_not_lose_the_others() -> None:
    @metric("always_raises", "operational")
    def _boom(data: MetricInput) -> float:
        """Explodes."""
        raise RuntimeError("no")

    try:
        scored = compute(an_input())
        assert scored["always_raises"] is None
        assert scored["swing"] is not None
    finally:
        del REGISTRY["always_raises"]


def test_an_accuracy_metric_cannot_report_a_mean_over_its_strata() -> None:
    """ "Stratified, never as a single number" is enforced by the shape rather
    than requested: a mapping is flattened per key and there is nowhere for an
    average over horizons to go."""
    data = forecasts_at(truth=10.0, point=10.0, spread=1.0)
    data.forecasts = [
        *data.forecasts,
        *[
            Forecast(
                t=f.t,
                src=f.src,
                dst=f.dst,
                path_id=f.path_id,
                horizon_s=5.0,
                point=f.point,
                quantiles=f.quantiles,
            )
            for f in data.forecasts
        ],
    ]
    scored = compute(data, families=("accuracy",))

    assert "pinball.h0" in scored and "pinball.h5" in scored
    assert "pinball" not in scored


def test_families_can_be_asked_for_on_their_own() -> None:
    scored = compute(an_input(), families=("stability",))

    assert set(scored) == {name for name, m in REGISTRY.items() if m.family == "stability"}


# --------------------------------------------------------------------------
# accuracy


def test_a_forecast_that_is_exactly_right_loses_nothing_at_the_median() -> None:
    scored = compute(forecasts_at(truth=10.0, point=10.0, spread=2.0), families=("accuracy",))

    # 0.1*(10-8) + 0 + 0.1*(12-10) = 0.4, over three quantiles. The mean, not
    # the sum: a model asked for five quantiles must not score worse than one
    # asked for three purely for having answered more fully.
    assert scored["pinball.h0"] == pytest.approx(0.4 / 3, abs=0.01)
    assert scored["coverage.h0"] == 1.0


def test_a_confident_and_wrong_model_is_caught_by_coverage_and_not_by_the_point() -> None:
    """The failure the accuracy family exists for. Two models with identical
    point error, one honest about it and one not."""
    honest = compute(forecasts_at(truth=10.0, point=14.0, spread=8.0), families=("accuracy",))
    sure = compute(forecasts_at(truth=10.0, point=14.0, spread=0.5), families=("accuracy",))

    assert honest["coverage.h0"] == 1.0
    assert sure["coverage.h0"] == 0.0
    assert sure["coverage_gap.h0"] == pytest.approx(-NOMINAL)
    assert sure["interval_width.h0"] < honest["interval_width.h0"]


def test_an_uninformative_interval_is_not_rewarded_for_covering_everything() -> None:
    """Coverage alone would call a model that predicts (0, 10^6) perfect, which
    is why ``interval_width`` is registered beside it rather than as an extra."""
    wide = compute(forecasts_at(truth=10.0, point=10.0, spread=1e6), families=("accuracy",))

    assert wide["coverage.h0"] == 1.0
    assert wide["interval_width.h0"] > 1e5
    assert wide["crps.h0"] > 1e4, "CRPS charges for the width that coverage forgives"


def test_conformal_alpha_stays_put_when_the_intervals_are_already_calibrated() -> None:
    """§19.5's rule: a_{t+1} = a_t + eta (target - hit). Drift is what shows a
    model holding nominal coverage by being corrected the whole way through."""
    calm = compute(forecasts_at(truth=10.0, point=10.0, spread=2.0), families=("accuracy",))
    broken = compute(forecasts_at(truth=10.0, point=99.0, spread=0.5), families=("accuracy",))

    assert calm["conformal_drift.h0"] < broken["conformal_drift.h0"]


def test_a_point_estimator_scores_none_rather_than_being_treated_as_a_distribution() -> None:
    data = forecasts_at(truth=10.0, point=10.0, spread=1.0)
    data.forecasts = [
        Forecast(
            t=f.t, src=f.src, dst=f.dst, path_id=f.path_id, horizon_s=f.horizon_s, point=f.point
        )
        for f in data.forecasts
    ]
    scored = compute(data, families=("accuracy",))

    assert scored["coverage"] is None and scored["crps"] is None


def test_a_forecast_whose_horizon_runs_past_the_end_is_dropped_not_scored_short() -> None:
    """Otherwise every long horizon is scored against the last sample and comes
    out looking exactly as good as the nowcast."""
    data = forecasts_at(truth=10.0, point=10.0, spread=1.0, n=20)
    data.forecasts = [
        Forecast(
            t=f.t,
            src=f.src,
            dst=f.dst,
            path_id=f.path_id,
            horizon_s=500.0,
            point=f.point,
            quantiles=f.quantiles,
        )
        for f in data.forecasts
    ]

    assert data.scored() == []
    assert compute(data, families=("accuracy",))["pinball"] is None


# --------------------------------------------------------------------------
# decision quality


def test_regret_is_the_gap_to_the_best_fixed_path() -> None:
    """The family that separates this from every open-loop SCION benchmark: a
    model can predict accurately and route badly, and only this sees it."""
    data = an_input(series=a_series([10.0] * 200, best=[4.0] * 200))
    scored = compute(data, families=("decision",))

    assert scored["regret_ms"] == pytest.approx(6.0)
    assert scored["regret_ratio"] == pytest.approx(2.5)


def test_a_model_that_took_the_best_path_has_no_regret() -> None:
    scored = compute(
        an_input(series=a_series([7.0] * 200, best=[7.0] * 200)), families=("decision",)
    )

    assert scored["regret_ms"] == pytest.approx(0.0)
    assert scored["regret_ratio"] == pytest.approx(1.0)


def test_regret_is_none_on_a_run_too_short_to_have_a_measured_tail() -> None:
    """A run shorter than the detectors' warmup has measured nothing, and saying
    so is better than averaging the warmup."""
    scored = compute(an_input(series=a_series([10.0] * 5, best=[1.0] * 5)), families=("decision",))

    assert scored["regret_ms"] is None


def test_realised_and_best_are_paired_sample_by_sample() -> None:
    """Names the bug: filtering each series for finite values on its own leaves
    two arrays of different lengths that still subtract, and the difference is
    then between samples taken at different instants."""
    series = a_series([10.0] * 200, best=[4.0] * 200)
    series.mean_cost_ms[3] = float("nan")
    scored = compute(an_input(series=series), families=("decision",))

    assert scored["regret_ms"] == pytest.approx(6.0)


# --------------------------------------------------------------------------
# stability


def test_convergence_reports_infinity_rather_than_a_large_number() -> None:
    """ "Settled at sample 9,999" and "never settled" are different findings and
    a large number reads as the first."""
    jumpy = [0.0 if i % 2 else 100.0 for i in range(200)]
    scored = compute(an_input(series=a_series(jumpy)), families=("stability",))

    assert scored["convergence_s"] == math.inf


def test_a_series_that_settles_reports_when_it_did() -> None:
    settling = [100.0] * 40 + [1.0] * 160
    scored = compute(an_input(series=a_series(settling)), families=("stability",))

    assert 30.0 <= scored["convergence_s"] <= 45.0


# --------------------------------------------------------------------------
# operational


def test_the_tail_of_the_decision_time_is_reported_beside_the_median() -> None:
    """A model with a good median and a bad tail overruns, and the median alone
    says it is fine."""
    data = an_input(latency_s=[1.0] * 90 + [100.0] * 10)
    scored = compute(data, families=("operational",))

    assert scored["decision_p50_s"] == pytest.approx(1.0)
    assert scored["decision_p95_s"] == pytest.approx(100.0)


def test_what_the_budget_refused_is_counted() -> None:
    scored = compute(an_input(), families=("operational",))

    assert scored["refusals"] == 2.0
    assert scored["late_calls"] == 1.0
    assert scored["calls_per_decision"] == pytest.approx(2.0)


def test_whether_the_grid_held_travels_with_the_numbers_it_would_invalidate() -> None:
    """A result file read back later has no other way to find out that every
    stability number in it is meaningless."""
    series = a_series([1.0] * 200)
    series.times[7] += 0.5
    scored = compute(an_input(series=series), families=("operational",))

    assert scored["grid_uniform"] == 0.0


# --------------------------------------------------------------------------
# the duplicated rendering


def test_the_truth_series_is_keyed_the_way_a_model_names_a_path() -> None:
    """``instrument`` may not import ``exposure``, so the path-id rendering is
    written down twice. This is the only thing keeping the copies equal, and the
    bug it guards is silent: a truth series keyed on the raw integer and a
    forecast keyed on the hex string never join, so every accuracy metric
    reports ``None`` and nothing fails.
    """
    from scionarena.exposure.session import _hex

    for value in (0, 1, 2252810224849662535, 2**64 - 1, -3):
        assert path_name(value) == _hex(value)
