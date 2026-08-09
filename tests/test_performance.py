"""Performance budgets, stated against the ``realistic`` tier.

M1's note: write this first, watch it fail, then build until it passes. The
failure mode it exists to prevent is building the substrate at toy scale and
finding out at M3 that it does not hold 2,000 ASes.

These are budgets, not benchmarks. They are deliberately loose enough not to
flake on a shared CI runner and tight enough that an order-of-magnitude
regression cannot pass. The tracked numbers live in ``benchmarks/baseline.json``.
"""

from __future__ import annotations

import time
import tracemalloc

import numpy as np
import pytest

from scionarena.core.linkstate import LinkState
from scionarena.core.segments import SegmentStore
from scionarena.core.tiers import REALISTIC, STRESS
from scionarena.core.topology import synthetic


def build_seconds(n_ases: int, n_links: int, seed: int = 0) -> tuple[float, object]:
    start = time.perf_counter()
    topo = synthetic(n_ases=n_ases, n_links=n_links, seed=seed)
    return time.perf_counter() - start, topo


def test_realistic_topology_builds_within_budget():
    elapsed, topo = build_seconds(REALISTIC.n_ases, REALISTIC.n_links)
    assert topo.n_ases == REALISTIC.n_ases
    assert elapsed < REALISTIC.build_budget_s, (
        f"realistic topology took {elapsed:.2f}s, budget {REALISTIC.build_budget_s}s"
    )


def test_realistic_topology_fits_in_the_memory_budget():
    """Array-backed means the whole graph is a few tens of MB. A Python object
    per link would be two orders of magnitude worse and would only show up here."""
    tracemalloc.start()
    try:
        topo = synthetic(n_ases=REALISTIC.n_ases, n_links=REALISTIC.n_links, seed=0)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert topo.nbytes < 200 * 1024**2, f"topology arrays are {topo.nbytes / 1024**2:.1f} MiB"
    assert peak < REALISTIC.memory_budget_bytes


def realistic_store() -> tuple[float, SegmentStore]:
    topo = synthetic(n_ases=REALISTIC.n_ases, n_links=REALISTIC.n_links, seed=0)
    start = time.perf_counter()
    store = SegmentStore.for_tier(topo, REALISTIC, seed=0)
    return time.perf_counter() - start, store


def scope_sample(n_ases: int, n: int = 200, seed: int = 4) -> list[tuple[int, int]]:
    draw = np.random.default_rng(seed).integers(0, n_ases, size=(n, 2))
    return [(int(a), int(b)) for a, b in draw if a != b]


def test_realistic_beaconing_builds_within_budget():
    elapsed, store = realistic_store()
    assert store.n_segments > REALISTIC.n_ases, "fewer segments than ASes; beaconing did nothing"
    assert elapsed < REALISTIC.build_budget_s, (
        f"beaconing took {elapsed:.2f}s, budget {REALISTIC.build_budget_s}s"
    )


def test_realistic_path_query_fits_in_the_step_budget():
    """A cold scope is the worst case: it walks the core graph. It still has to
    fit inside one simulated step, because the network does not wait
    (invariant 3) and a slow query would be charged to the world, not the model.
    """
    _, store = realistic_store()
    scopes = scope_sample(REALISTIC.n_ases)
    start = time.perf_counter()
    for src, dst in scopes:
        store.paths_for(src, dst)
    worst = time.perf_counter() - start
    mean = worst / len(scopes)
    assert mean < REALISTIC.step_budget_s, (
        f"cold path query averaged {mean * 1e3:.2f}ms, step budget {REALISTIC.step_budget_s * 1e3}ms"
    )


def test_realistic_paths_per_pair_lands_in_the_tier_band():
    """12 ASes is a demo. So is 12 paths per pair. The tier band is the target
    the whole substrate -- generator fan-out, beaconing breadth, composition --
    has to add up to.

    The mean is asserted, not the minimum: a single-homed stub genuinely has few
    paths, and flattening that out would be less realistic, not more.
    """
    _, store = realistic_store()
    floor, ceiling = REALISTIC.paths_per_pair
    counts = [len(store.paths_for(src, dst)) for src, dst in scope_sample(REALISTIC.n_ases, n=120)]
    mean = sum(counts) / len(counts)
    assert floor <= mean <= ceiling, f"mean paths per pair is {mean:.0f}, band is {floor}-{ceiling}"
    assert min(counts) > 0, "some scope resolved to nothing at all"


def test_rebeaconing_the_whole_world_fits_in_a_step():
    """Re-signing every segment at once is the worst refresh tick there is."""
    _, store = realistic_store()
    start = time.perf_counter()
    store.rebeacon()
    elapsed = time.perf_counter() - start
    assert elapsed < 20 * REALISTIC.step_budget_s, (
        f"a full re-beacon of {store.n_segments} segments took {elapsed * 1e3:.0f}ms"
    )


def test_realistic_link_metrics_fit_in_the_step_budget():
    """A step is: time moves, demand lands, every scope re-reads its paths.

    Fifty scopes with a few thousand paths between them is a modest closed loop
    and it is what M3 will do every tick. The metric arrays are memoised per
    step for exactly this reason -- recomputing 40,000 directions once per scope
    would be twenty times over budget while looking like the same code.
    """
    topo = synthetic(n_ases=REALISTIC.n_ases, n_links=REALISTIC.n_links, seed=0)
    state = LinkState(topo, seed=0)
    store = SegmentStore.for_tier(topo, REALISTIC, seed=0)
    rng = np.random.default_rng(0)
    scopes = scope_sample(REALISTIC.n_ases, n=50)
    paths = {scope: [p.ifaces for p in store.paths_for(*scope)] for scope in scopes}

    start = time.perf_counter()
    steps = 10
    for _ in range(steps):
        state.advance_to(state.t + 60.0)
        state.set_demand(rng.integers(0, state.n_ifaces, 200), rng.uniform(0, 500, 200))
        for scope in scopes:
            state.path_metrics_batch(paths[scope])
    per_step = (time.perf_counter() - start) / steps
    assert per_step < REALISTIC.step_budget_s, (
        f"a step over {len(scopes)} scopes took {per_step * 1e3:.2f}ms, "
        f"budget {REALISTIC.step_budget_s * 1e3}ms"
    )


def test_realistic_link_state_is_a_few_megabytes():
    topo = synthetic(n_ases=REALISTIC.n_ases, n_links=REALISTIC.n_links, seed=0)
    state = LinkState(topo, seed=0)
    assert state.nbytes < 64 * 1024**2, f"link state is {state.nbytes / 1024**2:.1f} MiB"


@pytest.mark.slow
def test_stress_topology_builds_at_all():
    """Not gated on time. Gated on not falling over."""
    elapsed, topo = build_seconds(STRESS.n_ases, STRESS.n_links)
    assert topo.n_ases == STRESS.n_ases
    print(f"\nstress build: {elapsed:.2f}s, {topo.nbytes / 1024**2:.1f} MiB")
