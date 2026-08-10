"""M3: the closed loop end to end.

These are the milestone's own criteria, run small enough to sit in a normal
test run. The headline discrimination is checked at the smoke tier with a
handful of scopes; the realistic-tier evidence is a demo run, recorded in
``docs/milestones/M3.md``, because 100 scopes over 120 rounds is twenty minutes
and does not belong in a pre-commit hook.

The important negative is
``test_peak_dominance_does_not_separate_the_two_models``: it is the finding in
ADR 0010, pinned so that a future change to the detector cannot quietly delete
the reason the criterion was restated.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.core.scenario import Scenario
from scionarena.exposure.budget import Budget
from scionarena.exposure.loop import (
    LoopConfig,
    _params_for,
    _tracked_ifaces,
    busiest_scopes,
    compare,
    run_loop,
)
from scionarena.reference.models import REFERENCE_MODELS

CYCLES = 100
SCOPES = 4


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return Scenario.for_tier("smoke", seed=7)


@pytest.fixture(scope="module")
def scopes(scenario: Scenario) -> list[tuple[str, str]]:
    return busiest_scopes(scenario.build(), SCOPES)


@pytest.fixture(scope="module")
def config() -> LoopConfig:
    return LoopConfig(cycles=CYCLES, n_hosts=200, seed=7)


@pytest.fixture(scope="module")
def runs(scenario: Scenario, scopes: list[tuple[str, str]], config: LoopConfig) -> dict:
    """One run per model, sharing scenario, seed and scopes. Built once."""
    return {
        name: run_loop(REFERENCE_MODELS[name](), scenario, scopes, config=config)
        for name in ("minrtt", "reference")
    }


# --------------------------------------------------------------------------
# the headline


def test_the_greedy_model_shakes_the_network_and_the_stochastic_one_does_not(runs) -> None:
    greedy, stochastic = runs["minrtt"], runs["reference"]
    assert greedy.swing() > 4 * stochastic.swing(), (
        f"link amplitude {greedy.swing():.3f} vs {stochastic.swing():.3f}"
    )


def test_the_greedy_model_throws_its_population_back_and_forth(runs) -> None:
    greedy, stochastic = runs["minrtt"], runs["reference"]
    assert greedy.share_swing() > 4 * stochastic.share_swing(), (
        f"split amplitude {greedy.share_swing():.3f} vs {stochastic.share_swing():.3f}"
    )


def test_oscillation_costs_what_it_is_supposed_to_cost(runs) -> None:
    """The pathology is not aesthetic: the herding model's own objective suffers."""
    assert runs["minrtt"].cost() > 2 * runs["reference"].cost()


def test_peak_dominance_does_not_separate_the_two_models(runs) -> None:
    """ADR 0010's negative finding, at the scale where it was found.

    The proposal's measure was a spectral peak. Under multi-scope contention the
    herding is aperiodic, so the peak is not there to find, and the index ranks
    the two models within noise of each other -- sometimes backwards. Keep this
    test: it is the evidence for restating the criterion.
    """
    greedy, stochastic = runs["minrtt"].oscillation(), runs["reference"].oscillation()
    assert greedy < 2 * stochastic, (
        f"peak dominance separated the models ({greedy:.3f} vs {stochastic:.3f}); "
        "if this is now reliable, revisit the M3 criterion and ADR 0010"
    )


# --------------------------------------------------------------------------
# sampling


def test_every_series_has_one_sample_per_decision_round(runs) -> None:
    """The FFT assumes a uniform grid. If this drifts, the detectors lie."""
    for result in runs.values():
        assert len(result.mean_cost_ms) == CYCLES
        for series in result.advised_load.values():
            assert len(series) == CYCLES
        for series in result.path_share.values():
            assert len(series) == CYCLES


def test_the_world_advances_by_the_decision_cadence(runs, config) -> None:
    for result in runs.values():
        elapsed = result.session_summary["t_s"]
        assert elapsed == pytest.approx(CYCLES * config.decision_s, rel=0.05)


def test_a_turn_that_overruns_its_round_is_counted_not_hidden(runs) -> None:
    """Jitter in the grid is a caveat on the numbers, so it is reported."""
    for result in runs.values():
        assert result.overruns <= CYCLES // 10, "the cadence is too tight for this model"
        assert "overruns" in result.report()


# --------------------------------------------------------------------------
# what the loop measures things on


def test_tracked_interfaces_are_contested_ones(scenario, scopes) -> None:
    """An interface every path of a scope uses cannot respond to an advisory.

    Ranking by raw crossings picks exactly those -- the access link out of the
    source -- and then reports that no model ever changes anything. Named here
    because that bug cost an afternoon.
    """
    world = scenario.build()
    indices = [(world.topology.as_index(a), world.topology.as_index(b)) for a, b in scopes]
    tracked = _tracked_ifaces(world, indices, 8)
    assert tracked
    for iface in tracked:
        users = [
            sum(1 for p in world.paths_for(a, b, limit=50) if iface in p.ifaces[::2])
            for a, b in indices
        ]
        totals = [len(world.paths_for(a, b, limit=50)) for a, b in indices]
        assert any(0 < used < total for used, total in zip(users, totals, strict=True)), (
            f"interface {iface} is used by all or none of every scope's paths"
        )


def test_load_is_sized_against_the_headroom_a_scope_actually_has(scenario, scopes) -> None:
    """Capacities span three orders of magnitude; a fixed Mbps means nothing."""
    world = scenario.build()
    config = LoopConfig(n_hosts=100, target_load=0.9)
    sized = [
        _params_for(
            world,
            world.topology.as_index(a),
            world.topology.as_index(b),
            scenario.hosts,
            config,
        ).offered_mbps
        for a, b in scopes
    ]
    assert all(m > 0 for m in sized)
    assert max(sized) > min(sized), "every scope got the same load, so nothing was derived"


def test_target_load_none_leaves_the_scenario_alone(scenario, scopes) -> None:
    world = scenario.build()
    config = LoopConfig(n_hosts=100, target_load=None)
    src, dst = (world.topology.as_index(x) for x in scopes[0])
    params = _params_for(world, src, dst, scenario.hosts, config)
    assert params.mbps_per_host == scenario.hosts.mbps_per_host


# --------------------------------------------------------------------------
# scopes


def test_every_chosen_scope_has_a_choice_to_make(scenario, scopes) -> None:
    world = scenario.build()
    for a, b in scopes:
        src, dst = world.topology.as_index(a), world.topology.as_index(b)
        assert len(world.paths_for(src, dst, limit=4)) >= 2


def test_the_same_scenario_picks_the_same_scopes(scenario) -> None:
    assert busiest_scopes(scenario.build(), 6) == busiest_scopes(scenario.build(), 6)


def test_a_hundred_scopes_can_be_asked_for(scenario) -> None:
    """The smoke tier is small; asking for more than it has must not hang."""
    assert len(busiest_scopes(scenario.build(), 100)) <= 100


# --------------------------------------------------------------------------
# determinism, budget, reporting


def test_the_whole_loop_is_deterministic_from_the_seed(scenario, scopes, config) -> None:
    digests = [
        run_loop(
            REFERENCE_MODELS["minrtt"](), scenario, scopes, config=LoopConfig(cycles=20, seed=7)
        ).session_summary["digest"]
        for _ in range(2)
    ]
    assert digests[0] == digests[1]


def test_two_seeds_give_two_runs(scenario, scopes) -> None:
    digests = [
        run_loop(
            REFERENCE_MODELS["minrtt"](), scenario, scopes, config=LoopConfig(cycles=20, seed=seed)
        ).session_summary["digest"]
        for seed in (7, 8)
    ]
    assert digests[0] != digests[1]


def test_a_model_that_runs_out_of_budget_still_finishes_the_episode(scenario, scopes) -> None:
    """Invariant 2. Exhaustion is a result, not a crash, and the world runs on."""
    result = run_loop(
        REFERENCE_MODELS["minrtt"](),
        scenario,
        scopes,
        config=LoopConfig(cycles=20, seed=7),
        budget=Budget(calls=25),
    )
    assert len(result.mean_cost_ms) == 20
    assert sum(result.session_summary["errors"].values()) > 0


def test_extra_latency_lands_the_advice_later(scenario, scopes) -> None:
    """The controlled version of "the model was slow", same model and seed."""
    quick, slow = (
        run_loop(
            REFERENCE_MODELS["minrtt"](),
            scenario,
            scopes,
            config=LoopConfig(cycles=30, seed=7, extra_latency_s=extra),
        )
        for extra in (0.0, 10.0)
    )
    assert np.mean(slow.latency_s) > np.mean(quick.latency_s) + 9.0


def test_the_report_card_has_the_numbers_the_milestone_asks_for(runs) -> None:
    report = runs["minrtt"].report()
    for key in ("swing", "share_swing", "oscillation_index", "mean_cost_ms", "digest"):
        assert key in report


def test_compare_ranks_the_worst_first(runs) -> None:
    ranked = compare(list(runs.values()))
    assert ranked[0]["model"] == "MinRTTGreedy"
