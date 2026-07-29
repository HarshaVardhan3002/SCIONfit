"""The suite is only worth anything if it separates good models from bad ones."""
import pytest

from scionfit import check
from scionfit.probes.base import Status
from scionfit.reference import (
    CapacityProportional,
    EMAOracle,
    MinRTTGreedy,
    ReferenceStochastic,
)

SEEDS = [0, 11, 1009]


def test_reference_is_conformant():
    card = check(ReferenceStochastic(), seed=0, repeats=3)
    assert card.verdict == "CONFORMANT", card.to_terminal(colour=False)
    assert card.closed_loop_ready


@pytest.mark.parametrize("cls", [EMAOracle, MinRTTGreedy])
def test_greedy_models_are_open_loop_only(cls):
    card = check(cls(), seed=0, repeats=3)
    assert card.verdict == "OPEN-LOOP ONLY"
    assert not card.closed_loop_ready


def test_probes_discriminate():
    """A suite everything passes measures nothing."""
    good = check(ReferenceStochastic(), seed=0, repeats=2)
    bad = check(EMAOracle(), seed=0, repeats=2)
    assert good.score > bad.score + 0.3
    assert good.verdict != bad.verdict


def test_honest_limitation_is_not_punished_as_a_failure():
    card = check(EMAOracle(), seed=0, repeats=1)
    by = {r.probe_id: r.status for r in card.results}
    assert by["R6"] is Status.DECLARED_ABSENT
    assert not card.false_claims


def test_false_claim_is_detected():
    """Claiming a capability you lack is the one thing scored worse than
    lacking it."""
    m = EMAOracle()
    m.capabilities.demand_conditioned = True     # a lie
    card = check(m, seed=0, repeats=1)
    assert card.verdict == "MISDECLARED"
    assert any(r.probe_id == "R6" for r in card.false_claims)


@pytest.mark.parametrize("seed", SEEDS)
def test_reference_stable_across_seeds(seed):
    card = check(ReferenceStochastic(), seed=seed, repeats=1)
    assert card.verdict in ("CONFORMANT", "PARTIAL"), card.to_terminal(colour=False)


def test_capacity_proportional_is_blind_to_telemetry():
    card = check(CapacityProportional(), seed=0, repeats=2)
    by = {r.probe_id: r.status for r in card.results}
    assert by["R1"] is Status.FAIL      # ignores link state entirely
    assert by["R8"] is Status.PASS      # but does spread traffic


def test_reports_render():
    card = check(ReferenceStochastic(), seed=0, repeats=1)
    assert "VERDICT" in card.to_terminal(colour=False)
    assert card.to_json().startswith("{")
    assert card.to_markdown().startswith("# scionfit report")
