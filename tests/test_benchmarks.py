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


def test_a_patch_release_counts_as_the_same_numpy(bench: Any):
    """A numpy patch bump should not switch the gate off."""
    here = bench.np.__version__.split(".")
    same_minor = f"{here[0]}.{here[1]}.{int(here[2]) + 7}"

    assert bench.numpy_matches({"recorded_on": {"numpy": same_minor}})
    assert not bench.numpy_matches({"recorded_on": {"numpy": f"{here[0]}.99.0"}})


def test_missing_numpy_metadata_keeps_the_old_gating_behaviour(bench: Any):
    """Older baselines without the numpy field still gate by interpreter."""
    assert bench.numpy_matches({})


def test_platform_family_must_match_to_gate(bench: Any):
    here = platform.platform().split("-", 1)[0]
    other = "Windows" if here != "Windows" else "Linux"

    assert bench.platform_matches({"recorded_on": {"platform": f"{here}-1.2.3"}})
    assert not bench.platform_matches({"recorded_on": {"platform": f"{other}-1.2.3"}})


def test_missing_platform_metadata_keeps_the_old_gating_behaviour(bench: Any):
    assert bench.platform_matches({})


def test_the_suite_runs_end_to_end_at_the_smoke_tier(bench: Any):
    """Cheap proof that every metric the runner claims to measure exists."""
    measured = bench.measure_tier("smoke", repeats=1)

    assert set(measured) == {
        "topology_build_s",
        "beaconing_build_s",
        "path_query_cold_s",
        "link_metrics_batch_s",
        "substrate_step_s",
        "topology_bytes",
        "link_state_bytes",
        "n_segments",
    }
    assert all(value > 0.0 for value in measured.values())
