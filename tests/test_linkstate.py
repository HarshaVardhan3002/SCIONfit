"""The link model: monotonicity, composition, and what the model cannot see.

:func:`test_cost_is_non_decreasing_in_offered_load` is M1's other named
acceptance criterion. Probe R7 asks whether a model has learned that cost rises
with load; if the substrate did not have that property, R7 would be testing
nothing and a model could pass it by being wrong in the same direction.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.core.linkstate import (
    SECONDS_PER_DAY,
    UTILISATION_CEILING,
    BackgroundParams,
    LinkParams,
    LinkState,
    fit_bpr,
)
from scionarena.core.segments import SegmentStore
from scionarena.core.tiers import DEV
from scionarena.core.topology import Topology, synthetic


@pytest.fixture(scope="module")
def dev_topo() -> Topology:
    return synthetic(n_ases=DEV.n_ases, n_links=DEV.n_links, seed=0)


@pytest.fixture
def state(dev_topo: Topology) -> LinkState:
    return LinkState(dev_topo, seed=0)


@pytest.fixture
def quiet(dev_topo: Topology) -> LinkState:
    """No background traffic, so a test can set utilisation exactly."""
    return LinkState(
        dev_topo, seed=0, background=BackgroundParams(mean_utilisation=0.0, noise_sigma=0.0)
    )


# --------------------------------------------------------------------------
# monotonicity — M1's acceptance criterion, and R7's foundation
# --------------------------------------------------------------------------


def test_cost_is_non_decreasing_in_offered_load(quiet: LinkState):
    """10,000 random states, each compared against itself under more load."""
    rng = np.random.default_rng(0)
    n = quiet.n_ifaces
    for _ in range(10_000 // n + 1):
        base = rng.uniform(0.0, 2.0, size=n) * quiet.capacity_mbps
        extra = rng.uniform(0.0, 1.0, size=n) * quiet.capacity_mbps
        quiet.set_demand(slice(None), base)  # type: ignore[arg-type]
        before = quiet.cost().copy()
        quiet.set_demand(slice(None), base + extra)  # type: ignore[arg-type]
        after = quiet.cost()
        worst = float((before - after).max())
        assert worst <= 1e-9, f"cost fell by {worst} when load rose"


def test_latency_and_loss_are_each_non_decreasing(quiet: LinkState):
    """Cost could be monotone while its parts were not, and a model is shown the
    parts."""
    rng = np.random.default_rng(1)
    n = quiet.n_ifaces
    for _ in range(200):
        base = rng.uniform(0.0, 1.5, size=n) * quiet.capacity_mbps
        quiet.set_demand(slice(None), base)  # type: ignore[arg-type]
        latency_before, loss_before = quiet.latency_ms().copy(), quiet.loss().copy()
        quiet.add_demand(slice(None), rng.uniform(0.0, 0.5, size=n) * quiet.capacity_mbps)  # type: ignore[arg-type]
        assert (quiet.latency_ms() >= latency_before - 1e-9).all()
        assert (quiet.loss() >= loss_before - 1e-9).all()


def test_available_bandwidth_is_non_increasing(quiet: LinkState):
    rng = np.random.default_rng(2)
    n = quiet.n_ifaces
    quiet.set_demand(slice(None), rng.uniform(0, 1, n) * quiet.capacity_mbps)  # type: ignore[arg-type]
    before = quiet.available_mbps().copy()
    quiet.add_demand(slice(None), rng.uniform(0, 1, n) * quiet.capacity_mbps)  # type: ignore[arg-type]
    assert (quiet.available_mbps() <= before + 1e-9).all()


def test_congestion_actually_bites(quiet: LinkState):
    """Monotone is not enough: a model has to be able to tell loaded from idle.
    A form that rose by a microsecond would satisfy the property above and
    measure nothing."""
    quiet.clear_demand()
    idle = quiet.cost().copy()
    quiet.set_demand(slice(None), quiet.capacity_mbps * 0.99)  # type: ignore[arg-type]
    loaded = quiet.cost()
    assert (loaded > 5 * idle).all(), "saturating a link barely changed its cost"


def test_the_queueing_tail_is_finite_at_saturation(quiet: LinkState):
    quiet.set_demand(slice(None), quiet.capacity_mbps * 100)  # type: ignore[arg-type]
    assert np.isfinite(quiet.latency_ms()).all()
    assert (quiet.utilisation() <= UTILISATION_CEILING + 1e-12).all()


def test_a_dead_link_is_not_a_fast_link(quiet: LinkState):
    """Zero usable capacity has to mean infinite utilisation, not zero. Dividing
    by a capacity of zero and getting a nan that clips to 0.0 would make a
    failed link look like the best one on the path list."""
    quiet.degrade(0, 0.0)
    quiet.clear_demand()
    assert quiet.utilisation()[0] == pytest.approx(UTILISATION_CEILING)
    assert quiet.loss()[0] > 0.3
    assert quiet.available_mbps()[0] == 0.0


# --------------------------------------------------------------------------
# per-direction state
# --------------------------------------------------------------------------


def test_directions_are_independent(quiet: LinkState):
    """Forward and reverse availability differ. One array per link would make
    that unrepresentable and the substrate would quietly be a different
    problem."""
    quiet.clear_demand()
    quiet.set_demand(0, quiet.capacity_mbps[0] * 0.99)
    assert quiet.cost()[0] > quiet.cost()[1]


def test_degradation_can_be_one_way(quiet: LinkState):
    quiet.degrade(5, 0.1, direction=0)
    assert quiet.health[10] == pytest.approx(0.1)
    assert quiet.health[11] == pytest.approx(1.0)


def test_restore_undoes_degradation(quiet: LinkState):
    quiet.degrade(5, 0.1)
    quiet.restore(5)
    assert quiet.health[10] == quiet.health[11] == 1.0


def test_health_is_refused_outside_zero_to_one(quiet: LinkState):
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        quiet.degrade(0, 1.5)


# --------------------------------------------------------------------------
# background traffic
# --------------------------------------------------------------------------


def test_background_moves_with_the_time_of_day(state: LinkState):
    utilisations = []
    for hour in range(0, 24, 3):
        state.advance_to(hour * 3600.0)
        utilisations.append(float(state.utilisation().mean()))
    assert max(utilisations) > 1.2 * min(utilisations), "the day is flat"


def test_the_weekend_is_quieter(dev_topo: Topology):
    weekday = LinkState(dev_topo, seed=0, background=BackgroundParams(noise_sigma=0.0))
    weekday.advance_to(2 * SECONDS_PER_DAY + 12 * 3600)  # Wednesday noon
    saturday = LinkState(dev_topo, seed=0, background=BackgroundParams(noise_sigma=0.0))
    saturday.advance_to(5 * SECONDS_PER_DAY + 12 * 3600)
    assert saturday.utilisation().mean() < weekday.utilisation().mean()


def test_links_do_not_all_peak_together(state: LinkState):
    """A world where they did would let a model infer the whole background from
    one link, which is not the problem we mean to pose."""
    watched = list(range(0, 40, 4))
    samples = []
    for hour in range(24):
        state.advance_to(hour * 3600.0)
        samples.append(state.utilisation()[watched])
    peaks = np.argmax(np.array(samples), axis=0)
    assert len(set(peaks.tolist())) > 3


def test_background_is_not_reachable_through_the_public_surface(state: LinkState):
    """Invariant 1 in its narrow form: the model cannot subtract out what it is
    not supposed to know. Anything reachable here reaches the exposure layer."""
    public = {name for name in dir(state) if not name.startswith("_")}
    assert "background_mbps" not in public
    for name in public:
        assert "background" not in name or name == "background_params", (
            f"{name} exposes the exogenous load"
        )


def test_background_depends_on_the_seed(dev_topo: Topology):
    a = LinkState(dev_topo, seed=1)
    b = LinkState(dev_topo, seed=2)
    a.advance_to(3600.0)
    b.advance_to(3600.0)
    assert a.digest() != b.digest()


def test_background_is_the_same_however_you_get_there(dev_topo: Topology):
    """Counter-based, not stateful. A run that stepped in 1 s increments and one
    that jumped straight there must see the same world, or invariant 4 holds
    only for identical step sequences."""
    stepper = LinkState(dev_topo, seed=3)
    for t in range(0, 601, 10):
        stepper.advance_to(float(t))
    jumper = LinkState(dev_topo, seed=3)
    jumper.advance_to(600.0)
    assert stepper.digest() == jumper.digest()


def test_time_does_not_run_backwards(state: LinkState):
    state.advance_to(100.0)
    with pytest.raises(ValueError, match="backwards"):
        state.advance_to(99.0)


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def test_latency_sums_bandwidth_minimises_loss_compounds(quiet: LinkState):
    quiet.clear_demand()
    ifaces = (0, 1, 4, 5, 8, 9)
    egress = [0, 4, 8]
    metrics = quiet.path_metrics(ifaces)
    assert metrics.hop_count == 3
    assert metrics.latency_ms == pytest.approx(float(quiet.latency_ms()[egress].sum()))
    assert metrics.bandwidth_mbps == pytest.approx(float(quiet.available_mbps()[egress].min()))
    per_hop = quiet.loss()[egress]
    assert metrics.loss == pytest.approx(1.0 - float(np.prod(1.0 - per_hop)))
    assert metrics.mtu == int(quiet.mtu[egress].min())


def test_loss_compounds_rather_than_sums(dev_topo: Topology):
    """Two 10% hops lose 19%, not 20%. Over a long path the difference is the
    difference between a usable estimate and a wrong one."""
    state = LinkState(
        dev_topo,
        seed=0,
        params=LinkParams(base_loss=0.1, max_loss=0.1),
        background=BackgroundParams(mean_utilisation=0.0, noise_sigma=0.0),
    )
    assert state.path_metrics((0, 1, 4, 5)).loss == pytest.approx(0.19)


def test_the_batch_agrees_with_the_one_at_a_time_version(quiet: LinkState, dev_topo: Topology):
    store = SegmentStore.for_tier(dev_topo, DEV, seed=0)
    paths = [p.ifaces for p in store.paths_for(3, 90)][:20]
    assert paths, "no paths to compose; the test proved nothing"
    latency, bandwidth, loss = quiet.path_metrics_batch(list(paths))
    for i, ifaces in enumerate(paths):
        one = quiet.path_metrics(ifaces)
        assert latency[i] == pytest.approx(one.latency_ms)
        assert bandwidth[i] == pytest.approx(one.bandwidth_mbps)
        assert loss[i] == pytest.approx(one.loss, abs=1e-9)


def test_an_empty_path_composes_to_nothing(quiet: LinkState):
    metrics = quiet.path_metrics(())
    assert metrics.hop_count == 0
    assert metrics.latency_ms == 0.0
    assert metrics.loss == 0.0


def test_batch_of_no_paths_is_empty(quiet: LinkState):
    latency, bandwidth, loss = quiet.path_metrics_batch([])
    assert latency.size == bandwidth.size == loss.size == 0


# --------------------------------------------------------------------------
# calibration hooks (M9)
# --------------------------------------------------------------------------


def test_fit_bpr_recovers_parameters_it_generated():
    u = np.linspace(0.05, 0.95, 60)
    ratio = 1.0 + 0.3 * u**3.0
    alpha, beta = fit_bpr(u, ratio)
    assert alpha == pytest.approx(0.3, abs=0.05)
    assert beta == pytest.approx(3.0, abs=0.2)


def test_fit_bpr_never_returns_a_negative_alpha():
    """A negative alpha would make latency fall with load, and every downstream
    monotonicity guarantee would be gone."""
    u = np.linspace(0.05, 0.95, 40)
    alpha, _ = fit_bpr(u, 1.0 - 0.2 * u)  # noisy or mismeasured input
    assert alpha >= 0.0


def test_fit_bpr_rejects_mismatched_input():
    with pytest.raises(ValueError, match="same non-empty shape"):
        fit_bpr(np.zeros(3), np.zeros(4))


def test_with_params_copies_rather_than_mutates(quiet: LinkState):
    quiet.set_demand(0, 100.0)
    harsher = quiet.with_params(alpha=2.0)
    assert harsher.params.alpha == 2.0
    assert quiet.params.alpha == LinkParams().alpha
    assert harsher.demand_mbps[0] == 100.0
    harsher.set_demand(0, 5.0)
    assert quiet.demand_mbps[0] == 100.0


def test_parameters_that_would_break_monotonicity_are_refused():
    with pytest.raises(ValueError, match="negative"):
        LinkParams(alpha=-0.1)
    with pytest.raises(ValueError, match="negative"):
        LinkParams(queue_ms=-1.0)
    with pytest.raises(ValueError, match="positive"):
        LinkParams(beta=0.0)


def test_loss_parameters_are_probabilities():
    with pytest.raises(ValueError, match="probabilities"):
        LinkParams(base_loss=0.5, max_loss=0.1)
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        LinkParams(loss_onset=1.0)


def test_background_parameters_are_checked():
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        BackgroundParams(mean_utilisation=1.0)
    with pytest.raises(ValueError, match="positive"):
        BackgroundParams(bucket_s=0.0)
