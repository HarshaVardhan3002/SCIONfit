"""The world must offer genuine alternatives or the probes measure nothing."""

import pytest

from scionarena.backends.analytical import World


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 1009, 2018, 31337])
def test_no_universal_interface(seed):
    """If one interface sits on every path, total load on it is invariant to
    the split, and demand conditioning becomes untestable. Regression test for
    a real bug: seeds 1009 and 2018 originally produced such topologies and
    made a correct model look demand-blind."""
    w = World(seed=seed)
    assert w.universal_interfaces() == []


@pytest.mark.parametrize("seed", [0, 5, 99])
def test_paths_are_coupled(seed):
    """...but paths must still share links, or R1 has nothing to detect."""
    w = World(seed=seed)
    counts = {i: sum(1 for p in w.paths if i in p.interfaces) for i in w.interfaces}
    assert max(counts.values()) >= 2


def test_cost_rises_with_load():
    w = World(seed=0)
    iid = next(iter(w.interfaces))
    prev = -1.0
    for load in (0.0, 0.1, 0.3, 0.5, 0.7):
        lat, bw, loss = w.link_metrics(iid, load)
        assert lat > prev
        prev = lat


def test_determinism():
    a, b = World(seed=3), World(seed=3)
    assert [p.interfaces for p in a.paths] == [p.interfaces for p in b.paths]


def test_observations_can_be_sparse():
    w = World(seed=0)
    obs = w.observe(coverage=0.0)
    assert obs == []
