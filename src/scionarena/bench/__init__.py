"""Benchmark front-end: *how does this model score, reproducibly?*

A suite is a matrix of cells -- one model, at one point of the six axes, on one
repeat -- run in a process pool and written one JSON file per cell. Two machines
running the same suite produce the same scores, which is M6's gate and is what
makes the benchmark citable.

    from scionarena.bench import SweepSpec, run_sweep

    spec = SweepSpec(name="standard", models=("mypkg.mine:MyModel",), tier="dev")
    results = run_sweep(spec, Path("results/standard"))

The default sweep varies one axis at a time around a named baseline (ADR 0016);
``mode="grid"`` runs the cartesian product. The five baselines Master Spec §28
makes mandatory -- persistence, Tier-0-only, static-only, latest-sample, EWMA --
are appended to whatever models were asked for, because a score without a floor
under it means nothing.
"""

from .axes import AXES, Axis, AxisValue, baseline_cell, settings_for
from .results import CellResult, cell_id, cell_seed, load_results, write_result
from .sweep import Cell, SweepSpec, plan, run_cell, run_sweep, summarise

__all__ = [
    "AXES",
    "Axis",
    "AxisValue",
    "Cell",
    "CellResult",
    "SweepSpec",
    "baseline_cell",
    "cell_id",
    "cell_seed",
    "load_results",
    "plan",
    "run_cell",
    "run_sweep",
    "settings_for",
    "summarise",
    "write_result",
]
