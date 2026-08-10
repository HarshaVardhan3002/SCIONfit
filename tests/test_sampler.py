"""M4: the sampler, against a world advanced in deliberately awkward jumps.

The thing being tested is one property -- **samples land on multiples of
``interval_s`` in simulated time, whatever moved the clock there** -- so most of
these move the clock in ways a model plausibly would and then check the grid.
The awkward jumps are the point: a tool call costs whatever it costs, so the
substrate is routinely stopped mid-tick, and M3's sampler drifted precisely
because it was only ever asked to sample at moments the driver had chosen.

``test_a_grid_point_crossed_without_a_tick_is_counted_not_invented`` is the one
negative. There is no way to sample a moment that has already passed, so the
sampler is allowed to miss one; what it may not do is fill the hole and let a
detector read the fabrication as data.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.core.scenario import Scenario, Substrate
from scionarena.instrument.sampler import Sampler, Series


@pytest.fixture
def world() -> Substrate:
    return Scenario.for_tier("smoke", seed=7).build()


def sampler_for(world: Substrate, interval_s: float = 10.0) -> Sampler:
    """A sampler over two contested interfaces and one scope with hosts on it."""
    src, dst = 0, world.topology.n_ases - 1
    world.add_scope(src, dst)
    scope = (world.topology.as_name(src), world.topology.as_name(dst))
    return Sampler(
        world,
        interval_s=interval_s,
        tracked=[0, 1],
        scopes=[scope],
        indices=[(src, dst)],
    )


# --------------------------------------------------------------------------
# the grid


def test_samples_land_on_the_grid_when_the_world_is_stepped_evenly(world) -> None:
    with sampler_for(world) as sampler:
        world.run(until_s=100.0)
    assert sampler.series.times == [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert sampler.series.uniform()


def test_samples_land_on_the_grid_when_the_world_is_stepped_raggedly(world) -> None:
    """**The M3 bug, as a test.**

    Advances of 0.3 s and 17.9 s are what a model's tool calls look like from the
    substrate's side: nothing lands on a tick boundary and nothing is a whole
    number of anything. Under M3's sampling the samples came out at 10.3, 28.2,
    ... and the FFT was told they were ten seconds apart.
    """
    with sampler_for(world) as sampler:
        for dt in (0.3, 0.4, 17.9, 2.1, 0.05, 6.05, 25.2, 1.0):
            world.step(dt)
    assert sampler.series.times == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert sampler.series.uniform()
    assert sampler.series.jitter_s() == 0.0
    assert sampler.series.skipped == 0


def test_one_enormous_advance_still_samples_every_grid_point_inside_it(world) -> None:
    """A model that spent four minutes on one turn is not four minutes unobserved.

    This is the behavioural change M4 makes, not just an accuracy fix: the
    interval a slow model spends thinking is now *in* the series, as samples of a
    network nobody is advising.
    """
    with sampler_for(world) as sampler:
        world.step(240.0)
    assert len(sampler.series) == 24
    assert sampler.series.uniform()


def test_the_grid_is_absolute_not_relative_to_attachment(world) -> None:
    """Anchoring at the attach time is the bug that cost an hour.

    Attaching at t=0.13 -- one tool call in -- would put the grid on 0.13, 10.13,
    ..., none of which is a tick boundary, so every sample would land at the next
    tick after it. The grid is multiples of the interval in absolute time, so two
    runs that spent their setup differently sample the same instants.
    """
    world.step(0.13)
    with sampler_for(world) as sampler:
        world.run(until_s=40.0)
    assert sampler.series.times == [10.0, 20.0, 30.0, 40.0]


def test_nothing_is_sampled_before_the_first_whole_interval(world) -> None:
    """The uniform split the loop starts from is not a decision any model made."""
    with sampler_for(world) as sampler:
        world.step(9.0)
        assert len(sampler.series) == 0
        world.step(1.0)
        assert len(sampler.series) == 1


def test_detaching_stops_the_series(world) -> None:
    sampler = sampler_for(world)
    sampler.attach()
    world.run(until_s=30.0)
    sampler.detach()
    world.run(until_s=100.0)
    assert sampler.series.times == [10.0, 20.0, 30.0]


def test_attaching_twice_does_not_double_sample(world) -> None:
    sampler = sampler_for(world)
    sampler.attach()
    sampler.attach()
    world.run(until_s=20.0)
    assert sampler.series.times == [10.0, 20.0]


# --------------------------------------------------------------------------
# what it refuses


def test_an_interval_off_the_world_grid_is_refused(world) -> None:
    """Half a tick never coincides with a tick, so it can only ever drift."""
    with pytest.raises(ValueError, match="whole multiple"):
        sampler_for(world, interval_s=0.5)


def test_a_nonsense_interval_is_refused(world) -> None:
    for bad in (0.0, -10.0, float("inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            sampler_for(world, interval_s=bad)


def test_a_grid_point_crossed_without_a_tick_is_counted_not_invented(world) -> None:
    """The one hole the sampler cannot fill, and must not pretend to.

    ``Clock.advance_to`` moves time without dispatching or ticking -- it exists
    for substrate components stepped directly -- so a caller using it can carry
    the clock past grid points that will never be sampled. Jumping from 10 to 35
    passes two of them: the next tick, at 36, records one sample standing in for
    the later of the two, and the earlier one is simply gone.

    The sample is lost, the gap in ``times`` says so, and ``uniform()`` is false
    for the rest of the run. Interpolating instead would hand a detector a reading
    of a moment nobody observed, which is worse than a hole, because a hole is
    visible.
    """
    with sampler_for(world) as sampler:
        world.run(until_s=10.0)
        world.clock.advance_to(35.0)
        world.run(until_s=60.0)
    assert sampler.series.skipped == 1, "grid point 20 was passed and never sampled"
    assert not sampler.series.uniform()
    assert sampler.series.jitter_s() > 0.0
    assert 20.0 not in sampler.series.times
    assert 36.0 in sampler.series.times, "the late sample is recorded at the time it was taken"


# --------------------------------------------------------------------------
# what it records


def test_every_series_has_the_same_length_as_the_time_axis(world) -> None:
    """Ragged series would silently misalign every detector against its own x axis."""
    with sampler_for(world) as sampler:
        world.run(until_s=100.0)
    series = sampler.series
    n = len(series.times)
    for values in (*series.utilisation.values(), *series.advised_load.values()):
        assert len(values) == n
    for values in series.path_share.values():
        assert len(values) == n
    assert len(series.deviation) == n
    assert len(series.mean_cost_ms) == n


def test_advised_load_excludes_the_background_and_utilisation_does_not(world) -> None:
    """ADR 0010 decision 3, at the point where the two series are written.

    The two are the same reading over the same capacity, minus and plus the
    exogenous cross-traffic, so the total is never below the advised part and is
    above it wherever the background is nonzero -- which is everywhere, since the
    background is what makes tier 1 a network rather than an empty graph.
    """
    with sampler_for(world) as sampler:
        world.run(until_s=60.0)
    series = sampler.series
    for iface, total in series.utilisation.items():
        advised = series.advised_load[iface]
        assert all(t >= a for t, a in zip(total, advised, strict=True))
        assert all(t > a for t, a in zip(total, advised, strict=True)), (
            "no background traffic on a tracked interface, so the two series are "
            "measuring the same thing and ADR 0010's separation buys nothing"
        )


def test_a_severed_series_says_so(world) -> None:
    """A topology change renumbers interfaces, so it is not one series any more.

    Rare and planned, per ADR 0002, and the run continues -- but a detector
    reading across the break is reading two different networks and has to be able
    to find that out.
    """
    with sampler_for(world) as sampler:
        world.run(until_s=20.0)
        assert sampler.series.continuous()
        world.clock.at(25.0, "topology_change", {"links": [0]})
        world.run(until_s=60.0)
    assert not sampler.series.continuous()
    assert sampler.series.severed == 1
    assert sampler.series.uniform(), "the grid is unaffected; only the meaning is"


def test_a_deleted_interface_samples_as_nan_not_as_zero(world) -> None:
    """Zero is a reading -- an idle link -- and a detector cannot tell them apart."""
    n_ifaces = world.links.n_ifaces
    sampler = Sampler(
        world,
        interval_s=10.0,
        tracked=[n_ifaces - 1],
        scopes=[],
        indices=[],
    )
    with sampler:
        world.run(until_s=10.0)
        world.clock.at(15.0, "topology_change", {"links": [0, 1, 2]})
        world.run(until_s=40.0)
    values = sampler.series.utilisation[n_ifaces - 1]
    assert not np.isnan(values[0])
    assert np.isnan(values[-1])


# --------------------------------------------------------------------------
# the series' own reporting


def test_an_empty_series_is_uniform_and_has_no_jitter() -> None:
    """Vacuously, and without raising: a scope may exist for less than one interval."""
    series = Series(interval_s=10.0)
    assert series.uniform()
    assert series.jitter_s() == 0.0
    assert len(series) == 0
