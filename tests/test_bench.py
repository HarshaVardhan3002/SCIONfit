"""M6: the sweep engine (ADR 0016).

The benchmark's claim is that two machines running the same suite produce the
same scores. Almost everything here is a test of that claim rather than of the
numbers: a cell's identity and seed must be functions of the cell and not of
when or where it ran, a suite that has been edited must not reuse the results of
the suite before the edit, and the five baselines Master Spec 28 makes mandatory
must not be quietly absent from a run that would rather not show a floor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scionarena.bench import (
    AXES,
    CellResult,
    SweepSpec,
    baseline_cell,
    cell_id,
    cell_seed,
    load_results,
    plan,
    run_cell,
    run_sweep,
    settings_for,
    write_result,
)
from scionarena.bench.cli import main as bench_main
from scionarena.bench.score import compliant_share_threshold
from scionarena.reference.baselines import MANDATORY_BASELINES

# A cell small enough to run in a test and large enough to mean something. The
# cycles figure is the one that matters: below about a hundred rounds the
# stability detectors report zero for a model that is visibly flapping.
TINY = {"tier": "smoke", "cycles": 6, "decision_s": 10.0, "scopes": 3, "repeats": 1}


def suite(**changes) -> SweepSpec:
    return SweepSpec(
        **{"name": "t", "models": ("minrtt",), "include_baselines": False, **TINY, **changes}
    )


# --------------------------------------------------------------------------
# a cell is what it is, wherever it ran


def test_a_cell_is_named_and_seeded_by_what_it_is_not_by_when_it_ran() -> None:
    """Names the bug this design avoids: a counter makes a seed a function of
    execution order, and execution order under a process pool is not
    deterministic -- so a cell run alone and the same cell run in the middle of
    a grid would draw different worlds and be reported as the same thing."""
    axes = baseline_cell()
    assert cell_id("s", "minrtt", axes, 0) == cell_id(
        "s", "minrtt", dict(reversed(list(axes.items()))), 0
    )
    assert cell_seed("s", "minrtt", axes, 0) == cell_seed("s", "minrtt", axes, 0)
    assert cell_seed("s", "minrtt", axes, 0) != cell_seed("s", "minrtt", axes, 1)
    assert cell_seed("s", "minrtt", axes, 0) != cell_seed("s", "ema", axes, 0)


def test_a_suite_repeats_by_default_because_one_world_is_not_a_measurement() -> None:
    """Measured, not chosen: ``EMAOracle`` at the dev tier on six seeds of one
    cell returned 2.29 0.32 2.26 0.39 0.30 2.44 -- bimodal, standard deviation
    equal to the mean -- while ``CapacityProportional`` returned 0.06 +- 0.02.
    The suite discriminates on one repeat; the magnitude is not estimable from
    one, and a benchmark meant to be quoted has to report a spread."""
    assert SweepSpec(name="s").repeats >= 3


def test_the_summary_reports_a_range_rather_than_a_mean_of_a_bimodal_thing() -> None:
    """A mean of 2.3 and 0.3 is a number that describes neither run."""
    from scionarena.bench import summarise

    cells = [
        CellResult(
            cell_id=str(i),
            suite="t",
            suite_digest="d",
            model="m",
            label="M",
            mandatory=False,
            axes=baseline_cell(),
            repeat=i,
            seed=i,
            scenario="s",
            metrics={"swing": value},
        )
        for i, value in enumerate((2.3, 0.3, 2.2))
    ]
    row = summarise(cells)[0]

    assert row["n"] == 3
    assert (row["min"], row["max"]) == (0.3, 2.3)
    assert row["swing"] == 2.2, "the median, not the mean"


def test_a_repeat_is_a_different_world_and_not_the_same_one_twice() -> None:
    spec = suite(repeats=2)
    seeds = {cell.identity(spec.name)[1] for cell in plan(spec)}

    assert len(seeds) == len(plan(spec)), "two cells share a seed"


def test_the_same_cell_twice_gives_the_same_numbers() -> None:
    """Invariant 4, at the level the benchmark is quoted at."""
    spec = suite()
    cell = plan(spec)[0]
    first, second = run_cell(spec, cell), run_cell(spec, cell)

    assert first.error is None and second.error is None
    assert first.substrate_digest == second.substrate_digest
    assert first.metrics["digest"] == second.metrics["digest"]


# --------------------------------------------------------------------------
# the matrix


def test_one_axis_at_a_time_is_the_baseline_plus_one_move_per_value() -> None:
    """The grid is 1,458 points before models. A sweep that only offered it
    would be run at the smoke tier and quoted as though it were the real one."""
    cells = plan(suite())
    moved = [c for c in cells if c.axes != baseline_cell()]

    assert len(cells) == 1 + sum(len(a.values) - 1 for a in AXES.values())
    assert all(sum(1 for k in c.axes if c.axes[k] != baseline_cell()[k]) == 1 for c in moved)


def test_a_grid_is_the_cartesian_product_and_says_so() -> None:
    total = 1
    for axis in AXES.values():
        total *= len(axis.values)

    assert len(plan(suite(mode="grid"))) == total


def test_only_one_axis_can_be_asked_for() -> None:
    cells = plan(suite(only=("staleness",)))

    assert len(cells) == len(AXES["staleness"].values)


def test_an_axis_that_does_not_exist_is_refused_with_the_ones_that_do() -> None:
    with pytest.raises(ValueError, match="staleness"):
        suite(only=("stalenes",))


# --------------------------------------------------------------------------
# the floor


def test_the_mandatory_baselines_are_appended_whether_or_not_they_were_asked_for() -> None:
    """Master Spec 28 requires accuracy to be reported against persistence,
    Tier-0-only, static-only, latest-sample and EWMA, and the requirement is
    worth nothing if it can be switched off: a floor that is optional is absent
    on exactly the runs that would rather omit it."""
    models = dict(SweepSpec(name="s", models=("minrtt",)).all_models())

    assert set(MANDATORY_BASELINES) <= set(models)
    assert all(models[spec] for spec in MANDATORY_BASELINES), "baselines are marked as such"
    assert models["minrtt"] is False


def test_a_baseline_named_as_a_user_model_is_not_run_twice() -> None:
    spec = SweepSpec(name="s", models=("scionarena.reference.models:EMAOracle",))
    specs = [m for m, _ in spec.all_models()]

    assert len(specs) == len(set(specs))


@pytest.mark.parametrize("spec_name", sorted(MANDATORY_BASELINES))
def test_every_mandatory_baseline_actually_runs(spec_name: str) -> None:
    """A baseline that raises would leave the floor missing from every report
    while the suite still looked green."""
    spec = suite(models=(spec_name,))
    result = run_cell(spec, plan(spec)[0])

    assert result.error is None, result.metrics.get("traceback", "")
    assert result.metrics["samples"] > 0


# --------------------------------------------------------------------------
# the result files


def test_a_sweep_resumes_where_it_was_interrupted(tmp_path: Path) -> None:
    spec = suite(only=("staleness",))
    cells = plan(spec)
    run_sweep(spec, tmp_path, workers=1, cells=cells[:1])
    assert len(list(tmp_path.glob("*.json"))) == 1

    ran: list[str] = []
    run_sweep(spec, tmp_path, workers=1, on_cell=lambda d, t, i, e: ran.append(i))

    assert len(ran) == len(cells) - 1, "the finished cell was run again"
    assert len(list(tmp_path.glob("*.json"))) == len(cells)


def test_an_edited_suite_does_not_reuse_the_old_suite_results(tmp_path: Path) -> None:
    """Names the failure: a cell file whose numbers were produced under a
    different suite reads as finished, and the report quotes it beside cells
    that were not."""
    first = suite(only=("staleness",), cycles=6)
    run_sweep(first, tmp_path, workers=1)
    assert first.digest() != suite(only=("staleness",), cycles=8).digest()

    ran: list[str] = []
    run_sweep(
        suite(only=("staleness",), cycles=8),
        tmp_path,
        workers=1,
        on_cell=lambda d, t, i, e: ran.append(i),
    )

    assert ran, "every cell was skipped although the suite changed"


def test_a_result_carries_what_would_be_needed_to_reproduce_it(tmp_path: Path) -> None:
    spec = suite(only=("staleness",))
    run_sweep(spec, tmp_path, workers=1, cells=plan(spec)[:1])
    result = next(iter(load_results(tmp_path)))

    assert result.seed and result.substrate_digest and result.suite_digest
    assert result.axes == baseline_cell()
    assert result.metrics["digest"]


def test_a_half_written_file_is_never_read_as_a_finished_cell(tmp_path: Path) -> None:
    """Written through a rename, because a sweep is interrupted by people rather
    than by the scheduler and a truncated file that parses is indistinguishable
    from a finished one."""
    (tmp_path / "torn.json").write_text('{"cell_id": "x", "metr', encoding="utf-8")
    write_result(
        tmp_path,
        CellResult(
            cell_id="good",
            suite="t",
            suite_digest="d",
            model="m",
            label="M",
            mandatory=False,
            axes={},
            repeat=0,
            seed=1,
            scenario="s",
        ),
    )

    assert [r.cell_id for r in load_results(tmp_path)] == ["good"]
    assert not list(tmp_path.glob("*.partial"))


def test_a_cell_that_fails_is_recorded_rather_than_taking_the_sweep_down(tmp_path: Path) -> None:
    """A sweep that died on the first failing model would throw away every cell
    that had already run, and the failure is itself a finding about the model."""
    spec = suite(models=("not.a.module:Nothing",))
    results = run_sweep(spec, tmp_path, workers=1)

    assert len(results) == len(plan(spec))
    assert all(not r.ok for r in results)
    assert "cannot load model" in (results[0].error or "")


# --------------------------------------------------------------------------
# the axes reach the substrate


def test_every_axis_value_lands_somewhere() -> None:
    """An axis value with no payload changes nothing, so a sweep over it
    measures the same cell repeatedly and reports it as an effect."""
    for name, axis in AXES.items():
        for value in axis.values:
            if value is axis.values[0]:
                continue  # the baseline is allowed to be the empty override
            assert value.hosts or value.loop or value.probes, f"{name}={value.label} does nothing"


def test_the_population_axis_reaches_the_driver_and_not_only_the_scenario() -> None:
    """Names the bug: the driver sizes each scope against the spare capacity of
    its best path and overwrites HostParams.n_hosts while doing it, so a
    population axis written onto the scenario was silently ignored."""
    _, loop, _ = settings_for({**baseline_cell(), "population": "10k"})

    assert loop["n_hosts"] == 10_000


def test_the_axes_a_cell_names_are_the_ones_it_runs_under() -> None:
    hosts, loop, probes = settings_for(
        {**baseline_cell(), "discipline": "hysteresis", "paths": "1"}
    )

    assert hosts["hysteresis"] == 0.1 and hosts["k_paths"] == 1
    assert probes == {} and loop["telemetry_delay_s"] == 0.0


# --------------------------------------------------------------------------
# the command line


def test_the_plan_is_free_and_the_axes_are_printable(capsys) -> None:
    assert bench_main(["axes"]) == 0
    assert "baseline" in capsys.readouterr().out

    assert bench_main(["plan", "--models", "minrtt", "--axes", "staleness"]) == 0
    out = capsys.readouterr().out
    assert "cells" in out and "Persistence" in out, "the floor is in the plan too"


def test_a_directory_can_be_read_back(tmp_path: Path, capsys) -> None:
    spec = suite(only=("staleness",))
    run_sweep(spec, tmp_path, workers=1, cells=plan(spec)[:1])

    assert bench_main(["show", str(tmp_path)]) == 0
    assert "MinRTTGreedy" in capsys.readouterr().out

    assert bench_main(["show", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["cell_id"]


def test_reading_an_empty_directory_says_so_rather_than_printing_a_blank_table(
    tmp_path: Path, capsys
) -> None:
    assert bench_main(["show", str(tmp_path)]) == 1
    assert "nothing in" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the metric registry reaches the result files (ADR 0017)


def test_a_bench_cell_records_every_family_and_not_only_the_report_card() -> None:
    """§28 makes accuracy mandatory, so a cell that never asked the model to
    predict anything could not report three of the four families."""
    spec = suite(models=("scionarena.reference.baselines:Tier0Only",), cycles=40)
    result = run_cell(spec, plan(spec)[0])

    assert result.error is None, result.metrics.get("traceback", "")
    assert result.metrics["coverage.h0"] is not None, "the accuracy family is empty"
    assert result.metrics["interval_width.h0"] > 0.0
    assert "swing" in result.metrics and "calls_per_decision" in result.metrics


def test_a_point_estimator_scores_none_on_accuracy_rather_than_zero() -> None:
    """Zero would be a score. The asymmetry between "has no intervals" and
    "has bad intervals" is the whole point of the family."""
    spec = suite(models=("minrtt",), cycles=40)
    result = run_cell(spec, plan(spec)[0])

    assert result.metrics["coverage"] is None
    assert result.metrics["swing"] is not None


def test_the_compliant_share_threshold_is_read_per_regime_and_not_pooled() -> None:
    """A threshold measured with ten thousand mixing hosts says nothing about a
    hundred single-path gateways: those two populations are not running the same
    mechanism, so pooling them averages two different experiments."""

    def cell(population: str, defectors: str, swing: float) -> CellResult:
        return CellResult(
            cell_id=f"{population}{defectors}{swing}",
            suite="t",
            suite_digest="d",
            model="m",
            label="M",
            mandatory=False,
            axes={**baseline_cell(), "population": population, "defectors": defectors},
            repeat=0,
            seed=1,
            scenario="s",
            metrics={"swing": swing},
        )

    rows = compliant_share_threshold(
        [
            cell("1k", "none", 0.2),
            cell("1k", "10pct", 0.4),
            cell("1k", "30pct", 3.0),
            cell("100", "none", 0.1),
            cell("100", "10pct", 0.1),
            cell("100", "30pct", 0.2),
        ],
        metric="swing",
        limit=1.0,
    )
    by_regime = {r.regime: r for r in rows}

    assert len(by_regime) == 2, "the two regimes were pooled into one threshold"
    assert by_regime["population=1k, paths=all"].share == 0.7
    assert by_regime["population=100, paths=all"].held, (
        "it never breached, so there is no threshold"
    )
