"""How a model is driven, and what a cell records about it (ADR 0019).

The bug this file exists to prevent is one that produced no error and no
warning: ``run_loop`` -- the closed loop ``bench`` runs every cell through --
never called ``act``. A model declaring ``uses_tools`` was scored on the fixed
observe/predict/advise cycle instead, silently, and its operational family
reported the decision latency of a ``predict`` call its author never intended
anyone to time. The capability was declared, cross-checked by conformance and
recorded on the cell, and unreachable from the benchmark that read it.

The rest is the confound that remains once the path is reachable: the fixed
cycle's probing policy is *ours*, so a sweep comparing an agent against a
forecaster is comparing two competences against one. The parity pair -- one
model, one world, run both ways -- is the fix, and most of what is checked here
is that the pair really does share a world.
"""

from __future__ import annotations

from typing import Any

import pytest

from scionarena.bench import REFUSED, SweepSpec, cell_id, cell_seed, plan, run_cell
from scionarena.core.scenario import Scenario, TopologySpec
from scionarena.exposure.contracts import Capabilities
from scionarena.exposure.loop import LoopConfig, busiest_scopes, resolve_drive, run_loop
from scionarena.reference.agents import BudgetedProber
from scionarena.reference.baselines import Persistence
from scionarena.reference.models import ReferenceStochastic

TINY = {"tier": "smoke", "cycles": 6, "decision_s": 10.0, "scopes": 3, "repeats": 1}


def smoke_scenario(seed: int = 7) -> Scenario:
    return Scenario(name="t", seed=seed, topology=TopologySpec(tier="smoke"))


class Counting(BudgetedProber):
    """A prober that records having been asked to drive itself."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.acted = 0

    def act(self, session: Any, deadline_s: float) -> None:
        self.acted += 1
        super().act(session, deadline_s)


# --------------------------------------------------------------------------
# the bug


def test_the_closed_loop_reaches_the_agentic_path() -> None:
    """Names the bug: ``run_loop`` had no branch on the drive at all, so every
    ``bench`` cell ever written ran the fixed cycle whatever the model
    declared. ``act`` was reachable only from ``run_episode``, which ``bench``
    does not use -- so a submitted agent was scored on a code path it does not
    implement for, with nothing anywhere saying so."""
    scenario = smoke_scenario()
    scopes = busiest_scopes(scenario.build(), 2)
    model = Counting()
    result = run_loop(
        model,
        scenario,
        scopes,
        config=LoopConfig(cycles=4, decision_s=10.0, n_hosts=20),
        world=scenario.build(),
    )
    assert result.drive == "agentic"
    assert model.acted == 4, "act() was not called once per decision round"


def test_an_agentic_model_still_publishes_and_is_still_timed() -> None:
    """The driver stops calling ``advise`` in agentic mode, so if the model's
    own ``publish_advisory`` did not land in the same place the fixed cycle's
    does, the whole operational family would silently read zero."""
    scenario = smoke_scenario()
    scopes = busiest_scopes(scenario.build(), 2)
    result = run_loop(
        BudgetedProber(),
        scenario,
        scopes,
        config=LoopConfig(cycles=4, decision_s=10.0, n_hosts=20, record_forecasts=True),
        world=scenario.build(),
    )
    assert result.session_summary["advisories"] > 0
    assert any(v > 0 for v in result.latency_s)
    assert result.forecasts, "record_forecasts must still fire in agentic mode"


# --------------------------------------------------------------------------
# refusal, not fallback


def test_agentic_is_refused_rather_than_quietly_downgraded() -> None:
    """A fallback to the fixed cycle here *is* the bug above, respelled: the
    caller asked for one measurement and would have been handed another."""
    with pytest.raises(ValueError, match="no act"):
        resolve_drive(ReferenceStochastic(), "agentic")


def test_auto_believes_the_declaration_and_fixed_overrides_it() -> None:
    assert resolve_drive(BudgetedProber(), "auto") == "agentic"
    assert resolve_drive(BudgetedProber(), "fixed") == "fixed"
    assert resolve_drive(ReferenceStochastic(), "auto") == "fixed"


def test_an_unknown_drive_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="'auto', 'fixed' or 'agentic'"):
        resolve_drive(BudgetedProber(), "agentix")


# --------------------------------------------------------------------------
# the parity pair shares a world


def test_a_parity_pair_is_two_cells_over_one_world() -> None:
    """Names the bug the seed refactor prevents: ``cell_seed`` used to be
    derived from ``cell_id``. Putting the drive in the id -- which it must be,
    or the pair overwrites its own result file -- would then have handed the two
    halves different worlds, and their difference would have measured the seed
    rather than the probing policy."""
    axes = {"defectors": "none"}
    assert cell_id("s", "prober", axes, 0, "fixed") != cell_id("s", "prober", axes, 0, "agentic")
    assert cell_seed("s", "prober", axes, 0) == cell_seed("s", "prober", axes, 0)

    spec = SweepSpec(name="s", models=("prober",), include_baselines=False, drive="both", **TINY)
    cells = plan(spec)
    fixed = [c for c in cells if c.drive == "fixed"]
    agentic = [c for c in cells if c.drive == "agentic"]
    assert len(fixed) == len(agentic) > 0
    for a, b in zip(fixed, agentic, strict=True):
        assert a.identity("s")[0] != b.identity("s")[0], "the pair would overwrite one file"
        assert a.identity("s")[1] == b.identity("s")[1], "the pair must face the same world"


def test_the_suite_digest_separates_the_drives() -> None:
    """Otherwise a resumed sweep reuses the other drive's cells, which is the
    staleness the digest exists to catch."""
    base = {"name": "s", "models": ("prober",), "include_baselines": False, **TINY}
    assert SweepSpec(drive="fixed", **base).digest() != SweepSpec(drive="agentic", **base).digest()


# --------------------------------------------------------------------------
# what the cell records


def test_the_drive_is_recorded_as_resolved_not_as_requested() -> None:
    """A cell that recorded ``auto`` would record the question, not the answer,
    and its operational numbers could not be compared with anything."""
    spec = SweepSpec(name="s", models=("prober",), include_baselines=False, drive="auto", **TINY)
    result = run_cell(spec, plan(spec)[0])
    assert result.error is None, result.error
    assert result.drive == "agentic"


def test_the_architecture_tag_survives_the_trip_to_the_cell() -> None:
    """It is the grouping key the two-level comparison needs, and the model
    object is gone by the time a report is built -- so a tag that is not
    recorded per cell is a tag that does not exist."""
    spec = SweepSpec(name="s", models=("ema",), include_baselines=False, **TINY)
    result = run_cell(spec, plan(spec)[0])
    assert result.capabilities["architecture"] == "ewma"


def test_two_variants_of_one_architecture_share_a_tag() -> None:
    """``LatestSample`` is EWMA with alpha=1 and says so in its own docstring.
    If the tag did not group them the report could not ask the question the
    whole benchmark is for: did the smoothing help, within this architecture."""
    from scionarena.reference.baselines import LatestSample
    from scionarena.reference.models import EMAOracle

    assert LatestSample().capabilities.architecture == EMAOracle().capabilities.architecture


def test_a_model_with_no_agentic_half_is_refused_not_reported_broken() -> None:
    """Names the failure mode: a parity sweep includes the five mandatory
    baselines, none of which can act. Recording those as ordinary errors would
    put five red rows in every report and tell the reader the floor is
    faulty."""
    spec = SweepSpec(name="s", models=("ema",), include_baselines=False, drive="agentic", **TINY)
    result = run_cell(spec, plan(spec)[0])
    assert result.refused
    assert result.error is not None and result.error.startswith(REFUSED)
    assert result.drive == "", "a cell that never ran must not claim to have run either way"


def test_the_refusal_happens_before_a_world_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise ``drive='both'`` pays for a full topology per baseline per cell
    to discover something knowable from the model alone -- at the realistic tier
    that is the whole cost of the sweep, spent on nothing."""

    def explode(self: Scenario) -> None:
        raise AssertionError("the world was built before the drive was resolved")

    monkeypatch.setattr(Scenario, "build", explode)
    spec = SweepSpec(name="s", models=("ema",), include_baselines=False, drive="agentic", **TINY)
    result = run_cell(spec, plan(spec)[0])
    assert result.refused


# --------------------------------------------------------------------------
# what an agent is told


def test_an_agent_can_find_out_which_scopes_it_serves() -> None:
    """Names the hole: the five tools all required a (src, dst) the model had no
    way to learn, so an agent loaded by name from a sweep -- which is the only
    way ``bench`` loads anything -- could not have advised on anything at all.
    ``BudgetedProber`` used to take its scopes as a constructor argument, which
    a bench user cannot supply because they have not seen the world."""
    scenario = smoke_scenario()
    scopes = busiest_scopes(scenario.build(), 2)
    model = BudgetedProber()  # told nothing
    assert model.scopes == []
    run_loop(
        model,
        scenario,
        scopes,
        config=LoopConfig(cycles=2, decision_s=10.0, n_hosts=20),
        world=scenario.build(),
    )
    assert set(model.scopes) >= set(scopes)


def test_listing_the_scopes_is_not_free() -> None:
    """Invariant 2. A call that costs nothing is a call a model may make every
    round without consequence, and then the budget stops meaning anything."""
    from scionarena.exposure.tools import TOOLS

    spec = TOOLS["list_scopes"]
    cost = spec.cost({}, {"n_scopes": 8})
    assert cost.wall_clock_s > 0
    assert cost.nbytes > 0


# --------------------------------------------------------------------------
# the declaration itself


def test_the_architecture_tag_is_not_validated_against_a_list() -> None:
    """Deliberate: an enumeration in the one module every model author imports
    would make adding an architecture a change to the contract. An unknown tag
    groups by itself, which is right for something new."""
    assert Capabilities(name="x", architecture="something-nobody-has-tried").architecture

    assert Persistence().capabilities.architecture == "persistence"
