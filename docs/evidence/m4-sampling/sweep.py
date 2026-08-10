"""Reproduces the three tables in ADR 0011. Writes its own log to stdout.

    python docs/evidence/m4-sampling/sweep.py > docs/evidence/m4-sampling/sweep.log

Smoke tier, seed 7, 240 decision rounds, both reference models on one scenario and
one set of scopes -- so anything that differs between two rows is the sampling, and
anything that differs between two models in a row is the model.

Three questions, in order:

1. Does the sample rate change what the detectors report? It should not: the fast
   band is defined in decision rounds and converted per run.
2. What does undersampling do? Measured by decimating a run's own 30 s grid, since
   ``run_loop`` refuses to sample slower than the cadence.
3. Does M3's scope sweep reproduce when the cadence is held at 30 s instead of being
   widened per row until every round fits it?
"""

from __future__ import annotations

from scionarena.core.scenario import Scenario
from scionarena.exposure.loop import LoopConfig, LoopResult, busiest_scopes, run_loop
from scionarena.instrument.detectors import band_min, fast_swing, oscillation_index, warmup_samples
from scionarena.reference.models import REFERENCE_MODELS

TIER, SEED, ROUNDS, HOSTS = "smoke", 7, 240, 200
MODELS = ("minrtt", "reference")

scenario = Scenario.for_tier(TIER, seed=SEED)
built = scenario.build()


def pair(scopes: int, *, cycles: int = ROUNDS, sample_s: float | None = None) -> list[LoopResult]:
    picked = busiest_scopes(built, scopes)
    config = LoopConfig(cycles=cycles, n_hosts=HOSTS, seed=SEED, n_tracked=24, sample_s=sample_s)
    return [run_loop(REFERENCE_MODELS[name](), scenario, picked, config=config) for name in MODELS]


def table(header: str, rows: list[str]) -> None:
    cols = header.count("|") - 1
    print(f"\n{header}\n|" + "---|" * cols)
    for row in rows:
        print(row)


# 1. Sample rate against what is measured. -----------------------------------
rows = []
for sample_s in (None, 3.0, 6.0, 10.0, 15.0, 30.0):
    g, s = pair(8, sample_s=sample_s)
    label = "default (per round)" if sample_s is None else f"{sample_s:g} s"
    rows.append(
        f"| {label} | {len(g.series)} | {'yes' if g.grid_uniform() and s.grid_uniform() else 'NO'} "
        f"| {g.swing():.3f} | {s.swing():.3f} | {g.swing() / s.swing():.2f}x "
        f"| {g.oscillation():.3f} | {s.oscillation():.3f} |"
    )
    print(f"...{label}", flush=True)
table(
    "| sample_s | samples | grid uniform | amplitude, greedy | amplitude, stochastic "
    "| ratio | dominance, greedy | dominance, stochastic |",
    rows,
)

# 2. Undersampling, by decimating the 30 s grid. -----------------------------
kept = pair(8, sample_s=30.0)
rows = []
for stride, label in ((1, "30 s (kept)"), (2, "60 s (decimated)"), (4, "120 s (decimated)")):
    band = {"warmup": warmup_samples(30.0 * stride, 30.0), "f_min": band_min(30.0 * stride, 30.0)}
    cells = []
    for r in kept:
        series = [x[::stride] for x in r.advised_load.values()]
        cells.append(
            (
                max(fast_swing(x, **band) for x in series),
                max(oscillation_index(x, **band) for x in series),
            )
        )
    n = len(next(iter(kept[0].advised_load.values()))[::stride])
    (gs, gd), (ss, sd) = cells
    rows.append(f"| {label} | {n} | {gs:.3f} | {ss:.3f} | {gs / ss:.2f}x | {gd:.3f} | {sd:.3f} |")
table(
    "| effective rate | samples | amplitude, greedy | amplitude, stochastic | ratio "
    "| dominance, greedy | dominance, stochastic |",
    rows,
)

# 3. M3's scope sweep, cadence held. ----------------------------------------
rows = []
for scopes in (4, 8, 16, 24):
    g, s = pair(scopes)
    rows.append(
        f"| {scopes} | {g.cadence_s:g} s | {g.overruns}/{ROUNDS} | {g.swing():.2f} | {s.swing():.2f} "
        f"| {g.swing() / s.swing():.1f}x | {g.oscillation():.3f} | {s.oscillation():.3f} "
        f"| {'yes' if g.grid_uniform() and s.grid_uniform() else 'NO'} |"
    )
    print(f"...{scopes} scopes", flush=True)
table(
    "| scopes | cadence | overruns | amplitude, greedy | amplitude, stochastic | ratio "
    "| dominance, greedy | dominance, stochastic | grid uniform |",
    rows,
)

# The configuration M3 could not keep a grid on. -----------------------------
r = pair(24, cycles=12)[0]
times = r.series.times
gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
print(
    f"\n24 scopes, 12 rounds, held cadence: overruns {r.overruns}/12, "
    f"{times[-1]:.0f} s of world time, mean round {times[-1] / 12:.1f} s, "
    f"{len(times)} samples, gaps {min(gaps):g}-{max(gaps):g} s, "
    f"jitter {r.series.jitter_s():.3g} s, skipped {r.series.skipped}, uniform {r.grid_uniform()}"
)
