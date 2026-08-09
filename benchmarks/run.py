"""The benchmark suite. Any change to ``core/`` runs this.

    python benchmarks/run.py            # measure and compare against the baseline
    python benchmarks/run.py --update   # record a new baseline
    python benchmarks/run.py --tier dev # smaller, for a quick look

A regression beyond 15% on the ``realistic`` tier exits non-zero, which is how
CI fails on it.

**Why the numbers are normalised.** A baseline recorded on one machine and
compared on another measures the machines, not the code. Each run therefore
also times a fixed numpy workload -- the calibration -- and comparisons scale
the baseline by the ratio of calibrations. A runner half the speed of the
machine that recorded the baseline expects every number to be twice as large,
and only a change in the *shape* of the cost trips the gate.

That correction is imperfect: it assumes the code and the calibration scale
together, and they will not exactly. It is much better than not correcting,
and the alternative -- a gate that fires whenever CI is busy -- gets muted
within a week and then protects nothing.

Timings are the **minimum** of the repeats, not the mean. The minimum is the
one measurement that noise can only move in one direction.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from scionarena.core.linkstate import LinkState
from scionarena.core.scenario import Scenario, TimelineEvent, TopologySpec
from scionarena.core.segments import SegmentStore
from scionarena.core.tiers import TIERS
from scionarena.core.tiers import tier as tier_by_name
from scionarena.core.topology import synthetic

BASELINE = Path(__file__).parent / "baseline.json"

#: A regression beyond this fraction on a gated tier fails CI.
TOLERANCE = 0.15

#: Only this tier is gated. The others are recorded so a regression is visible.
GATED_TIER = "realistic"


def calibration_s(repeats: int = 7) -> float:
    """A fixed workload, to normalise for how fast this machine is.

    Deliberately mixed. The substrate is part interpreter-bound (segment
    composition walks the graph in Python) and part numpy-bound (metric arrays
    over every interface), and a calibration that was only one of those tracked
    the wrong half: a run where numpy happened to get more memory bandwidth
    then looked like a code regression everywhere else.
    """
    data = np.random.default_rng(0).random(500_000)
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        total = 0
        for i in range(200_000):
            total += i % 7
        np.sort(data.copy())
        float(np.sqrt(data).sum() + total)
        best = min(best, time.perf_counter() - start)
    return best


def best_of(repeats: int, fn: Callable[[], float]) -> float:
    return min(fn() for _ in range(repeats))


def scope_sample(n_ases: int, n: int, seed: int = 4) -> list[tuple[int, int]]:
    rng = np.random.default_rng(seed)
    pairs = rng.integers(0, n_ases, size=(n * 2, 2))
    return [(int(a), int(b)) for a, b in pairs if a != b][:n]


def measure_tier(name: str, *, repeats: int = 3) -> dict[str, float]:
    """Every number in one tier's row of the baseline."""
    band = tier_by_name(name)
    out: dict[str, float] = {}

    def build_topology() -> float:
        start = time.perf_counter()
        synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)
        return time.perf_counter() - start

    out["topology_build_s"] = best_of(repeats, build_topology)

    topo = synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)

    def build_segments() -> float:
        start = time.perf_counter()
        SegmentStore.for_tier(topo, band, seed=0)
        return time.perf_counter() - start

    out["beaconing_build_s"] = best_of(repeats, build_segments)

    scopes = scope_sample(band.n_ases, n=50)

    def cold_query() -> float:
        store = SegmentStore.for_tier(topo, band, seed=0)
        start = time.perf_counter()
        for src, dst in scopes:
            store.paths_for(src, dst)
        return (time.perf_counter() - start) / len(scopes)

    out["path_query_cold_s"] = best_of(repeats, cold_query)

    store = SegmentStore.for_tier(topo, band, seed=0)
    paths = {scope: [p.ifaces for p in store.paths_for(*scope)] for scope in scopes}

    def metrics_batch() -> float:
        state = LinkState(topo, seed=0)
        start = time.perf_counter()
        for scope in scopes:
            state.path_metrics_batch(paths[scope])
        return (time.perf_counter() - start) / len(scopes)

    out["link_metrics_batch_s"] = best_of(repeats, metrics_batch)

    duration_s = 3_600.0

    def substrate_step() -> float:
        scenario = Scenario(
            name=f"bench-{name}",
            topology=TopologySpec(tier=name),
            duration_s=duration_s,
            step_s=1.0,
        ).then(
            TimelineEvent(
                at_s=duration_s / 3, kind="link_degrade", params={"link": 5, "factor": 0.1}
            ),
            TimelineEvent(at_s=duration_s / 2, kind="demand_surge", params={"mbps": 100.0}),
        )
        world = scenario.build()
        start = time.perf_counter()
        steps = world.run()
        return (time.perf_counter() - start) / steps

    out["substrate_step_s"] = best_of(max(1, repeats - 1), substrate_step)

    out["topology_bytes"] = float(topo.nbytes)
    out["link_state_bytes"] = float(LinkState(topo, seed=0).nbytes)
    out["n_segments"] = float(store.n_segments)
    return out


def measure(tiers: list[str], *, repeats: int = 3) -> dict[str, Any]:
    return {
        "schema": 2,
        "recorded_on": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
            "numpy": np.__version__,
        },
        "calibration_s": calibration_s(),
        "tolerance": TOLERANCE,
        "gated_tier": GATED_TIER,
        "tiers": {name: measure_tier(name, repeats=repeats) for name in tiers},
    }


def compare(
    baseline: dict[str, Any], current: dict[str, Any], *, tolerance: float
) -> tuple[list[str], list[str]]:
    """Regressions, as human sentences: ``(gating, all)``.

    An empty ``gating`` list means the suite is within budget. Regressions on
    an ungated tier are reported and do not fail; they are the early warning
    that the gated tier is about to move.
    """
    scale = current["calibration_s"] / baseline["calibration_s"]
    gating: list[str] = []
    everything: list[str] = []
    for tier_name, measured in current["tiers"].items():
        recorded = baseline["tiers"].get(tier_name)
        if not recorded:
            continue
        gated = tier_name == baseline.get("gated_tier", GATED_TIER)
        for metric, value in measured.items():
            was = recorded.get(metric)
            if was is None or was <= 0.0:
                continue
            # Byte counts and counts do not scale with machine speed.
            expected = was * scale if metric.endswith("_s") else was
            ratio = value / expected
            if ratio > 1.0 + tolerance:
                line = (
                    f"{tier_name}.{metric}: {value:.6g} vs {expected:.6g} expected "
                    f"({(ratio - 1) * 100:.0f}% slower)"
                )
                everything.append(line if gated else f"[not gated] {line}")
                if gated:
                    gating.append(line)
    return gating, everything


def report(current: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    scale = 1.0 if baseline is None else current["calibration_s"] / baseline["calibration_s"]
    print(f"calibration {current['calibration_s'] * 1e3:.1f}ms (machine factor {scale:.2f}x)")
    for tier_name, measured in current["tiers"].items():
        print(f"\n{tier_name}")
        recorded = (baseline or {}).get("tiers", {}).get(tier_name, {})
        for metric, value in measured.items():
            unit = "ms" if metric.endswith("_s") else ""
            shown = value * 1e3 if metric.endswith("_s") else value
            was = recorded.get(metric)
            if was:
                expected = was * scale if metric.endswith("_s") else was
                delta = (value / expected - 1) * 100
                print(f"  {metric:<24} {shown:>12.4g}{unit}  ({delta:+.0f}%)")
            else:
                print(f"  {metric:<24} {shown:>12.4g}{unit}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="record a new baseline")
    parser.add_argument(
        "--tier",
        action="append",
        choices=sorted(TIERS),
        help="tier to measure; repeatable. Default: dev and realistic.",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tolerance", type=float, default=TOLERANCE)
    parser.add_argument("--json", action="store_true", help="print the measurements as JSON")
    args = parser.parse_args(argv)

    tiers = args.tier or ["dev", "realistic"]
    current = measure(tiers, repeats=args.repeats)

    if args.json:
        print(json.dumps(current, indent=2, sort_keys=True))

    baseline: dict[str, Any] | None = None
    if BASELINE.exists():
        loaded = json.loads(BASELINE.read_text(encoding="utf-8"))
        if loaded.get("schema") == 2 and loaded.get("tiers"):
            baseline = loaded

    if args.update:
        merged = baseline or current
        if baseline is not None:
            merged = dict(current)
            merged["tiers"] = {**baseline["tiers"], **current["tiers"]}
        BASELINE.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report(current, None)
        print(f"\nbaseline written to {BASELINE}")
        return 0

    if baseline is None:
        report(current, None)
        print("\nno baseline recorded yet; run with --update")
        return 0

    report(current, baseline)
    gating, everything = compare(baseline, current, tolerance=args.tolerance)
    if everything:
        print("\nregressions:")
        for line in everything:
            print(f"  {line}")
    if gating:
        print(f"\nFAIL: {len(gating)} gated regression(s) beyond {args.tolerance:.0%}")
        return 1
    print(f"\nOK: within {args.tolerance:.0%} on {baseline.get('gated_tier', GATED_TIER)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
