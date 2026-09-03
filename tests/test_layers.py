"""R11-R13, the layered reference model, and a demand that says which it is.

Three probes and one contract change from ADR 0023. What each of them is *for*
is written on the probe; what is checked here is that each of them can fail, and
that something ships which does fail it -- invariant 6, which is the only thing
separating a probe from a formality.
"""

from __future__ import annotations

import pytest

from scionarena.backends.analytical import World
from scionarena.conformance.probes import ALL_PROBES, Status
from scionarena.conformance.probes.conformance import (
    R11LayerDiscipline,
    R12IdentityChurnHygiene,
    R13CalibrationUnderShift,
)
from scionarena.conformance.runner import check
from scionarena.exposure.contracts import Advisory, Demand
from scionarena.exposure.session import demand_from_advisory
from scionarena.reference.baselines import LatestSample
from scionarena.reference.layered import LayeredRanker
from scionarena.reference.models import ReferenceStochastic

# --------------------------------------------------------------------------
# intended is not realised


def test_a_demand_without_telemetry_says_so_rather_than_reporting_zero() -> None:
    """``None`` is ignorance and 0.0 is a measurement, and a probe that treated
    them the same would score a model for a path nobody was using."""
    d = Demand(per_path={"a": 0.6, "b": 0.4})
    assert d.realised is None
    assert d.gap() == {}


def test_the_gap_is_what_the_world_did_minus_what_was_asked_for() -> None:
    """The quantity streams.py has always been emphatic about and no probe could
    see: sampling noise, defectors, dwell timers and hysteresis all live here."""
    d = Demand(per_path={"a": 0.5, "b": 0.5}, realised={"a": 0.8, "b": 0.2})
    assert d.gap() == pytest.approx({"a": 0.3, "b": -0.3})
    assert d.intended is d.per_path


def test_a_path_with_no_telemetry_is_absent_from_the_gap_not_zero_in_it() -> None:
    d = Demand(per_path={"a": 0.5, "b": 0.5}, realised={"a": 0.9})
    assert set(d.gap()) == {"a"}


def test_demand_from_an_advisory_defaults_to_intent_and_labels_it() -> None:
    """The old behaviour, unchanged, and now correctly named: a demand built
    from an advisory alone is entirely the model's own intent."""
    d = demand_from_advisory(Advisory(weights={"a": 3.0, "b": 1.0}), n_hosts=10)
    assert d.realised is None
    assert d.normalised() == pytest.approx({"a": 0.75, "b": 0.25})

    with_truth = demand_from_advisory(
        Advisory(weights={"a": 3.0, "b": 1.0}), n_hosts=10, realised={"a": 0.5, "b": 0.5}
    )
    assert with_truth.gap()["a"] == pytest.approx(-0.25)


def test_the_session_can_say_what_traffic_it_actually_saw() -> None:
    from scionarena.core.scenario import Scenario, TopologySpec
    from scionarena.exposure.budget import Budget
    from scionarena.exposure.session import Session

    world = Scenario(name="t", seed=5, topology=TopologySpec(tier="smoke")).build()
    session = Session(world, budget=Budget.unlimited(), seed=1)
    shares = session.realised_shares()
    assert isinstance(shares, dict)
    assert all(v > 0.0 for v in shares.values()), "a path nobody used is absent, not zero"


# --------------------------------------------------------------------------
# R11: the ranking, not the nowcast


def _r11(model, seed: int = 0):
    import random

    world = World(n_ases=8, n_paths=6, seed=seed)
    return R11LayerDiscipline().execute(model, world, random.Random(seed))


def test_a_model_that_ranks_on_congestion_is_failed() -> None:
    """Invariant 6. The probe ships with something that fails it, and this is
    the assertion that keeps that true."""
    outcomes = [_r11(LayeredRanker(discipline=False), seed).status for seed in range(12)]
    assert Status.FAIL in outcomes, outcomes


def test_the_disciplined_variant_holds_its_ranking_on_every_seed() -> None:
    """A probe that also fails the correct model measures nothing but noise."""
    for seed in range(12):
        result = _r11(LayeredRanker(discipline=True), seed)
        assert result.status is not Status.FAIL, (seed, result.finding, result.evidence)


def test_the_probe_refuses_to_grade_an_experiment_it_did_not_stage() -> None:
    """It congests until the world's *own* cheapest path changes, and reports
    NOT_APPLICABLE rather than a pass when nothing short of saturation does."""
    result = _r11(LayeredRanker(discipline=True), 0)
    if result.status is Status.PASS:
        assert result.evidence["was_cheapest"] != result.evidence["now_cheapest"]


def test_a_model_that_declares_no_requirement_class_is_not_applicable() -> None:
    """Empty is not a failure: a model that claims nothing has claimed nothing
    R11 can contradict."""
    assert _r11(ReferenceStochastic(), 0).status is Status.NOT_APPLICABLE


def test_congestion_moves_mass_through_the_temperature_not_per_path() -> None:
    """Names the bug this model shipped with for one afternoon: dividing each
    weight by its own congestion term reordered the published ranking on exactly
    the input R11 tests. The static ranking was untouched and the advisory
    swapped anyway, because a big enough demotion carries a path past its
    neighbour."""
    from scionarena.exposure.contracts import SLA

    def order(advisory: Advisory) -> list[str]:
        weights = advisory.normalised()
        return [k for k, _ in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))]

    world = World(n_ases=8, n_paths=6, seed=4)
    model = LayeredRanker(discipline=True)
    topo = world.snapshot()
    model.reset(topo, seed=1)
    for _ in range(12):
        model.observe(world.observe(demand=world.uniform_demand()), world.snapshot())
        world.step()

    before = model.advise(world.snapshot(), list(world.paths), SLA.presets()["bulk"], 1000)
    world.congest(world.busiest_interfaces(3), background=0.93)
    for _ in range(8):
        model.observe(world.observe(demand=world.uniform_demand()), world.snapshot())
        world.step()
    after = model.advise(world.snapshot(), list(world.paths), SLA.presets()["bulk"], 1000)
    assert order(before) == order(after)


# --------------------------------------------------------------------------
# R12: a grade, never a block


def _r12(model, seed: int = 0):
    import random

    world = World(n_ases=8, n_paths=6, seed=seed)
    return R12IdentityChurnHygiene().execute(model, world, random.Random(seed))


def test_re_signing_changes_the_identifier_and_nothing_else() -> None:
    world = World(n_ases=8, n_paths=6, seed=1)
    before = {p.path_id: p.interfaces for p in world.paths}
    renamed = world.resign(fraction=1.0)
    after = {p.path_id: p.interfaces for p in world.paths}
    assert set(renamed) == set(before)
    assert sorted(after.values()) == sorted(before.values()), "the network must not move"
    assert not set(after) & set(before), "every identifier changed"


def test_a_model_keyed_on_the_identifier_is_graded_not_failed() -> None:
    """Q1 resolved that the deployed fingerprint hashes the interface sequence
    alone, so a model that loses memory across a rename is carrying a hygiene
    defect rather than failing a deployment requirement. R12 must never emit a
    blocking status for it."""
    graded = [_r12(LatestSample(), seed) for seed in range(6)]
    assert any(r.status is Status.WEAK for r in graded), [r.status for r in graded]
    assert all(not r.status.is_blocking for r in graded)


def test_a_model_keyed_on_the_hops_keeps_its_memory() -> None:
    for seed in range(6):
        result = _r12(LayeredRanker(discipline=True), seed)
        assert result.status in (Status.PASS, Status.NOT_APPLICABLE), (seed, result.finding)


# --------------------------------------------------------------------------
# R13: calibration under shift


def _r13(model, seed: int = 0):
    import random

    world = World(n_ases=8, n_paths=6, seed=seed)
    return R13CalibrationUnderShift().execute(model, world, random.Random(seed))


def test_a_point_estimator_has_no_interval_to_lose() -> None:
    assert _r13(LatestSample(), 0).status is Status.DECLARED_ABSENT


def test_a_fixed_width_interval_is_the_thing_this_probe_catches() -> None:
    """Names the finding: before the adaptive level, ReferenceStochastic went
    from 0.67 coverage to 0.47 after a degrade and stayed at 0.47 for the rest of
    the run, while reporting an interval it was not achieving."""

    class Fixed(ReferenceStochastic):
        """The same model with its adaptive level switched off."""

        ETA_ACI = 0.0

    outcomes = [_r13(Fixed(), seed).status for seed in range(6)]
    assert Status.FAIL in outcomes, outcomes


def test_the_adaptive_level_recovers_coverage() -> None:
    outcomes = [_r13(ReferenceStochastic(), seed) for seed in range(6)]
    assert all(r.status is not Status.FAIL for r in outcomes), [
        (r.status, r.evidence) for r in outcomes
    ]


def test_it_is_not_tied_to_the_distributional_flag() -> None:
    """A model with intervals that do not recover has not lied about being
    distributional; it is distributional and badly calibrated, which is a
    different and more interesting finding."""
    assert R13CalibrationUnderShift().capability is None


# --------------------------------------------------------------------------
# the suite


def test_the_three_probes_are_appended_not_inserted() -> None:
    """So an existing report card's column order is unchanged and two cards from
    different versions still line up."""
    ids = [p.probe_id for p in ALL_PROBES]
    assert ids[:10] == [f"R{i}" for i in range(1, 11)]
    assert ids[10:] == ["R11", "R12", "R13"]


def test_the_reference_model_still_passes_everything() -> None:
    """A suite with a probe nothing can pass is as useless as one nothing fails.
    ReferenceStochastic exists to show the interface is satisfiable."""
    assert check(ReferenceStochastic(), seed=3, repeats=2).verdict == "CONFORMANT"


def test_the_matrix_still_discriminates() -> None:
    cards = {
        "disciplined": check(LayeredRanker(True), seed=3, repeats=2),
        "lagged": check(LayeredRanker(False), seed=3, repeats=2),
        "latest": check(LatestSample(), seed=3, repeats=2),
    }
    rows = {name: tuple(r.status.value for r in card.results) for name, card in cards.items()}
    assert len(set(rows.values())) == len(rows), "three models, three different report cards"
