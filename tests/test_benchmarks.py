"""The benchmark suite's own tests.

The suite is what CI trusts to say whether ``core/`` got slower, so the thing
worth testing is the comparison logic rather than the timings: a gate that
cannot fire, or one that fires on a slower machine, is worse than no gate.
"""

from __future__ import annotations

import importlib.util
import json
import platform
from pathlib import Path
from typing import Any

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "run.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("benchmarks_run", BENCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench() -> Any:
    return load_module()


def measurement(**metrics: float) -> dict[str, Any]:
    return {
        "schema": 2,
        "calibration_s": metrics.pop("calibration_s", 1.0),
        "gated_tier": "realistic",
        "tiers": {"realistic": dict(metrics)},
    }


def test_the_recorded_baseline_is_populated():
    """M1's last acceptance criterion. An empty baseline gates nothing."""
    data = json.loads((BENCH.parent / "baseline.json").read_text(encoding="utf-8"))

    assert data["schema"] == 2
    assert data["calibration_s"] > 0.0
    assert data["tiers"]["realistic"]["substrate_step_s"] > 0.0


def test_the_baseline_records_what_it_was_measured_on():
    """A number without a machine attached cannot be argued with later."""
    data = json.loads((BENCH.parent / "baseline.json").read_text(encoding="utf-8"))

    assert data["recorded_on"]["python"]
    assert data["recorded_on"]["numpy"]


def test_a_regression_beyond_the_tolerance_fails(bench: Any):
    baseline = measurement(substrate_step_s=1.0)
    current = measurement(substrate_step_s=1.30)

    gating, _ = bench.compare(baseline, current, tolerance=0.15)

    assert gating and "substrate_step_s" in gating[0]


def test_a_change_within_the_tolerance_passes(bench: Any):
    baseline = measurement(substrate_step_s=1.0)
    current = measurement(substrate_step_s=1.10)

    gating, everything = bench.compare(baseline, current, tolerance=0.15)

    assert not gating and not everything


def test_getting_faster_never_fails(bench: Any):
    baseline = measurement(substrate_step_s=1.0)
    current = measurement(substrate_step_s=0.2)

    gating, _ = bench.compare(baseline, current, tolerance=0.15)

    assert not gating


def test_a_slower_machine_is_not_a_regression(bench: Any):
    """The gate has to survive being run somewhere other than where the
    baseline was recorded, or it gets muted within a week."""
    baseline = measurement(calibration_s=1.0, substrate_step_s=1.0)
    current = measurement(calibration_s=2.0, substrate_step_s=1.9)

    gating, _ = bench.compare(baseline, current, tolerance=0.15)

    assert not gating


def test_a_real_regression_on_a_slower_machine_still_fails(bench: Any):
    baseline = measurement(calibration_s=1.0, substrate_step_s=1.0)
    current = measurement(calibration_s=2.0, substrate_step_s=3.0)

    gating, _ = bench.compare(baseline, current, tolerance=0.15)

    assert gating


def test_byte_counts_are_not_scaled_by_machine_speed(bench: Any):
    """Memory does not get bigger because the CPU got slower."""
    baseline = measurement(calibration_s=1.0, topology_bytes=1_000.0)
    current = measurement(calibration_s=2.0, topology_bytes=1_400.0)

    gating, _ = bench.compare(baseline, current, tolerance=0.15)

    assert gating and "topology_bytes" in gating[0]


def test_an_ungated_tier_is_reported_and_does_not_fail(bench: Any):
    baseline = {
        "schema": 2,
        "calibration_s": 1.0,
        "gated_tier": "realistic",
        "tiers": {"dev": {"substrate_step_s": 1.0}},
    }
    current = {
        "schema": 2,
        "calibration_s": 1.0,
        "gated_tier": "realistic",
        "tiers": {"dev": {"substrate_step_s": 2.0}},
    }

    gating, everything = bench.compare(baseline, current, tolerance=0.15)

    assert not gating
    assert everything and everything[0].startswith("[not gated]")


def test_a_metric_the_baseline_never_recorded_is_skipped(bench: Any):
    """Adding a measurement should not fail the build that adds it."""
    baseline = measurement(substrate_step_s=1.0)
    current = measurement(substrate_step_s=1.0, brand_new_metric_s=99.0)

    gating, _ = bench.compare(baseline, current, tolerance=0.15)

    assert not gating


def test_the_baseline_in_the_repo_is_gateable_on_the_interpreter_that_recorded_it(bench: Any):
    """Names the bug: CI ran the gate on 3.12 against a 3.11 baseline.

    Machine speed is one scalar and the calibration divides it out. An
    interpreter is not: at numpy 2.4.6 on one machine, 3.12 ran
    ``beaconing_build_s`` 15% slower and ``substrate_step_s`` 52% faster than
    3.11, so three metrics were reported as regressions that no commit caused.
    """
    data = json.loads((BENCH.parent / "baseline.json").read_text(encoding="utf-8"))

    assert bench.interpreter_matches(data) == (
        data["recorded_on"]["python"].split(".")[:2] == platform.python_version().split(".")[:2]
    )


def test_a_patch_release_counts_as_the_same_interpreter(bench: Any):
    """Otherwise a runner image bumping 3.11.15 to 3.11.16 mutes the gate."""
    here = platform.python_version().split(".")
    same_minor = f"{here[0]}.{here[1]}.{int(here[2]) + 7}"

    assert bench.interpreter_matches({"recorded_on": {"python": same_minor}})
    assert not bench.interpreter_matches({"recorded_on": {"python": f"{here[0]}.99.0"}})
    assert not bench.interpreter_matches({})


METRICS = {
    "topology_build_s",
    "beaconing_build_s",
    "path_query_cold_s",
    "link_metrics_batch_s",
    "substrate_step_s",
    "topology_bytes",
    "link_state_bytes",
    "n_segments",
    "n_segments_after_50_scopes",
}


def test_the_suite_runs_end_to_end_at_the_smoke_tier(bench: Any):
    """Cheap proof that every metric the runner claims to measure exists, and
    that the subprocess protocol ADR 0014 introduced actually round-trips."""
    measured = bench.measure_tier("smoke", repeats=1)

    assert set(measured) == METRICS
    assert all(value > 0.0 for value in measured.values())


def test_isolating_a_metric_does_not_change_which_metrics_there_are(bench: Any):
    """``--in-process`` is kept for a quick local look. It has to measure the
    same set of things, or the quick look answers a different question."""
    assert set(bench.measure_tier("smoke", repeats=1, isolate=False)) == METRICS


def test_every_measurement_can_be_asked_for_on_its_own(bench: Any):
    """Names the bug: metrics shared one process, in a fixed order, with
    ``substrate_step_s`` last -- so the gated number was partly a function of
    how much allocation the four benchmarks above it had done. It reported a
    145% regression on a branch that changed no file under ``core/``, and the
    same core measured 3.45 ms isolated at the commit that recorded 2.09 ms.
    """
    assert set(bench.MEASUREMENTS) == {
        "topology_build",
        "beaconing_build",
        "path_query_cold",
        "link_metrics_batch",
        "substrate_step",
        "sizes",
    }
    one = bench.run_isolated("smoke", "sizes", 1)

    assert set(one) == {
        "topology_bytes",
        "link_state_bytes",
        "n_segments",
        "n_segments_after_50_scopes",
    }


def test_a_child_that_fails_takes_the_run_down_with_its_stderr(bench: Any):
    """Swallowing it and reporting a zero would read as an enormous
    improvement and pass the gate."""
    with pytest.raises(RuntimeError) as caught:
        bench.run_isolated("no-such-tier", "sizes", 1)

    assert "sizes" in str(caught.value)


def test_the_segment_count_says_whether_it_was_taken_at_rest(bench: Any):
    """Names the bug: a store materialises path sets lazily, so its count grows
    as it is queried. The baseline recorded 18,327 for the realistic tier, which
    was 15,002 at rest plus whatever fifty scopes of ``link_metrics_batch``
    setup had pulled in -- and nothing said which of the two it was.
    """
    measured = bench.measure_tier("smoke", repeats=1)

    assert measured["n_segments_after_50_scopes"] >= measured["n_segments"]
