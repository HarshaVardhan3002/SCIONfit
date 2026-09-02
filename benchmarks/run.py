"""The benchmark suite. Any change to ``core/`` runs this.

    python benchmarks/run.py            # measure and compare against the baseline
    python benchmarks/run.py --update   # record a new baseline
    python benchmarks/run.py --tier dev # smaller, for a quick look

A regression beyond ``TOLERANCE`` on the ``realistic`` tier exits non-zero,
which is how CI fails on it.

**Why each metric gets its own process.** After a few million segment
re-signings, everything in the process is about twice as slow and stays that
way -- a freshly built world run for 600 simulated seconds costs 2.1 ms per step
in a young process and 4.5 ms in an aged one, same code, same seed. This suite
used to measure all six metrics in one process with ``substrate_step_s`` last,
so the gated number was partly a function of how much allocation had happened
earlier in the run, which is not a property of the code under test. It reported
145% on a branch that touched no file under ``core/``. Each metric is now
measured in a subprocess that does its own setup, so measurement order is no
longer an input. See ADR 0014 and
``docs/evidence/substrate_step_process_ageing.py``.

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

How imperfect, measured: between the M1 baseline and M4, on the same machine and
the same interpreter, the calibration got 9% quicker while the substrate's own
timings were flat to 10% slower. The scaling turned that into +18-23% and would
have failed a 15% gate on code nobody had touched. That is where ``TOLERANCE``
below comes from, and why it is a measured number rather than a round one.

**Where the correction gives up: a different interpreter.** Machine speed is one
number and it can be divided out. A Python version is not. Measured on one
machine with numpy held at 2.4.6, moving from CPython 3.11 to 3.12 made
`beaconing_build_s` 15% slower and `substrate_step_s` 52% *faster*, while the
calibration itself got 8% quicker -- so the scaling pushed every number the
wrong way and reported three regressions that were not regressions. Different
code shapes shift by different amounts and one scalar cannot absorb that.

So the gate only fires when the interpreter's minor version matches the one the
baseline was recorded on. Elsewhere the numbers are printed and the comparison
is labelled unscaled rather than trusted, and re-recording with ``--update`` on
that interpreter is what turns the gate back on. Refusing to gate is
uncomfortable; a gate that cries wolf on every matrix leg is worse, because the
next person deletes it.

Timings are the **minimum** of the repeats, not the mean. The minimum is the
one measurement that noise can only move in one direction -- and, since ageing
only ever makes a later repeat slower, it is also the least-aged one, which is
why repeats can share a child process when metrics cannot.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import numpy as np

from scionarena.core.linkstate import LinkState
from scionarena.core.scenario import Scenario, TimelineEvent, TopologySpec
from scionarena.core.segments import SegmentStore
from scionarena.core.tiers import TIERS, Tier
from scionarena.core.tiers import tier as tier_by_name
from scionarena.core.topology import synthetic

BASELINE = Path(__file__).parent / "baseline.json"

#: A regression beyond this fraction on a gated tier fails CI.
#
#: 15% at M1, raised to 25% at M4 once the normaliser's own error was measured
#: rather than assumed. Same machine, same interpreter, same commit, three runs:
#: absolute timings repeated to within 3%, but the calibration came out 9%
#: quicker than when the baseline was recorded, so every expectation shrank by
#: 9% and metrics that had genuinely moved 8-10% were reported at +18-23%. A
#: gate whose normaliser carries ~10% of error cannot resolve 15%. 25% still
#: catches what this gate exists for -- the M3 regression it is modelled on was
#: a hundredfold, not a fifth -- and the per-metric deltas are printed on every
#: run whether they gate or not, so a real 20% is still visible in the log.
TOLERANCE = 0.25

#: Only this tier is gated. The others are recorded so a regression is visible.
GATED_TIER = "realistic"


def interpreter_matches(baseline: dict[str, Any]) -> bool:
    """Is this the interpreter the baseline was recorded on, to the minor version?

    Patch releases are treated as the same interpreter: they do not change the
    bytecode or the evaluation loop in ways that move these numbers, and
    requiring an exact match would mean the gate switched itself off every time
    a runner image picked up 3.11.16.
    """
    recorded = str(baseline.get("recorded_on", {}).get("python", ""))
    return recorded.split(".")[:2] == platform.python_version().split(".")[:2]


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


def scenario_for(name: str, duration_s: float) -> Scenario:
    return Scenario(
        name=f"bench-{name}",
        topology=TopologySpec(tier=name),
        duration_s=duration_s,
        step_s=1.0,
    ).then(
        TimelineEvent(at_s=duration_s / 3, kind="link_degrade", params={"link": 5, "factor": 0.1}),
        TimelineEvent(at_s=duration_s / 2, kind="demand_surge", params={"mbps": 100.0}),
    )


# --------------------------------------------------------------------------
# One measurement each. Every one of these does its own setup, because it runs
# in a process of its own and cannot borrow the previous metric's -- which is
# the point: see ADR 0014.


def m_topology_build(band: Tier, repeats: int) -> dict[str, float]:
    def once() -> float:
        start = time.perf_counter()
        synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)
        return time.perf_counter() - start

    return {"topology_build_s": best_of(repeats, once)}


def m_beaconing_build(band: Tier, repeats: int) -> dict[str, float]:
    topo = synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)

    def once() -> float:
        start = time.perf_counter()
        SegmentStore.for_tier(topo, band, seed=0)
        return time.perf_counter() - start

    return {"beaconing_build_s": best_of(repeats, once)}


def m_path_query_cold(band: Tier, repeats: int) -> dict[str, float]:
    topo = synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)
    scopes = scope_sample(band.n_ases, n=50)

    def once() -> float:
        store = SegmentStore.for_tier(topo, band, seed=0)
        start = time.perf_counter()
        for src, dst in scopes:
            store.paths_for(src, dst)
        return (time.perf_counter() - start) / len(scopes)

    return {"path_query_cold_s": best_of(repeats, once)}


def m_link_metrics_batch(band: Tier, repeats: int) -> dict[str, float]:
    topo = synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)
    store = SegmentStore.for_tier(topo, band, seed=0)
    scopes = scope_sample(band.n_ases, n=50)
    paths = {scope: [p.ifaces for p in store.paths_for(*scope)] for scope in scopes}

    def once() -> float:
        state = LinkState(topo, seed=0)
        start = time.perf_counter()
        for scope in scopes:
            state.path_metrics_batch(paths[scope])
        return (time.perf_counter() - start) / len(scopes)

    return {"link_metrics_batch_s": best_of(repeats, once)}


def m_substrate_step(band: Tier, repeats: int) -> dict[str, float]:
    """The metric the process ageing was corrupting, and the one that matters.

    ``repeats - 1`` because an hour of simulated time at ``realistic`` is the
    most expensive thing the suite does, and the minimum of two is already the
    young measurement.
    """

    def once() -> float:
        world = scenario_for(band.name, 3_600.0).build()
        start = time.perf_counter()
        steps = world.run()
        return (time.perf_counter() - start) / steps

    return {"substrate_step_s": best_of(max(1, repeats - 1), once)}


def m_sizes(band: Tier, repeats: int) -> dict[str, float]:
    """Counts, not timings -- but ``n_segments`` had the same disease.

    A store materialises path sets lazily per scope, so its segment count grows
    as it is queried. The old suite read ``n_segments`` at the end of the tier,
    off a store that the ``link_metrics_batch`` setup had already queried fifty
    times, and recorded 18,327 for what is 15,002 at rest. Nothing said which
    it was, and adding or removing a query anywhere above it moved it.

    Both are worth recording, so both are, under names that say which is which.
    """
    topo = synthetic(n_ases=band.n_ases, n_links=band.n_links, seed=0)
    store = SegmentStore.for_tier(topo, band, seed=0)
    at_rest = store.n_segments
    for src, dst in scope_sample(band.n_ases, n=50):
        store.paths_for(src, dst)
    return {
        "topology_bytes": float(topo.nbytes),
        "link_state_bytes": float(LinkState(topo, seed=0).nbytes),
        "n_segments": float(at_rest),
        "n_segments_after_50_scopes": float(store.n_segments),
    }


#: Measurement name -> what to run. The key is what ``--measure-one`` takes; a
#: measurement may report more than one metric (``sizes`` reports three).
MEASUREMENTS: Final[Mapping[str, Callable[[Tier, int], dict[str, float]]]] = {
    "topology_build": m_topology_build,
    "beaconing_build": m_beaconing_build,
    "path_query_cold": m_path_query_cold,
    "link_metrics_batch": m_link_metrics_batch,
    "substrate_step": m_substrate_step,
    "sizes": m_sizes,
}


def run_isolated(name: str, measurement: str, repeats: int) -> dict[str, float]:
    """Run one measurement in a process this one has not aged.

    A child that fails takes the run down with its stderr attached. The
    alternative -- swallowing it and reporting a zero -- would show up as an
    enormous improvement and pass the gate.
    """
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--measure-one",
        measurement,
        "--tier",
        name,
        "--repeats",
        str(repeats),
    ]
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(
            f"measuring {name}.{measurement} failed (exit {done.returncode}):\n{done.stderr}"
        )
    try:
        parsed = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"measuring {name}.{measurement} printed no measurement:\n{done.stdout}"
        ) from exc
    return {str(k): float(v) for k, v in parsed.items()}


def measure_tier(name: str, *, repeats: int = 3, isolate: bool = True) -> dict[str, float]:
    """Every number in one tier's row of the baseline.

    ``isolate=False`` measures them all here, in this process, in this order --
    which is how the suite worked before ADR 0014, and how it produced a 145%
    regression on unchanged code. It is for a quick local look; the numbers it
    produces are not comparable with a recorded baseline.
    """
    band = tier_by_name(name)
    out: dict[str, float] = {}
    for measurement, fn in MEASUREMENTS.items():
        out.update(run_isolated(name, measurement, repeats) if isolate else fn(band, repeats))
    return out


def measure(tiers: list[str], *, repeats: int = 3, isolate: bool = True) -> dict[str, Any]:
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
        "tiers": {name: measure_tier(name, repeats=repeats, isolate=isolate) for name in tiers},
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
    parser.add_argument(
        "--gate-anyway",
        action="store_true",
        help="gate even if the baseline came from another interpreter (it will lie)",
    )
    parser.add_argument("--json", action="store_true", help="print the measurements as JSON")
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="measure every metric here, in one process, as the suite did before ADR 0014. "
        "Faster, and the numbers are not comparable with a recorded baseline.",
    )
    parser.add_argument(
        "--measure-one",
        choices=sorted(MEASUREMENTS),
        help=argparse.SUPPRESS,  # the child half of --measure-one; not a user-facing knob
    )
    args = parser.parse_args(argv)

    tiers = args.tier or ["dev", "realistic"]

    if args.measure_one:
        # One measurement, one process, one line of JSON on stdout. Nothing else
        # may print here: the parent reads the last line and parses it.
        print(json.dumps(MEASUREMENTS[args.measure_one](tier_by_name(tiers[0]), args.repeats)))
        return 0

    # The calibration runs in this process, which measures nothing else once the
    # metrics moved into children. It has to stay that way -- calibrating in an
    # aged parent would scale every expectation by the ageing it exists to avoid.
    current = measure(tiers, repeats=args.repeats, isolate=not args.in_process)

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

    same_interpreter = interpreter_matches(baseline)
    if not same_interpreter and not args.gate_anyway:
        recorded = baseline.get("recorded_on", {}).get("python", "unknown")
        print(
            f"\nNOT GATED: the baseline was recorded on Python {recorded} and this is "
            f"{platform.python_version()}. The calibration divides out machine speed, "
            f"not an interpreter -- see the module docstring for the measurement. "
            f"The numbers above are printed for comparison and nothing above is trusted. "
            f"Re-record with --update on this interpreter, or force with --gate-anyway."
        )
        return 0

    if gating:
        print(f"\nFAIL: {len(gating)} gated regression(s) beyond {args.tolerance:.0%}")
        return 1
    print(f"\nOK: within {args.tolerance:.0%} on {baseline.get('gated_tier', GATED_TIER)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
