"""Beaconing, expiry, re-signing, and the two identifiers.

The test this module exists for is
:func:`test_resigning_keeps_structural_id_and_changes_segment_id`. It is M1's
acceptance criterion and it is the thing probe R4 will later depend on: if
re-signing did not change ``segment_id``, R4 could not distinguish a model that
handles identity churn from one that ignores it, and the probe would pass
everything.

The composition tests exist because getting path construction subtly wrong
produces paths that look plausible -- right length, right endpoints -- and
violate SCION's valley-free rule, which no downstream measurement would catch.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.core.segments import (
    SEG_CORE,
    SEG_UP,
    BeaconPolicy,
    FilterPolicy,
    Segment,
    SegmentStore,
    structural_hash,
)
from scionarena.core.tiers import DEV
from scionarena.core.topology import (
    ROLE_CORE,
    ROLE_CUSTOMER,
    ROLE_PEER,
    ROLE_PROVIDER,
    Topology,
    synthetic,
)


@pytest.fixture(scope="module")
def dev_topo() -> Topology:
    return synthetic(n_ases=DEV.n_ases, n_links=DEV.n_links, seed=0)


@pytest.fixture
def store(dev_topo: Topology) -> SegmentStore:
    return SegmentStore.for_tier(dev_topo, DEV, seed=0)


def sample_pairs(topo: Topology, n: int = 40, seed: int = 3) -> list[tuple[int, int]]:
    rng = np.random.default_rng(seed)
    draw = rng.integers(0, topo.n_ases, size=(n, 2))
    return [(int(a), int(b)) for a, b in draw if a != b]


# --------------------------------------------------------------------------
# identity under re-signing — M1's acceptance criterion
# --------------------------------------------------------------------------


def test_resigning_keeps_structural_id_and_changes_segment_id(store: SegmentStore):
    """The whole corrected domain model in one assertion (ADR 0002).

    Same interfaces, new material. A model keyed on segment_id loses its memory
    here and nothing in its output says so.
    """
    before = {
        i: (seg.ifaces, seg.structural_id, seg.segment_id) for i, seg in store.iter_segments()
    }
    store.rebeacon()
    for i, seg in store.iter_segments():
        ifaces, structural_id, segment_id = before[i]
        assert seg.ifaces == ifaces, "re-signing must not change the interface sequence"
        assert seg.structural_id == structural_id
        assert seg.segment_id != segment_id, "re-signing must change the crypto-bound id"


def test_resigning_changes_path_ids_the_same_way(store: SegmentStore):
    """The property has to survive composition, because a model is shown paths,
    not segments."""
    src, dst = next((a, b) for a, b in sample_pairs(store.topology) if store.paths_for(a, b))
    before = {p.structural_id: p.segment_id for p in store.paths_for(src, dst)}
    store.rebeacon()
    after = store.paths_for(src, dst)
    assert {p.structural_id for p in after} == set(before), "the graph did not change"
    for path in after:
        assert path.segment_id != before[path.structural_id]


def test_generation_counter_advances(store: SegmentStore):
    assert all(seg.generation == 0 for _, seg in store.iter_segments())
    store.rebeacon()
    store.rebeacon()
    assert all(seg.generation == 2 for _, seg in store.iter_segments())


def test_identity_policy_selects_which_id_is_the_path_id(store: SegmentStore):
    src, dst = next((a, b) for a, b in sample_pairs(store.topology) if store.paths_for(a, b))
    path = store.paths_for(src, dst)[0]
    assert path.path_id("structural") == path.structural_id
    assert path.path_id("crypto_bound") == path.segment_id


def test_unknown_identity_policy_is_refused(store: SegmentStore):
    """Neither policy is the blessed default while Q1 is open, so a typo must
    not quietly fall back to one of them."""
    src, dst = next((a, b) for a, b in sample_pairs(store.topology) if store.paths_for(a, b))
    path = store.paths_for(src, dst)[0]
    with pytest.raises(ValueError, match="identity_policy"):
        path.path_id("stable")


def test_structural_hash_is_not_the_builtin_hash():
    """``hash(tuple)`` is salted per process. A structural id built on it would
    differ between runs of the same seed and break invariant 4."""
    assert structural_hash((4, 5)) == structural_hash([4, 5])
    assert structural_hash((4, 5)) != structural_hash((5, 4))


def test_structural_hash_is_stable_across_processes(tmp_path):
    import subprocess
    import sys

    script = (
        "from scionarena.core.segments import structural_hash; print(structural_hash((7,6,3,2)))"
    )
    outputs = set()
    for hashseed in ("1", "2"):
        env = {**__import__("os").environ, "PYTHONHASHSEED": hashseed}
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        )
        outputs.add(out.stdout.strip())
    assert len(outputs) == 1, f"structural id moved with PYTHONHASHSEED: {outputs}"


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_same_seed_gives_the_same_segments(dev_topo: Topology):
    a = SegmentStore.for_tier(dev_topo, DEV, seed=5)
    b = SegmentStore.for_tier(dev_topo, DEV, seed=5)
    assert a.digest() == b.digest()
    assert [s.segment_id for _, s in a.iter_segments()] == [
        s.segment_id for _, s in b.iter_segments()
    ]


def test_material_depends_on_the_seed_but_structure_does_not(dev_topo: Topology):
    """Beaconing order is topological; the signatures are not. Two seeds should
    produce the same segments carrying different material."""
    a = SegmentStore.for_tier(dev_topo, DEV, seed=5)
    b = SegmentStore.for_tier(dev_topo, DEV, seed=6)
    assert a.digest() == b.digest()
    assert [s.segment_id for _, s in a.iter_segments()] != [
        s.segment_id for _, s in b.iter_segments()
    ]


def test_paths_are_the_same_on_a_second_ask(store: SegmentStore):
    src, dst = next((a, b) for a, b in sample_pairs(store.topology) if store.paths_for(a, b))
    first = [p.structural_id for p in store.paths_for(src, dst)]
    store._path_cache.clear()  # not the cache answering
    assert [p.structural_id for p in store.paths_for(src, dst)] == first


# --------------------------------------------------------------------------
# what a segment is
# --------------------------------------------------------------------------


def test_up_segments_climb_provider_links_only(store: SegmentStore):
    """An up-segment that used a peer or customer link would be a valley, and a
    valley-free violation is invisible in every downstream measurement."""
    topo = store.topology
    for a in range(0, topo.n_ases, 5):
        for seg in store.up_segments(a):
            for i in range(seg.hop_count):
                here = seg.ases[i]
                iface = seg.ifaces[2 * i]
                neighbours, ifaces = topo.neighbours_by_role(here, ROLE_PROVIDER)
                assert iface in ifaces, f"AS {here} left on a non-provider link"
                assert seg.ases[i + 1] in neighbours


def test_up_segments_end_at_a_core_as(store: SegmentStore):
    topo = store.topology
    for a in range(0, topo.n_ases, 5):
        for seg in store.up_segments(a):
            assert seg.origin == a
            assert topo.as_is_core[seg.terminal]


def test_core_ases_have_a_zero_hop_up_segment(store: SegmentStore):
    topo = store.topology
    for a in np.flatnonzero(topo.as_is_core)[:5]:
        segs = store.up_segments(int(a))
        assert len(segs) == 1
        assert segs[0].hop_count == 0
        assert segs[0].ases == (int(a),)


def test_interfaces_and_ases_line_up(store: SegmentStore):
    topo = store.topology
    for _, seg in store.iter_segments():
        assert len(seg.ifaces) == 2 * (len(seg.ases) - 1)
        for i in range(seg.hop_count):
            near, far = seg.ifaces[2 * i], seg.ifaces[2 * i + 1]
            assert topo.far_iface(near) == far
            assert topo.as_of_iface(near) == seg.ases[i]
            assert topo.as_of_iface(far) == seg.ases[i + 1]


def test_core_segments_use_core_links(store: SegmentStore):
    topo = store.topology
    cores = [int(c) for c in np.flatnonzero(topo.as_is_core)]
    found = 0
    for c1 in cores[:4]:
        for c2 in cores[:4]:
            for seg in store.core_segments(c1, c2):
                found += 1
                assert seg.seg_type == SEG_CORE
                assert topo.as_is_core[list(seg.ases)].all()
    assert found, "no core segments were built at all; the test proved nothing"


def test_reversing_a_segment_keeps_the_material(store: SegmentStore):
    """A down-segment is a registered up-segment read backwards. Same beacon,
    same generation, same signature -- a different structural id, because the
    interface sequence really is a different sequence."""
    seg = next(s for _, s in store.iter_segments() if s.hop_count > 0 and s.seg_type == SEG_UP)
    back = seg.reversed_()
    assert back.ases == tuple(reversed(seg.ases))
    assert back.generation == seg.generation
    assert back.signature_id == seg.signature_id
    assert back.structural_id != seg.structural_id
    assert back.reversed_().structural_id == seg.structural_id


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def test_paths_start_and_end_where_asked(store: SegmentStore):
    for src, dst in sample_pairs(store.topology):
        for path in store.paths_for(src, dst):
            assert path.ases[0] == src
            assert path.ases[-1] == dst


def test_paths_do_not_repeat_an_as(store: SegmentStore):
    for src, dst in sample_pairs(store.topology):
        for path in store.paths_for(src, dst):
            assert len(set(path.ases)) == len(path.ases)


def test_path_hops_are_real_links(store: SegmentStore):
    topo = store.topology
    for src, dst in sample_pairs(store.topology, n=15):
        for path in store.paths_for(src, dst):
            for i in range(path.hop_count):
                near = path.ifaces[2 * i]
                assert topo.as_of_iface(near) == path.ases[i]
                assert topo.as_of_iface(topo.far_iface(near)) == path.ases[i + 1]


def test_paths_are_valley_free(store: SegmentStore):
    """Up-hops, then at most one lateral hop, then down-hops. A path that goes
    down and up again would have a customer carrying transit it never sold."""
    topo = store.topology
    for src, dst in sample_pairs(store.topology, n=15):
        for path in store.paths_for(src, dst):
            phase = 0  # 0 climbing, 1 lateral done, 2 descending
            for i in range(path.hop_count):
                here, iface = path.ases[i], path.ifaces[2 * i]
                role = next(
                    r
                    for r in (ROLE_PROVIDER, ROLE_PEER, ROLE_CUSTOMER, ROLE_CORE)
                    if iface in topo.neighbours_by_role(here, r)[1]
                )
                if role in (ROLE_PROVIDER, ROLE_CORE):
                    assert phase == 0, f"climbed again after descending: {path.ases}"
                elif role == ROLE_PEER:
                    assert phase <= 1, f"two lateral hops: {path.ases}"
                    phase = 2
                else:
                    phase = 2


def test_every_pair_of_ases_has_at_least_one_path(store: SegmentStore):
    """Zero paths must mean a policy filter or a real partition, never a search
    that gave up. The bounded core search has a shortest-path fallback for
    exactly this reason."""
    empty = [(a, b) for a, b in sample_pairs(store.topology, n=120) if not store.paths_for(a, b)]
    assert not empty, f"{len(empty)} pairs resolved to nothing, e.g. {empty[:3]}"


def test_paths_are_distinct(store: SegmentStore):
    for src, dst in sample_pairs(store.topology, n=15):
        ids = [p.structural_id for p in store.paths_for(src, dst)]
        assert len(ids) == len(set(ids))


def test_no_path_to_self(store: SegmentStore):
    assert store.paths_for(7, 7) == []


def test_path_expiry_is_the_earliest_component(store: SegmentStore):
    src, dst = next((a, b) for a, b in sample_pairs(store.topology) if store.paths_for(a, b))
    for path in store.paths_for(src, dst):
        assert path.expiry_s <= min(
            seg.expiry_s
            for _, seg in store.iter_segments()
            if seg.structural_id in path.component_ids
        )


def test_path_links_are_derivable(store: SegmentStore):
    """The link-state model is indexed by link id, so a path has to name its
    links without a lookup table."""
    topo = store.topology
    src, dst = next((a, b) for a, b in sample_pairs(store.topology) if store.paths_for(a, b))
    path = store.paths_for(src, dst)[0]
    assert len(path.links) == path.hop_count
    for link, iface in zip(path.links, path.ifaces[::2], strict=True):
        assert topo.link_of_iface(iface) == link


def test_limit_truncates_without_recomputing(store: SegmentStore):
    src, dst = next(
        (a, b) for a, b in sample_pairs(store.topology) if len(store.paths_for(a, b)) > 3
    )
    assert len(store.paths_for(src, dst, limit=3)) == 3
    assert [p.structural_id for p in store.paths_for(src, dst, limit=3)] == [
        p.structural_id for p in store.paths_for(src, dst)[:3]
    ]


# --------------------------------------------------------------------------
# laziness and eviction
# --------------------------------------------------------------------------


def test_paths_are_not_materialised_until_asked(dev_topo: Topology):
    """Nobody enumerates all paths. At the realistic tier that is two million
    scopes, and building them eagerly is the difference between a substrate that
    runs and one that does not."""
    store = SegmentStore.for_tier(dev_topo, DEV, seed=0)
    assert len(store._path_cache) == 0
    store.paths_for(3, 90)
    assert len(store._path_cache) == 1


def test_the_path_cache_evicts(dev_topo: Topology):
    store = SegmentStore.for_tier(dev_topo, DEV, seed=0, path_cache_size=8)
    for a, b in sample_pairs(dev_topo, n=40, seed=11):
        store.paths_for(a, b)
    assert len(store._path_cache) <= 8


def test_rebeaconing_invalidates_the_path_cache(store: SegmentStore):
    store.paths_for(3, 90)
    epoch = store.epoch
    store.rebeacon()
    assert store.epoch > epoch
    assert len(store._path_cache) == 0


# --------------------------------------------------------------------------
# time and expiry
# --------------------------------------------------------------------------


def test_advancing_time_refreshes_what_is_due(store: SegmentStore):
    assert store.advance_to(1.0) == 0, "nothing is due one second in"
    refreshed = store.advance_to(store.policy.interval_s * 2)
    assert refreshed == store.n_segments


def test_refresh_is_spread_out_by_jitter(store: SegmentStore):
    """If the whole world re-signed on one tick, a model could detect refresh
    from timing alone and the identity probe would measure nothing."""
    partial = store.advance_to(store.policy.interval_s * (1 - store.policy.jitter / 2))
    assert 0 < partial < store.n_segments


def test_segments_expire(store: SegmentStore):
    assert store.expired() == []
    assert len(store.expired(store.policy.lifetime_s + 1)) == store.n_segments


def test_time_does_not_run_backwards(store: SegmentStore):
    store.advance_to(100.0)
    with pytest.raises(ValueError, match="backwards"):
        store.advance_to(99.0)


def test_a_segment_outlives_the_interval_that_refreshes_it():
    with pytest.raises(ValueError, match="outlive"):
        BeaconPolicy(interval_s=100.0, lifetime_s=50.0)


def test_beacon_policy_rejects_nonsense():
    with pytest.raises(ValueError, match="positive"):
        BeaconPolicy(interval_s=0.0)
    with pytest.raises(ValueError, match="jitter"):
        BeaconPolicy(jitter=1.0)


# --------------------------------------------------------------------------
# policy filtering — availability changes with the graph untouched
# --------------------------------------------------------------------------


def test_filtering_removes_paths_without_touching_the_topology(store: SegmentStore):
    """An AS declining to propagate a segment is not a link failure. A model
    that infers "link down" from "path gone" is wrong, and the substrate has to
    be able to produce the situation that catches it."""
    src, dst = next(
        (a, b) for a, b in sample_pairs(store.topology) if len(store.paths_for(a, b)) > 1
    )
    before = len(store.paths_for(src, dst))
    digest = store.topology.digest()
    victim = store._up_by_as[src][0]
    store.filter_segments([victim])
    assert len(store.paths_for(src, dst)) < before
    assert store.topology.digest() == digest, "policy filtering changed the graph"


def test_unfiltering_restores_the_paths(store: SegmentStore):
    src, dst = next(
        (a, b) for a, b in sample_pairs(store.topology) if len(store.paths_for(a, b)) > 1
    )
    before = {p.structural_id for p in store.paths_for(src, dst)}
    store.filter_segments([store._up_by_as[src][0]])
    store.unfilter_segments()
    assert {p.structural_id for p in store.paths_for(src, dst)} == before


def test_filtering_survives_rebeaconing(store: SegmentStore):
    """Re-signing a filtered segment must not quietly republish it."""
    store.filter_segments([store._up_by_as[3][0]])
    store.rebeacon()
    assert store.filtered == frozenset({store._up_by_as[3][0]})


def test_every_resigning_reaches_the_feed_not_just_the_last_round(store: SegmentStore):
    """C3. ``_resign_all`` assigned the round's segment ids over the pending
    list instead of extending it, so a reader that drained less often than the
    world re-signed saw only the most recent round. Measured at 81% of
    re-signings lost, and the corrected 5 s beacon interval multiplies the
    number of rounds between two drains by sixty.

    Counted against the generation counters, which cannot be fooled by the
    accumulator itself: a segment's ``generation`` increments once per
    re-signing whatever the feed does.
    """
    before = sum(seg.generation for _, seg in store.iter_segments())
    for _ in range(4):
        store.rebeacon()

    drained = store.drain_resigned()
    after = sum(seg.generation for _, seg in store.iter_segments())

    assert len(drained) == after - before == 4 * store.n_segments
    assert store.drain_resigned() == [], "draining twice reported the same events twice"


def test_a_policy_filter_catches_core_segments_discovered_later(store: SegmentStore):
    """C1. The filter used to be a set of segment ids snapshotted when the
    event fired, and core segments are discovered lazily on first ask -- so
    every core segment composed after the event carried a fresh id that was
    never in the set, and roughly eleven thousand paths at the ``dev`` tier
    went on traversing an AS the scenario had filtered. Nothing reported it:
    ``filtered`` said the filter was applied, and it was, to the segments that
    happened to exist at the time.

    Composing *before* installing the policy would hide the bug, because the
    core cache would already be warm. The whole point is the segment that does
    not exist yet.
    """
    pairs = sample_pairs(store.topology, n=60, seed=11)
    victim = max(
        range(store.topology.n_ases),
        key=lambda a: sum(
            any(a in p.ases for p in store.paths_for(src, dst)) for src, dst in pairs
        ),
    )
    fresh = SegmentStore.for_tier(store.topology, DEV, seed=0)
    fresh.apply_filter_policy(FilterPolicy(as_=victim))

    leaked = [
        (src, dst, p) for src, dst in pairs for p in fresh.paths_for(src, dst) if victim in p.ases
    ]
    assert not leaked, (
        f"{len(leaked)} paths still traverse filtered AS {victim}; "
        f"first is {leaked[0][0]}->{leaked[0][1]} via {leaked[0][2].ases}"
    )


def test_a_partial_policy_filter_decides_per_segment_not_per_arrival(store: SegmentStore):
    """C1, the ``fraction`` half. A partial filter drew a random subset of the
    segments that existed when it fired, so which segments it hid depended on
    which had been composed first. Keyed on the structural id instead, the
    decision is a property of the segment, so two stores that discover the same
    segments in a different order hide the same ones.
    """
    pairs = sample_pairs(store.topology, n=30, seed=5)
    policy = FilterPolicy.seeded(3, 0.5, seed=7, at_s=30.0)

    eager = SegmentStore.for_tier(store.topology, DEV, seed=0)
    eager.apply_filter_policy(policy)
    for src, dst in pairs:
        eager.paths_for(src, dst)

    lazy = SegmentStore.for_tier(store.topology, DEV, seed=0)
    for src, dst in reversed(pairs):
        lazy.paths_for(src, dst)
    lazy.apply_filter_policy(policy)

    def hidden(store_: SegmentStore) -> set[int]:
        return {store_.segment(i).structural_id for i in store_.filtered}

    assert hidden(eager) == hidden(lazy)
    assert hidden(eager), "a 50% filter on a well-connected AS hid nothing"


def test_unfiltering_everything_also_lifts_the_policy(store: SegmentStore):
    """Otherwise the policy keeps hiding segments composed after the unfilter,
    and a scenario's ``as_policy_unfilter`` only half restores the world."""
    src, dst = next(
        (a, b) for a, b in sample_pairs(store.topology) if len(store.paths_for(a, b)) > 1
    )
    victim = store.paths_for(src, dst)[0].ases[1]

    fresh = SegmentStore.for_tier(store.topology, DEV, seed=0)
    fresh.apply_filter_policy(FilterPolicy(as_=victim))
    fresh.unfilter_segments()

    assert fresh.filter_policies == ()
    assert any(victim in p.ases for p in fresh.paths_for(src, dst))


# --------------------------------------------------------------------------
# a segment on its own
# --------------------------------------------------------------------------


def test_segment_validity_window():
    seg = Segment(
        seg_type=SEG_UP,
        ifaces=(0, 1),
        ases=(0, 1),
        structural_id=structural_hash((0, 1)),
        generation=0,
        signature_id=1,
        created_s=10.0,
        expiry_s=20.0,
    )
    assert not seg.is_valid_at(9.9)
    assert seg.is_valid_at(10.0)
    assert seg.is_valid_at(19.9)
    assert not seg.is_valid_at(20.0)
