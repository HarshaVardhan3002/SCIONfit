"""The bad day: the scenario axis, and the family that scores what followed.

Names the bug ADR 0022 exists for. Every metric in the four spec families scores
an episode *as a whole*, and an outage is thirty samples out of six hundred, so a
model that never comes back and a model that comes back in nine seconds landed
within noise of each other on every number the harness produced. The mean
swallowed the difference, and the difference is the question the product is for.

Two things had to be true before that could be measured, and both are checked
here: a fault has to be expressible on an axis that does not know the topology's
link indices, and a metric has to be able to see when the fault fired.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.bench.axes import AXES, baseline_cell, timeline_for
from scionarena.bench.sweep import SweepSpec, plan, run_cell
from scionarena.core.scenario import Disturbance, Scenario, TopologySpec
from scionarena.instrument.metrics import (
    FAMILIES,
    REGISTRY,
    MetricInput,
    compute,
)
from scionarena.instrument.sampler import Series

# --------------------------------------------------------------------------
# a fault an axis can express


def test_a_disturbance_is_expanded_against_the_topology_not_the_tier() -> None:
    """Names the bug the ``Disturbance`` type exists for: ``Tier.n_links`` is a
    target the generator hits within a few, so an axis value naming link indices
    is valid at one tier and refused at another -- and an axis whose meaning
    depends on the tier is not an axis."""
    hit = {}
    for tier in ("smoke", "dev"):
        scenario = Scenario(
            name="t",
            seed=11,
            duration_s=600.0,
            topology=TopologySpec(tier=tier),
            disturbances=(
                Disturbance("link_degrade", at_frac=0.5, fraction=0.1, params={"factor": 0.3}),
            ),
        )
        world = scenario.build()
        events = [e for e in world.timeline if e.kind == "link_degrade"]
        hit[tier] = len(events)
        assert all(0 <= int(e.params["link"]) < world.topology.n_links for e in events)
        assert all(e.at_s == 300.0 for e in events)
    assert hit["dev"] > hit["smoke"], (
        "a tenth of the links has to be a tenth at every tier, or the axis means "
        "'a serious outage' at smoke and 'nothing' at realistic while reading the same"
    )


def test_the_same_seed_gets_the_same_bad_day() -> None:
    """Both halves of a parity pair share a seed and must face the same fault,
    or the pair measures the weather rather than the drive."""

    def links(seed: int) -> list[int]:
        world = Scenario(
            name="t",
            seed=seed,
            duration_s=600.0,
            topology=TopologySpec(tier="smoke"),
            disturbances=(
                Disturbance("link_degrade", at_frac=0.5, fraction=0.2, params={"factor": 0.3}),
            ),
        ).build()
        return [int(e.params["link"]) for e in world.timeline if e.kind == "link_degrade"]

    assert links(4) == links(4)
    assert links(4) != links(5)


def test_a_fraction_that_rounds_to_nothing_schedules_nothing() -> None:
    """Rather than one event, which would make a 0.1% fraction at the smoke tier
    and at the realistic tier the same fault."""
    world = Scenario(
        name="t",
        seed=3,
        duration_s=600.0,
        topology=TopologySpec(tier="smoke"),
        disturbances=(
            Disturbance("link_degrade", at_frac=0.5, fraction=0.001, params={"factor": 0.3}),
        ),
    ).build()
    assert not [e for e in world.timeline if e.kind == "link_degrade"]


def test_an_out_of_range_time_is_refused_at_declaration() -> None:
    with pytest.raises(ValueError, match=r"fraction of the run"):
        Disturbance("link_degrade", at_frac=1.4, fraction=0.1, params={"factor": 0.3})


def test_an_unknown_kind_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown disturbance kind"):
        Disturbance("link_explode", at_frac=0.5)


def test_the_substrate_reports_what_it_installed_not_what_was_written() -> None:
    """``scenario.timeline`` is the intent and ``substrate.timeline`` is the
    fact; a recovery metric needs the second, because a ``Disturbance`` has no
    instant until the topology exists."""
    scenario = Scenario(
        name="t",
        seed=2,
        duration_s=400.0,
        topology=TopologySpec(tier="smoke"),
        disturbances=(Disturbance("demand_surge", at_frac=0.5, params={"mbps": 100.0}),),
    )
    assert scenario.timeline == ()
    assert [e.kind for e in scenario.build().timeline] == ["demand_surge"]


def test_a_scenario_round_trips_through_json_with_its_disturbances() -> None:
    scenario = Scenario(
        name="t",
        seed=2,
        duration_s=400.0,
        topology=TopologySpec(tier="smoke"),
        disturbances=(Disturbance("link_restore", at_frac=0.5, note="repair"),),
    )
    back = Scenario.from_json(scenario.to_json())
    assert back.disturbances == scenario.disturbances
    assert back.digest() == scenario.digest()


# --------------------------------------------------------------------------
# the axis


def test_the_scenario_axis_baseline_is_the_ordinary_flight() -> None:
    """Every cell recorded before this axis existed ran no faults, so its
    baseline value has to be the empty timeline or the baseline cell silently
    stops being the cell everything else is measured against."""
    assert baseline_cell()["scenario"] == "steady"
    assert timeline_for(baseline_cell()) == ()


def test_every_scenario_value_schedules_something_the_substrate_implements() -> None:
    from scionarena.core.scenario import EVENT_KINDS

    for value in AXES["scenario"].values[1:]:
        assert value.disturbances, f"{value.label} stages no fault"
        assert all(d.kind in EVENT_KINDS for d in value.disturbances)


def test_the_axis_value_is_in_the_suite_digest() -> None:
    """Correcting a fault's severity must invalidate the cells recorded under
    the old one rather than silently changing what their scores mean."""
    before = AXES["scenario"].to_dict()
    assert any(v["disturbances"] for v in before["values"])


# --------------------------------------------------------------------------
# the family


def _run(
    cost: list[float], events: list[tuple[float, str]], interval_s: float = 1.0
) -> MetricInput:
    series = Series(interval_s=interval_s)
    series.times = [i * interval_s for i in range(len(cost))]
    series.mean_cost_ms = list(cost)
    return MetricInput(series=series, events=events)


def test_recovery_is_its_own_family_and_nothing_averages_over_it() -> None:
    """A family is the unit the report summarises over, so filing these under
    ``stability`` would produce exactly the aggregate that must not exist."""
    assert FAMILIES[-1] == "recovery"
    names = {n for n, m in REGISTRY.items() if m.family == "recovery"}
    assert names == {
        "recovered",
        "time_to_recover_s",
        "cost_during_recovery",
        "recovered_to",
        "n_faults",
    }


def test_a_run_with_no_fault_scores_nothing_rather_than_zero() -> None:
    out = compute(_run([10.0] * 60, []), families=["recovery"])
    assert out["recovered"] is None
    assert out["time_to_recover_s"] is None
    assert out["n_faults"] == 0.0


def test_a_model_that_comes_back_and_one_that_does_not_are_told_apart() -> None:
    """The whole finding in one assertion: before this family these two series
    differed by nine per cent in mean cost and by nothing else."""
    steady = [10.0] * 30
    came_back = _run(steady + [40.0] * 8 + [10.0] * 30, [(30.0, "link_degrade")])
    stayed_broken = _run(steady + [40.0] * 38, [(30.0, "link_degrade")])

    good = compute(came_back, families=["recovery"])
    bad = compute(stayed_broken, families=["recovery"])
    assert good["recovered"] == 1.0
    assert bad["recovered"] == 0.0
    assert good["time_to_recover_s"] == pytest.approx(8.0)
    assert bad["time_to_recover_s"] is None, (
        "a model that never recovered has no time to report; the pair of metrics "
        "exists so that a None here cannot be read as 'not measured'"
    )


def test_a_fault_that_disturbed_nothing_is_not_a_flawless_recovery() -> None:
    """Names the bug: a degrade that lands on links no driven scope was using is
    a real event that changed nothing, and a recovery search starting at the
    fault would find the still-undisturbed samples and report zero seconds."""
    quiet = _run([10.0] * 30 + [10.0] * 30, [(30.0, "link_degrade")])
    out = compute(quiet, families=["recovery"])
    assert out["recovered"] is None
    assert out["n_faults"] == 1.0, "the event still happened; it simply did not bite"


def test_recovered_to_catches_a_model_that_came_back_worse() -> None:
    """``recovered`` alone would call this a success. The new operating point is
    the number that says the network is permanently forty per cent worse."""
    data = _run([10.0] * 30 + [40.0] * 6 + [14.0] * 30, [(30.0, "link_degrade")])
    out = compute(data, families=["recovery"])
    assert out["recovered_to"] == pytest.approx(1.4, rel=0.05)


def test_cost_during_recovery_charges_the_whole_time_it_stayed_broken() -> None:
    """Otherwise a model that never came back pays for a shorter window than one
    that did, which inverts the metric."""
    fast = _run([10.0] * 30 + [30.0] * 4 + [10.0] * 30, [(30.0, "link_degrade")])
    never = _run([10.0] * 30 + [30.0] * 34, [(30.0, "link_degrade")])
    assert float(compute(never, families=["recovery"])["cost_during_recovery"] or 0.0) > float(
        compute(fast, families=["recovery"])["cost_during_recovery"] or 0.0
    )


def test_one_sample_inside_the_band_is_a_crossing_not_a_recovery() -> None:
    data = _run(
        [10.0] * 30 + [40.0, 40.0, 10.0, 40.0, 40.0, 40.0] + [40.0] * 20,
        [(30.0, "link_degrade")],
    )
    assert compute(data, families=["recovery"])["recovered"] == 0.0


def test_the_band_widens_with_the_pre_fault_noise() -> None:
    """A model whose cost already swings by twenty per cent must not be scored
    as broken for swinging by twenty per cent after the fault."""
    noisy = [10.0 + 3.0 * ((-1) ** i) for i in range(30)]
    data = _run(noisy + [13.0] * 20, [(30.0, "link_degrade")])
    assert compute(data, families=["recovery"])["recovered"] is None


def test_a_fault_too_early_to_have_a_baseline_scores_nothing() -> None:
    data = _run([10.0] * 3 + [40.0] * 40, [(1.0, "link_degrade")])
    assert compute(data, families=["recovery"])["recovered"] is None


def test_an_advisory_reaching_the_hosts_is_not_a_fault() -> None:
    """It is the loop working. Counting it would make the fault instant the
    first decision round of every run ever recorded."""
    data = _run([10.0] * 60, [(5.0, "advisory_apply"), (12.0, "advisory_apply")])
    assert compute(data, families=["recovery"])["n_faults"] == 0.0
    assert compute(data, families=["recovery"])["recovered"] is None


def test_a_nan_hole_in_the_series_is_not_a_recovery() -> None:
    data = _run(
        [10.0] * 30 + [40.0, 40.0] + [float("nan")] * 5 + [40.0] * 20,
        [(30.0, "link_degrade")],
    )
    assert compute(data, families=["recovery"])["recovered"] == 0.0


# --------------------------------------------------------------------------
# through a real cell


@pytest.mark.parametrize("label,expect_fault", [("steady", False), ("brownout", True)])
def test_the_axis_reaches_a_real_run(label: str, expect_fault: bool) -> None:
    """A fault expressed on the axis has to arrive in the substrate, be visible
    to the metric, and produce a number. Each of those three seams has broken
    once."""
    spec = SweepSpec(
        name="rec",
        models=("ema",),
        include_baselines=False,
        tier="smoke",
        cycles=60,
        decision_s=10.0,
        scopes=3,
        repeats=1,
        only=("scenario",),
    )
    cell = next(c for c in plan(spec) if c.axes["scenario"] == label)
    result = run_cell(spec, cell)
    assert result.error is None, result.error
    if expect_fault:
        assert (result.metrics["n_faults"] or 0) > 0
        # Deliberately *not* asserting that the recovery numbers are present.
        # A fault can land on links this cell was not using, and a model already
        # swinging wider than the fault is not disturbed by it; both cases score
        # None on purpose, and a test that demanded a number here would be
        # demanding the metric lie about one of them.
    else:
        assert result.metrics["n_faults"] == 0.0
        assert result.metrics["recovered"] is None


def test_a_permanent_degrade_leaves_a_worse_network_than_no_degrade() -> None:
    """The axis has to bite. An axis value that changes nothing measures the same
    cell twice and reports it as an effect.

    Held on **one world**, which is what a sweep cannot do: a cell's seed is a
    hash of its axes, so the ``steady`` and ``brownout`` cells of one suite run
    different worlds and their difference is a fault plus a seed. Repeats and a
    median are how the sweep handles that; a test asserting the fault bites has
    to remove the seed instead.
    """
    from scionarena.exposure.loading import load_model
    from scionarena.exposure.loop import LoopConfig, busiest_scopes, run_loop

    base = Scenario(
        name="one-world",
        seed=19,
        duration_s=700.0,
        topology=TopologySpec(tier="smoke"),
    )
    hurt = base.with_(
        disturbances=(
            Disturbance("link_degrade", at_frac=0.4, fraction=0.3, params={"factor": 0.6}),
        )
    )
    config = LoopConfig(cycles=60, decision_s=10.0, n_hosts=200, seed=19)
    scopes = busiest_scopes(base.build(), 3)

    calm = run_loop(load_model("ema"), base, scopes, config=config, world=base.build())
    hit = run_loop(load_model("ema"), hurt, scopes, config=config, world=hurt.build())
    assert hit.cost() > calm.cost(), (hit.cost(), calm.cost())


def test_the_series_and_the_events_are_the_only_inputs() -> None:
    """``instrument`` may not import ``exposure``, so a recovery metric has to be
    computable from a result read back off disk months later."""
    data = _run([10.0] * 30 + [40.0] * 6 + [10.0] * 20, [(30.0, "link_degrade")])
    assert np.isfinite(float(compute(data, families=["recovery"])["cost_during_recovery"] or 0.0))


# --------------------------------------------------------------- the draw itself


def test_two_disturbances_that_differ_only_in_params_pick_different_links() -> None:
    """Names the bug: ``expand`` seeded its draw on ``len(self.kind)``, so a
    scenario saying "degrade a tenth mildly and a tenth hard, together" degraded
    the *same* tenth twice and left the other nine tenths untouched -- while the
    result file, which records only the two disturbances, read as a fifth."""
    scenario = Scenario(
        name="t",
        seed=7,
        duration_s=600.0,
        topology=TopologySpec(tier="dev"),
        disturbances=(
            Disturbance("link_degrade", at_frac=0.5, fraction=0.1, params={"factor": 0.8}),
            Disturbance("link_degrade", at_frac=0.5, fraction=0.1, params={"factor": 0.2}),
        ),
    )
    world = scenario.build()
    mild = {int(e.params["link"]) for e in world.timeline if e.params.get("factor") == 0.8}
    hard = {int(e.params["link"]) for e in world.timeline if e.params.get("factor") == 0.2}
    assert mild and hard
    assert mild != hard, "identical draws: the second disturbance is a no-op dressed as a fault"
    assert len(mild | hard) > len(mild), "two tenths must reach more links than one"


def test_a_negative_seed_is_refused_by_name_and_not_by_numpy() -> None:
    """Names the crash: a negative seed reached ``np.random.default_rng`` in
    three unrelated places and each raised ``ValueError: expected non-negative
    integer`` from inside numpy -- naming neither the scenario nor the field,
    and only at build time, long after the operator typed it."""
    with pytest.raises(ValueError, match="seed must not be negative"):
        Scenario(
            name="t",
            seed=-42,
            duration_s=600.0,
            topology=TopologySpec(tier="smoke"),
            disturbances=(
                Disturbance("link_degrade", at_frac=0.5, fraction=0.2, params={"factor": 0.3}),
            ),
        )


def test_the_draw_is_still_the_same_draw_twice() -> None:
    """The seed change must not have cost determinism (invariant 4)."""

    def links() -> list[int]:
        scenario = Scenario(
            name="t",
            seed=3,
            duration_s=600.0,
            topology=TopologySpec(tier="smoke"),
            disturbances=(
                Disturbance("link_degrade", at_frac=0.4, fraction=0.3, params={"factor": 0.5}),
            ),
        )
        return [int(e.params["link"]) for e in scenario.build().timeline]

    assert links() == links()


# ------------------------------------------- what a fault is, and when it fired


def test_one_declared_disturbance_is_one_incident_at_every_tier() -> None:
    """Names the bug: ``n_faults`` counted timeline events, and one declared
    ``Disturbance`` expands to one event per link it drew. The identical
    ``outage`` axis value therefore reported 7 incidents at smoke, 81 at dev and
    1,001 at realistic -- and the support family exists to be divided by, so the
    same declared bad day carried three different confidences."""
    from scionarena.instrument.metrics import MetricInput as MI
    from scionarena.instrument.sampler import Series as S

    outage = next(v for v in AXES["scenario"].values if v.label == "outage").disturbances
    seen = {}
    for tier in ("smoke", "dev"):
        world = Scenario(
            name="n",
            seed=3,
            duration_s=600.0,
            topology=TopologySpec(tier=tier),
            disturbances=outage,
        ).build()
        events = [(e.at_s, e.kind) for e in world.timeline]
        seen[tier] = compute(MI(series=S(interval_s=1.0), events=events), families=["recovery"])[
            "n_faults"
        ]
        assert len(events) > seen[tier], "the fixture must expand to many events per incident"
    assert seen["smoke"] == seen["dev"] == 2.0, seen


def test_the_recovery_clock_starts_at_the_fault_that_did_the_damage() -> None:
    """Names the bug: the window anchored on ``min(fault times)``, so a scenario
    whose first disturbance drew links nothing was using charged the model for
    the quiet interval before the fault that actually hurt. Measured at 13.0 s
    against a true 3.0 s."""
    cost = [10.0] * 25 + [40.0, 40.0, 40.0] + [10.0] * 20
    series = Series(interval_s=1.0)
    series.times = [float(i) for i in range(len(cost))]
    series.mean_cost_ms = cost

    quiet_then_real = compute(
        MetricInput(series=series, events=[(15.0, "link_degrade"), (25.0, "link_degrade")]),
        families=["recovery"],
    )
    real_only = compute(
        MetricInput(series=series, events=[(25.0, "link_degrade")]), families=["recovery"]
    )
    assert real_only["time_to_recover_s"] == pytest.approx(3.0)
    assert quiet_then_real["time_to_recover_s"] == real_only["time_to_recover_s"], (
        "an earlier fault that disturbed nothing is inflating the recovery time"
    )
    assert quiet_then_real["n_faults"] == 2.0, "both incidents are still counted"
