import pytest

from scionarena import Advisory, Dist, Prediction


def test_point_estimate_is_not_distributional():
    assert not Dist.point_estimate(5.0).is_distributional
    assert Dist(quantiles={0.1: 1, 0.5: 2, 0.9: 3}).is_distributional


def test_quantile_order_check():
    assert Dist(quantiles={0.1: 1, 0.5: 2, 0.9: 3}).quantiles_monotone()
    assert not Dist(quantiles={0.1: 3, 0.5: 2, 0.9: 1}).quantiles_monotone()


def test_advisory_entropy_bounds():
    one_hot = Advisory(weights={"a": 1.0, "b": 0.0, "c": 0.0})
    uniform = Advisory(weights={"a": 1.0, "b": 1.0, "c": 1.0})
    assert one_hot.normalised_entropy == pytest.approx(0.0)
    assert uniform.normalised_entropy == pytest.approx(1.0)
    assert one_hot.max_weight == 1.0


def test_advisory_normalises_unnormalised_input():
    a = Advisory(weights={"a": 2.0, "b": 2.0})
    assert abs(sum(a.normalised().values()) - 1.0) < 1e-12


def test_degenerate_weights_fall_back_to_uniform():
    a = Advisory(weights={"a": 0.0, "b": 0.0})
    assert a.normalised() == {"a": 0.5, "b": 0.5}


def test_cost_tolerates_nan_and_zero_bandwidth():
    p = Prediction(Dist.point_estimate(float("nan")),
                   Dist.point_estimate(0.0),
                   Dist.point_estimate(float("nan")))
    assert p.cost() == p.cost()      # not NaN
