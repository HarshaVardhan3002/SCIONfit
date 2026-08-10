"""Performance budgets, stated against the ``realistic`` tier.

M1's note: write this first, watch it fail, then build until it passes. The
failure mode it exists to prevent is building the substrate at toy scale and
finding out at M3 that it does not hold 2,000 ASes.

These are budgets, not benchmarks. They are deliberately loose enough not to
flake on a shared CI runner and tight enough that an order-of-magnitude
regression cannot pass. The tracked numbers live in ``benchmarks/baseline.json``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tracemalloc

import numpy as np
import pytest

from scionarena.core.linkstate import LinkState
from scionarena.core.scenario import Scenario, TimelineEvent, TopologySpec
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


def _counting_recomputes(state: LinkState) -> dict[str, int]:
    """Count how often the metric arrays are actually computed.

    Every memoised array goes through ``_remember`` exactly once per computation,
    so counting those calls counts recomputations without a clock in sight.
    """
    seen = {"n": 0}
    original = state._remember

    def remember(name: str, value: object) -> object:
        seen["n"] += 1
        return original(name, value)  # type: ignore[arg-type]

    state._remember = remember  # type: ignore[assignment, method-assign]
    return seen


def _one_step_over(state: LinkState, scopes, paths, rng) -> None:
    state.advance_to(state.t + 60.0)
    state.set_demand(rng.integers(0, state.n_ifaces, 200), rng.uniform(0, 500, 200))
    for scope in scopes:
        state.path_metrics_batch(paths[scope])


def test_link_metrics_are_computed_once_a_step_and_not_once_a_scope():
    """A step is: time moves, demand lands, every scope re-reads its paths.

    Fifty scopes with a few thousand paths between them is a modest closed loop
    and it is what M3 does every tick. The metric arrays are memoised per step
    for exactly that reason -- recomputing 40,000 directions once per scope would
    be twenty times over budget while looking like the same code.

    Stated as a count rather than as a wall clock, which is what this test
    asserted until it started failing on CI at 13.4 ms against a 10 ms budget
    while measuring 7.1 ms on the machine it was written on. Nothing had
    regressed; an absolute millisecond budget on an unspecified runner measures
    the runner. The timing of this same loop stays gated in
    ``benchmarks/run.py`` as ``realistic.link_metrics_batch_s``, where a
    calibration workload normalises for how fast the machine is and the gate is
    a 15% change in shape rather than an absolute number. The count is the
    property the memoisation exists for, and it holds on any hardware.
    """
    topo = synthetic(n_ases=REALISTIC.n_ases, n_links=REALISTIC.n_links, seed=0)
    store = SegmentStore.for_tier(topo, REALISTIC, seed=0)
    scopes = scope_sample(REALISTIC.n_ases, n=50)
    paths = {scope: [p.ifaces for p in store.paths_for(*scope)] for scope in scopes}
    n_paths = sum(len(p) for p in paths.values())
    assert n_paths > 1_000, f"only {n_paths} paths over {len(scopes)} scopes; not a real step"

    state = LinkState(topo, seed=0)
    counted = _counting_recomputes(state)
    steps = 10
    for _ in range(steps):
        _one_step_over(state, scopes, paths, np.random.default_rng(0))
    per_step = counted["n"] / steps

    # Four arrays -- utilisation, latency, loss, available bandwidth -- and one
    # cache that a step's worth of load and time changes invalidates once.
    assert per_step <= 8, (
        f"{per_step:.0f} metric recomputations per step over {len(scopes)} scopes; "
        f"the per-step cache is not holding across scopes"
    )

    # Invariant 6, applied to a test: the same measurement with the memoisation
    # defeated has to fail it, or it is measuring nothing. Dropping the cache
    # before each scope is what the pre-memoisation code did.
    naive = LinkState(topo, seed=0)
    naive_counted = _counting_recomputes(naive)
    naive.advance_to(naive.t + 60.0)
    for scope in scopes:
        naive._invalidate()
        naive.path_metrics_batch(paths[scope])
    assert naive_counted["n"] > 20 * per_step, (
        f"recomputing per scope cost {naive_counted['n']} computations against "
        f"{per_step:.0f}; this test cannot tell the two apart"
    )


def test_realistic_link_metrics_are_reported_in_wall_clock_too():
    """The number the count above replaced, measured and printed, not gated.

    Kept because "the property holds" and "the loop is fast enough on this
    machine" are two different claims and the second one is still worth seeing
    in the log next to the first.
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
        _one_step_over(state, scopes, paths, rng)
    per_step = (time.perf_counter() - start) / steps
    print(
        f"\nlink metrics over {len(scopes)} scopes: {per_step * 1e3:.2f}ms per step, "
        f"design budget {REALISTIC.step_budget_s * 1e3:.0f}ms on the reference machine"
    )


def test_realistic_link_state_is_a_few_megabytes():
    topo = synthetic(n_ases=REALISTIC.n_ases, n_links=REALISTIC.n_links, seed=0)
    state = LinkState(topo, seed=0)
    assert state.nbytes < 64 * 1024**2, f"link state is {state.nbytes / 1024**2:.1f} MiB"


def realistic_scenario(duration_s: float = 3_600.0, **changes: object) -> Scenario:
    """An hour of the realistic tier with something happening in it.

    An hour rather than a minute because the beacon interval is five minutes:
    a shorter run would measure a substrate that never re-signed anything and
    would report a step cost the real loop never sees.
    """
    scenario = Scenario(
        name="realistic-perf",
        topology=TopologySpec(tier="realistic"),
        duration_s=duration_s,
        step_s=1.0,
        **changes,  # type: ignore[arg-type]
    )
    return scenario.then(
        TimelineEvent(at_s=duration_s / 3, kind="link_degrade", params={"link": 5, "factor": 0.1}),
        TimelineEvent(at_s=duration_s / 2, kind="demand_surge", params={"mbps": 100.0}),
    )


def test_realistic_substrate_steps_within_budget():
    """M1's step criterion, over the whole substrate rather than one component.

    Clock, segments and link state advancing together, including the
    re-beaconing rounds that fall inside the hour.
    """
    world = realistic_scenario().build()

    start = time.perf_counter()
    steps = world.run()
    per_step = (time.perf_counter() - start) / steps

    assert per_step < REALISTIC.step_budget_s, (
        f"a substrate step took {per_step * 1e3:.3f}ms over {steps} steps, "
        f"budget {REALISTIC.step_budget_s * 1e3}ms"
    )


def test_realistic_substrate_builds_within_budget():
    start = time.perf_counter()
    world = realistic_scenario().build()
    elapsed = time.perf_counter() - start

    assert world.topology.n_ases == REALISTIC.n_ases
    assert elapsed < REALISTIC.build_budget_s, (
        f"building the substrate took {elapsed:.2f}s, budget {REALISTIC.build_budget_s}s"
    )


def test_the_same_scenario_hashes_the_same_in_another_process(tmp_path):
    """M1's determinism criterion: same seed, two processes, same trace hash.

    A separate process, not a separate object, because the failure this catches
    is a hash that depends on something process-local -- ``PYTHONHASHSEED``,
    dict iteration order, an address in a repr -- and none of those move within
    one interpreter.
    """
    scenario = Scenario(
        name="determinism", topology=TopologySpec(tier="smoke"), duration_s=1_200.0, step_s=5.0
    ).then(
        TimelineEvent(at_s=300.0, kind="link_degrade", params={"link": 0, "factor": 0.2}),
        TimelineEvent(at_s=600.0, kind="as_policy_filter", params={"as_": 1}),
        TimelineEvent(at_s=900.0, kind="demand_surge", params={"mbps": 150.0}),
    )
    path = tmp_path / "scenario.json"
    scenario.save(path)
    script = (
        "from scionarena.core.scenario import Scenario;"
        f"w = Scenario.load({str(path)!r}).build();"
        "w.run();"
        "print(w.digest())"
    )

    digests = set()
    for hashseed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hashseed}
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        )
        digests.add(out.stdout.strip())

    in_process = scenario.build()
    in_process.run()
    digests.add(in_process.digest())

    assert len(digests) == 1, f"the trace hash moved between processes: {digests}"


@pytest.mark.slow
def test_stress_topology_builds_at_all():
    """Not gated on time. Gated on not falling over."""
    elapsed, topo = build_seconds(STRESS.n_ases, STRESS.n_links)
    assert topo.n_ases == STRESS.n_ases
    print(f"\nstress build: {elapsed:.2f}s, {topo.nbytes / 1024**2:.1f} MiB")


@pytest.mark.slow
def test_stress_substrate_steps_at_all():
    """Not gated on time either. Recorded so a later regression is visible."""
    scenario = Scenario(
        name="stress-perf",
        topology=TopologySpec(tier="stress"),
        duration_s=600.0,
        step_s=1.0,
    )
    start = time.perf_counter()
    world = scenario.build()
    built = time.perf_counter() - start

    start = time.perf_counter()
    steps = world.run()
    per_step = (time.perf_counter() - start) / steps

    print(f"\nstress substrate: build {built:.2f}s, step {per_step * 1e3:.3f}ms")


# --------------------------------------------------------------------------
# the reference model, at the scope counts M3 asks for
#
# Regression tests for the bug that made ">= 100 concurrent scopes over
# realistic" impossible to demonstrate: ``ReferenceStochastic._path_load``
# asked, for every path and every link on it, which of the paths the model had
# ever seen crossed that link -- inside the MSA loop, so ``msa_iters`` times
# per advisory. ``self._paths`` accumulates across scopes and rounds, so the
# cost grew with the whole network rather than with the scope being advised,
# and a decision round at the realistic tier took minutes instead of seconds.


def _snapshot_with(paths):
    from scionarena.exposure.contracts import TopologySnapshot

    return TopologySnapshot(t=0.0, interfaces={}, paths=tuple(paths))


def _path(index: int, hops: int = 4):
    from scionarena.exposure.contracts import PathRef

    return PathRef(
        path_id=f"p{index}",
        src="1-ff00:0:1",
        dst="1-ff00:0:2",
        interfaces=tuple(f"if{(index + h) % 400}" for h in range(hops)),
    )


class _NoScan(dict):
    """A path table that refuses to be walked in its entirety."""

    def values(self):  # pragma: no cover - the point is that it is not called
        raise AssertionError("predict rescanned every path the model has seen")


def test_predict_does_not_rescan_every_path_the_model_has_seen():
    from scionarena.exposure.contracts import Demand
    from scionarena.reference.models import ReferenceStochastic

    model = ReferenceStochastic()
    seen = [_path(i) for i in range(500)]
    model.reset(_snapshot_with(seen))
    model._paths = _NoScan(model._paths)

    asked = seen[:20]
    demand = Demand(per_path={p.path_id: 1.0 / len(asked) for p in asked}, n_hosts=100)
    out = model.predict(_snapshot_with(asked), asked, demand=demand)
    assert len(out) == len(asked)


def test_the_interface_totals_are_what_the_scan_they_replaced_computed():
    """Same answer, one pass instead of one pass per path per link."""
    from scionarena.exposure.contracts import Demand
    from scionarena.reference.models import ReferenceStochastic

    model = ReferenceStochastic()
    seen = [_path(i) for i in range(60)]
    model.reset(_snapshot_with(seen))
    nd = Demand(per_path={p.path_id: float(i + 1) for i, p in enumerate(seen)}).normalised()

    totals = model._interface_load(nd)
    for iid in {i for p in seen for i in p.interfaces}:
        naive = sum(nd.get(q.path_id, 0.0) for q in seen if iid in q.interfaces)
        assert totals.get(iid, 0.0) == pytest.approx(naive)


class _Counting(dict):
    """A path table that records how often it was consulted."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lookups = 0

    def get(self, key, default=None):
        self.lookups += 1
        return super().get(key, default)


def test_the_cost_of_an_advisory_does_not_grow_with_the_rest_of_the_network():
    """The property, stated without a clock.

    A scope's advisory should cost what that scope's path set costs. Asserting
    it by timing is unreliable and, measured, too weak to catch the regression
    it is for: the quadratic version answered this same case in 1.5s.
    """
    from scionarena.exposure.contracts import SLA, Demand
    from scionarena.reference.models import ReferenceStochastic

    def lookups_with(n_seen: int) -> int:
        model = ReferenceStochastic()
        seen = [_path(i) for i in range(n_seen)]
        model.reset(_snapshot_with(seen))
        model._paths = _Counting(model._paths)
        asked = seen[:200]
        snapshot = _snapshot_with(asked)
        demand = Demand(per_path={p.path_id: 1.0 / len(asked) for p in asked}, n_hosts=200)
        model.predict(snapshot, asked, demand=demand)
        model.advise(snapshot, asked, SLA(), n_hosts=200)
        return model._paths.lookups

    small = lookups_with(400)
    large = lookups_with(20000)  # what 100 concurrent scopes leaves behind
    assert small == large, (
        f"advising one scope consulted {small} paths against a small network and "
        f"{large} against a large one; the cost is following the network again"
    )
