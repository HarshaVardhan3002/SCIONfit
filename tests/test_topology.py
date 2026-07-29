"""The static graph: immutability, determinism, and the relationship algebra.

ADR 0002 makes physical topology static for the duration of a run. These tests
are what "static" means operationally, and the role tests exist because getting
the parent-child orientation backwards would invert every up-segment in the
substrate while leaving every count and every timing looking correct.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from scionarena.core.tiers import DEV, REALISTIC, SMOKE
from scionarena.core.topology import REL_CORE, Topology, synthetic


@pytest.fixture(scope="module")
def smoke() -> Topology:
    return synthetic(n_ases=SMOKE.n_ases, n_links=SMOKE.n_links, seed=0)


@pytest.fixture(scope="module")
def dev() -> Topology:
    return synthetic(n_ases=DEV.n_ases, n_links=DEV.n_links, seed=0)


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_same_seed_gives_the_same_graph():
    a = synthetic(n_ases=200, n_links=800, seed=7)
    b = synthetic(n_ases=200, n_links=800, seed=7)
    assert a.digest() == b.digest()


def test_different_seed_gives_a_different_graph():
    a = synthetic(n_ases=200, n_links=800, seed=7)
    b = synthetic(n_ases=200, n_links=800, seed=8)
    assert a.digest() != b.digest()


# --------------------------------------------------------------------------
# immutability (ADR 0002)
# --------------------------------------------------------------------------


def test_link_arrays_reject_writes(smoke: Topology):
    """Not a convention. numpy refuses."""
    with pytest.raises(ValueError, match="read-only"):
        smoke.link_capacity_mbps[0] = 1.0


def test_as_arrays_reject_writes(smoke: Topology):
    with pytest.raises(ValueError, match="read-only"):
        smoke.as_is_core[0] = True


def test_adjacency_rejects_writes(smoke: Topology):
    with pytest.raises(ValueError, match="read-only"):
        smoke.adj_neighbour[0] = 0


def test_topology_fields_reject_rebinding(smoke: Topology):
    with pytest.raises(dataclasses.FrozenInstanceError):
        smoke.seed = 1  # type: ignore[misc]


def test_scheduled_change_produces_a_new_topology(dev: Topology):
    """The only sanctioned way the graph changes. The original is untouched, so
    a trace can hold both and say which was in force when."""
    before = dev.digest()
    after = dev.without_links([0, 1, 2])
    assert after.n_links == dev.n_links - 3
    assert dev.digest() == before
    assert after.digest() != before
    after.validate()


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [SMOKE, DEV])
def test_generator_hits_the_link_target(tier):
    topo = synthetic(n_ases=tier.n_ases, n_links=tier.n_links, seed=0)
    assert topo.n_links == tier.n_links


@pytest.mark.parametrize("tier", [SMOKE, DEV])
def test_no_isolated_ases(tier):
    """An AS with no links is a hole every path query has to special-case."""
    topo = synthetic(n_ases=tier.n_ases, n_links=tier.n_links, seed=0)
    degrees = np.diff(topo.adj_indptr)
    assert degrees.min() >= 1


def test_every_isd_has_a_core(dev: Topology):
    for isd in range(dev.n_isds):
        assert dev.core_ases_of_isd(isd).size >= 1


def test_core_links_join_core_ases(dev: Topology):
    core_links = dev.link_rel == REL_CORE
    assert dev.as_is_core[dev.link_a[core_links]].all()
    assert dev.as_is_core[dev.link_b[core_links]].all()


def test_no_duplicate_links(dev: Topology):
    pairs = {(min(a, b), max(a, b)) for a, b in zip(dev.link_a, dev.link_b, strict=True)}
    assert len(pairs) == dev.n_links


def test_no_self_loops(dev: Topology):
    assert not np.any(dev.link_a == dev.link_b)


# --------------------------------------------------------------------------
# the relationship algebra
# --------------------------------------------------------------------------


def test_provider_and_customer_are_the_same_link_seen_from_two_ends(dev: Topology):
    """If b is a's provider then a is b's customer. Getting this backwards
    inverts every up-segment while leaving every count correct."""
    for a in range(0, dev.n_ases, 7):
        providers, _ = dev.providers(a)
        for p in providers:
            customers, _ = dev.customers(int(p))
            assert a in customers


def test_peering_is_symmetric(dev: Topology):
    for a in range(0, dev.n_ases, 7):
        peers, _ = dev.peers(a)
        for p in peers:
            back, _ = dev.peers(int(p))
            assert a in back


def test_customer_provider_graph_is_acyclic(dev: Topology):
    """A cycle here would make up-segment construction non-terminating."""
    depth = np.zeros(dev.n_ases, dtype=int)
    for a in range(dev.n_ases):
        providers, _ = dev.providers(a)
        if providers.size:
            depth[a] = 1 + max(depth[int(p)] for p in providers)
    for a in range(dev.n_ases):
        providers, _ = dev.providers(a)
        for p in providers:
            assert depth[int(p)] < depth[a]


def test_roles_partition_the_adjacency(dev: Topology):
    """Every neighbour has exactly one role and the roles sum to the degree."""
    for a in range(0, dev.n_ases, 11):
        total = sum(dev.neighbours_by_role(a, role)[0].size for role in range(4))
        assert total == dev.degree(a)


def test_interfaces_pair_up(dev: Topology):
    for link in range(0, dev.n_links, 13):
        near, far = 2 * link, 2 * link + 1
        assert dev.far_iface(near) == far
        assert dev.far_iface(far) == near
        assert dev.as_of_iface(near) == int(dev.link_a[link])
        assert dev.as_of_iface(far) == int(dev.link_b[link])
        assert dev.link_of_iface(near) == dev.link_of_iface(far) == link


# --------------------------------------------------------------------------
# the name side table
# --------------------------------------------------------------------------


def test_names_round_trip(dev: Topology):
    for index in (0, 1, dev.n_ases // 2, dev.n_ases - 1):
        assert dev.as_index(dev.as_name(index)) == index


def test_names_carry_the_isd(dev: Topology):
    for index in (0, dev.n_ases // 3, dev.n_ases - 1):
        assert dev.as_name(index).startswith(f"{int(dev.as_isd[index]) + 1}-")


def test_unknown_name_says_so(dev: Topology):
    with pytest.raises(KeyError, match="no such AS"):
        dev.as_index("99-ff00:0:dead")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def test_validate_rejects_a_self_loop(dev: Topology):
    broken = Topology(
        as_isd=dev.as_isd,
        as_is_core=dev.as_is_core,
        as_names=dev.as_names,
        link_a=np.array([3, 4], dtype=np.int32),
        link_b=np.array([3, 5], dtype=np.int32),
        link_rel=np.zeros(2, dtype=np.int8),
        link_capacity_mbps=np.ones(2, dtype=np.float32),
        link_latency_ms=np.ones(2, dtype=np.float32),
        link_mtu=np.full(2, 1400, dtype=np.int32),
        adj_indptr=np.zeros(dev.n_ases + 1, dtype=np.int64),
        adj_neighbour=np.zeros(4, dtype=np.int32),
        adj_iface=np.zeros(4, dtype=np.int32),
        adj_link=np.zeros(4, dtype=np.int32),
        adj_role=np.zeros(4, dtype=np.int8),
    )
    with pytest.raises(ValueError, match="self-loop"):
        broken.validate()


def test_tier_scale_definitions_are_ordered():
    assert SMOKE.n_ases < DEV.n_ases < REALISTIC.n_ases
    assert REALISTIC.build_budget_s is not None
    assert REALISTIC.mean_degree == pytest.approx(10.0)
