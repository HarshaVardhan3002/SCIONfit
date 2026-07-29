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

import pytest

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


@pytest.mark.slow
def test_stress_topology_builds_at_all():
    """Not gated on time. Gated on not falling over."""
    elapsed, topo = build_seconds(STRESS.n_ases, STRESS.n_links)
    assert topo.n_ases == STRESS.n_ases
    print(f"\nstress build: {elapsed:.2f}s, {topo.nbytes / 1024**2:.1f} MiB")
