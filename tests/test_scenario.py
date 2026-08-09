"""The scenario schema and the engine that runs one.

Two tests here are worth more than the rest.
:func:`test_every_tunable_constant_is_serialised` walks the dataclass fields
and fails if a parameter that moves a number is not in the file -- the failure
mode ADR 0007 exists to prevent, where two runs described as identical are not.
:func:`test_a_policy_filter_removes_paths_without_touching_the_graph` is the
corrected domain model made executable: availability changes, connectivity does
not.
"""

from __future__ import annotations

import json
from dataclasses import fields

import numpy as np
import pytest

from scionarena.core.linkstate import BackgroundParams, LinkParams
from scionarena.core.scenario import (
    EVENT_KINDS,
    SCHEMA_VERSION,
    Scenario,
    SearchSpec,
    Substrate,
    TimelineEvent,
    TopologySpec,
)
from scionarena.core.segments import BeaconPolicy


@pytest.fixture
def scenario() -> Scenario:
    return Scenario(name="smoke", topology=TopologySpec(tier="smoke"), duration_s=600.0)


@pytest.fixture
def world(scenario: Scenario) -> Substrate:
    return scenario.build()


def quiet(scenario: Scenario) -> Scenario:
    """No background traffic, so a test can set utilisation exactly."""
    return scenario.with_(background=BackgroundParams(mean_utilisation=0.0, noise_sigma=0.0))


def busiest_scope(world: Substrate) -> tuple[int, int]:
    """The (src, dst) pair with the most paths, so a test has room to lose one."""
    scopes = (
        (len(world.paths_for(src, dst)), src, dst)
        for src in range(world.topology.n_ases)
        for dst in range(world.topology.n_ases)
        if src != dst
    )
    best, src, dst = max(scopes)
    if best < 2:
        raise AssertionError("no scope in this topology resolves to more than one path")
    return src, dst


def a_transit_as(world: Substrate, src: int, dst: int) -> int:
    """An AS some path passes *through*. Filtering src or dst proves nothing."""
    for path in world.paths_for(src, dst):
        for as_ in path.ases[1:-1]:
            return int(as_)
    raise AssertionError(f"every path from {src} to {dst} is a single hop")


# --------------------------------------------------------------------------
# the schema says what ran
# --------------------------------------------------------------------------


def test_round_trip_through_json_preserves_everything(scenario: Scenario):
    enriched = scenario.then(
        TimelineEvent(at_s=60.0, kind="link_degrade", params={"link": 0, "factor": 0.25}),
        TimelineEvent(at_s=120.0, kind="link_restore", note="maintenance ends"),
    )

    assert Scenario.from_json(enriched.to_json()) == enriched


def test_a_saved_scenario_reloads_from_disk(scenario: Scenario, tmp_path):
    path = tmp_path / "scenario.json"
    scenario.save(path)

    assert Scenario.load(path) == scenario


def test_every_tunable_constant_is_serialised(scenario: Scenario):
    """A parameter that moves a number and is not in the file is a silent
    reproducibility hole: two runs differ, both look identical on paper.

    Fails when someone adds a field to LinkParams, BackgroundParams,
    BeaconPolicy or SearchSpec and does not extend the round trip.
    """
    data = scenario.to_dict()

    for section, spec in (
        ("link", LinkParams),
        ("background", BackgroundParams),
        ("beaconing", BeaconPolicy),
        ("search", SearchSpec),
        ("topology", TopologySpec),
    ):
        expected = {f.name for f in fields(spec)}
        assert set(data[section]) == expected, f"{section} lost {expected - set(data[section])}"


def test_defaults_are_written_out_not_omitted(scenario: Scenario):
    """The file says what ran, not what differed from today's defaults."""
    data = json.loads(scenario.to_json())

    assert data["link"]["alpha"] == LinkParams().alpha
    assert data["search"]["core_beam"] == SearchSpec().core_beam


def test_an_unknown_key_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="unknown scenario key"):
        Scenario.from_dict({"name": "typo", "duration": 600.0})


def test_an_unknown_nested_key_is_refused():
    with pytest.raises(ValueError, match="unknown link key"):
        Scenario.from_dict({"link": {"alpha": 0.2, "beta_": 4.0}})


def test_a_scenario_from_a_future_schema_is_refused():
    with pytest.raises(ValueError, match="schema"):
        Scenario(schema=SCHEMA_VERSION + 1)


def test_the_digest_changes_when_a_hidden_constant_changes(scenario: Scenario):
    """Beam width is not something a user would think to write down, and two
    runs that differ in it are not comparable. The digest has to notice."""
    wider = scenario.with_(search=SearchSpec(core_beam=64))

    assert wider.digest() != scenario.digest()


def test_the_digest_is_stable_across_equal_scenarios(scenario: Scenario):
    assert scenario.digest() == Scenario.from_json(scenario.to_json()).digest()


def test_the_digest_ignores_nothing_about_the_timeline(scenario: Scenario):
    a = scenario.then(TimelineEvent(at_s=60.0, kind="link_restore"))
    b = scenario.then(TimelineEvent(at_s=61.0, kind="link_restore"))

    assert a.digest() != b.digest()


# --------------------------------------------------------------------------
# validation happens before the run, not in hour nine
# --------------------------------------------------------------------------


def test_an_unknown_event_kind_is_refused():
    with pytest.raises(ValueError, match="unknown event kind"):
        TimelineEvent(at_s=1.0, kind="link_degrde", params={"link": 0, "factor": 0.5})


def test_a_front_end_may_carry_its_own_event_under_the_x_prefix(scenario: Scenario):
    carried = scenario.then(TimelineEvent(at_s=60.0, kind="x:gym_reset"))
    world = carried.build()
    seen: list[float] = []
    world.clock.on("x:gym_reset", lambda e: seen.append(e.at_s))

    world.run()

    assert seen == [60.0]


def test_a_missing_parameter_is_caught_at_build_not_at_fire(scenario: Scenario):
    broken = scenario.then(TimelineEvent(at_s=300.0, kind="link_degrade", params={"link": 0}))

    with pytest.raises(ValueError, match="missing parameter"):
        broken.build()


def test_an_out_of_range_link_is_caught_at_build(scenario: Scenario):
    broken = scenario.then(
        TimelineEvent(at_s=1.0, kind="link_degrade", params={"link": 10_000, "factor": 0.5})
    )

    with pytest.raises(ValueError, match="names link 10000"):
        broken.build()


def test_an_event_after_the_end_of_the_run_is_refused(scenario: Scenario):
    with pytest.raises(ValueError, match="after the run ends"):
        scenario.then(TimelineEvent(at_s=scenario.duration_s + 1.0, kind="link_restore"))


def test_an_unknown_identity_policy_is_refused():
    with pytest.raises(ValueError, match="identity_policy"):
        Scenario(identity_policy="whatever")


def test_an_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="unknown tier"):
        TopologySpec(tier="enormous")


def test_a_file_backed_source_needs_a_path():
    with pytest.raises(ValueError, match="needs a path"):
        TopologySpec(source="caida")


def test_the_unbuilt_topology_sources_say_so_rather_than_pretending():
    """from_caida and from_dqnsim are the open half of M1 deliverable 1. The
    schema carries them so scenarios written now stay valid."""
    scenario = Scenario(topology=TopologySpec(source="caida", path="as-rel.txt"))

    with pytest.raises(NotImplementedError, match="M1 deliverable 1"):
        scenario.build()


# --------------------------------------------------------------------------
# stepping
# --------------------------------------------------------------------------


def test_a_step_moves_every_part_of_the_substrate_together(world: Substrate):
    """One object owns all four so a step cannot advance three and forget one."""
    world.step()

    assert world.now == pytest.approx(1.0)
    assert world.segments.t == pytest.approx(1.0)
    assert world.links.t == pytest.approx(1.0)


def test_run_reaches_the_scenario_duration(scenario: Scenario):
    world = scenario.with_(step_s=60.0).build()

    steps = world.run()

    assert steps == 10
    assert world.now == pytest.approx(600.0)


def test_run_can_stop_early_and_be_resumed(world: Substrate):
    world.run(until_s=100.0)
    assert world.now == pytest.approx(100.0)

    world.run(until_s=150.0)
    assert world.now == pytest.approx(150.0)


def test_a_step_dispatches_the_events_that_fall_in_it(scenario: Scenario):
    timed = scenario.then(
        TimelineEvent(at_s=0.5, kind="link_restore"),
        TimelineEvent(at_s=1.5, kind="link_restore"),
    )
    world = timed.build()

    assert world.step() == 1
    assert world.step() == 1


# --------------------------------------------------------------------------
# what the events do
# --------------------------------------------------------------------------


def test_a_degradation_lands_when_it_is_scheduled_and_not_before(scenario: Scenario):
    world = (
        quiet(scenario)
        .then(TimelineEvent(at_s=60.0, kind="link_degrade", params={"link": 3, "factor": 0.1}))
        .build()
    )

    world.run(until_s=59.0)
    assert world.links.health[6] == 1.0

    world.run(until_s=61.0)
    assert world.links.health[6] == pytest.approx(0.1)


def test_a_degradation_can_be_one_way(scenario: Scenario):
    world = scenario.then(
        TimelineEvent(
            at_s=10.0, kind="link_degrade", params={"link": 2, "factor": 0.0, "direction": 1}
        )
    ).build()

    world.run(until_s=20.0)

    assert world.links.health[4] == 1.0
    assert world.links.health[5] == 0.0


def test_restore_undoes_a_degradation(scenario: Scenario):
    world = scenario.then(
        TimelineEvent(at_s=10.0, kind="link_degrade", params={"link": 1, "factor": 0.2}),
        TimelineEvent(at_s=20.0, kind="link_restore", params={"link": 1}),
    ).build()

    world.run(until_s=30.0)

    assert world.links.health[2] == 1.0


def test_a_policy_filter_removes_paths_without_touching_the_graph(scenario: Scenario):
    """Availability changes, connectivity does not. The corrected domain model.

    A model that reasons about availability as if it were connectivity is wrong
    here in a way no link metric reveals: nothing physical happened.
    """
    world = scenario.build()
    src, dst = busiest_scope(world)
    before = len(world.paths_for(src, dst))
    graph_before = world.topology.digest()
    via = a_transit_as(world, src, dst)

    filtered = scenario.then(
        TimelineEvent(at_s=30.0, kind="as_policy_filter", params={"as_": via})
    ).build()
    filtered.run(until_s=40.0)

    assert len(filtered.paths_for(src, dst)) < before
    assert filtered.topology.digest() == graph_before
    assert filtered.links.health.min() == 1.0


def test_a_partial_policy_filter_removes_some_and_keeps_others(scenario: Scenario):
    world = scenario.build()
    src, dst = busiest_scope(world)
    via = a_transit_as(world, src, dst)

    partial = scenario.then(
        TimelineEvent(at_s=30.0, kind="as_policy_filter", params={"as_": via, "fraction": 0.5})
    ).build()
    total = scenario.then(
        TimelineEvent(at_s=30.0, kind="as_policy_filter", params={"as_": via})
    ).build()
    partial.run(until_s=40.0)
    total.run(until_s=40.0)

    assert len(total.segments.filtered) >= len(partial.segments.filtered) > 0


def test_unfilter_puts_the_paths_back(scenario: Scenario):
    world = scenario.build()
    src, dst = busiest_scope(world)
    before = len(world.paths_for(src, dst))
    via = a_transit_as(world, src, dst)

    restored = scenario.then(
        TimelineEvent(at_s=30.0, kind="as_policy_filter", params={"as_": via}),
        TimelineEvent(at_s=60.0, kind="as_policy_unfilter"),
    ).build()
    restored.run(until_s=90.0)

    assert len(restored.paths_for(src, dst)) == before


def test_a_demand_surge_raises_utilisation(scenario: Scenario):
    world = (
        quiet(scenario)
        .then(TimelineEvent(at_s=30.0, kind="demand_surge", params={"mbps": 500.0}))
        .build()
    )
    before = float(world.links.utilisation().mean())

    world.run(until_s=40.0)

    assert float(world.links.utilisation().mean()) > before


def test_a_scoped_surge_only_loads_that_scope(scenario: Scenario):
    world = quiet(scenario).build()
    src, dst = busiest_scope(world)
    on_path = set()
    for path in world.paths_for(src, dst):
        on_path.update(path.ifaces)

    surged = (
        quiet(scenario)
        .then(
            TimelineEvent(
                at_s=30.0, kind="demand_surge", params={"mbps": 400.0, "scope": [src, dst]}
            )
        )
        .build()
    )
    surged.run(until_s=40.0)

    loaded = set(np.flatnonzero(surged.links.demand_mbps > 0.0).tolist())
    assert loaded and loaded <= on_path


def test_demand_clear_empties_the_load(scenario: Scenario):
    world = (
        quiet(scenario)
        .then(
            TimelineEvent(at_s=10.0, kind="demand_surge", params={"mbps": 100.0}),
            TimelineEvent(at_s=20.0, kind="demand_clear"),
        )
        .build()
    )

    world.run(until_s=30.0)

    assert float(world.links.demand_mbps.max()) == 0.0


def test_a_scheduled_topology_change_rebuilds_and_bumps_the_generation(scenario: Scenario):
    changed = scenario.then(
        TimelineEvent(at_s=60.0, kind="topology_change", params={"links": [0, 1]}, note="planned")
    ).build()
    before = changed.topology.n_links

    changed.run(until_s=70.0)

    assert changed.topology.n_links == before - 2
    assert changed.generation == 1
    assert changed.segments.t == pytest.approx(70.0)
    assert changed.links.t == pytest.approx(70.0)


def test_a_topology_change_is_the_only_thing_that_moves_the_graph(scenario: Scenario):
    """Everything else leaves the physical graph exactly where it was."""
    busy = quiet(scenario).then(
        TimelineEvent(at_s=10.0, kind="link_degrade", params={"link": 0, "factor": 0.0}),
        TimelineEvent(at_s=20.0, kind="as_policy_filter", params={"as_": 1}),
        TimelineEvent(at_s=30.0, kind="demand_surge", params={"mbps": 200.0}),
    )
    world = busy.build()
    before = world.topology.digest()

    world.run()

    assert world.topology.digest() == before
    assert world.generation == 0


# --------------------------------------------------------------------------
# determinism — invariant 4
# --------------------------------------------------------------------------


def test_the_same_scenario_produces_the_same_digest(scenario: Scenario):
    busy = scenario.then(
        TimelineEvent(at_s=60.0, kind="link_degrade", params={"link": 0, "factor": 0.3}),
        TimelineEvent(at_s=120.0, kind="as_policy_filter", params={"as_": 1}),
        TimelineEvent(at_s=180.0, kind="demand_surge", params={"mbps": 250.0}),
    )

    first = busy.build()
    second = busy.build()
    first.run()
    second.run()

    assert first.digest() == second.digest()


def test_a_different_seed_produces_a_different_digest(scenario: Scenario):
    a = scenario.build()
    b = scenario.with_(seed=1).build()
    a.run()
    b.run()

    assert a.digest() != b.digest()


def test_the_digest_notices_an_event_that_did_not_happen(scenario: Scenario):
    plain = scenario.build()
    degraded = scenario.then(
        TimelineEvent(at_s=60.0, kind="link_degrade", params={"link": 0, "factor": 0.3})
    ).build()
    plain.run()
    degraded.run()

    assert plain.digest() != degraded.digest()


def test_step_size_does_not_change_where_the_world_ends_up(scenario: Scenario):
    """A scenario stepped finely and coarsely is the same world at the end.

    Not a free property: it holds because the substrate is event-driven rather
    than per-tick, and it would break the moment something integrated per step.
    """
    fine = quiet(scenario).with_(step_s=0.5)
    coarse = quiet(scenario).with_(step_s=10.0)
    a, b = fine.build(), coarse.build()
    a.run()
    b.run()

    assert a.links.digest() == b.links.digest()
    assert a.segments.digest() == b.segments.digest()


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------


def test_every_declared_event_kind_has_a_handler(scenario: Scenario):
    """A kind in the schema with nothing behind it would be accepted, recorded
    in the trace, and do nothing."""
    world = scenario.build()

    for kind in EVENT_KINDS:
        assert world.clock._handlers.get(kind), f"{kind} has no handler"


def test_for_tier_names_itself_after_the_tier():
    assert Scenario.for_tier("dev").topology.tier == "dev"
    assert Scenario.for_tier("dev").name == "dev-default"


def test_the_timeline_is_sorted_however_it_was_written(scenario: Scenario):
    out_of_order = scenario.then(
        TimelineEvent(at_s=300.0, kind="link_restore"),
        TimelineEvent(at_s=100.0, kind="demand_clear"),
    )

    assert [e.at_s for e in out_of_order.timeline] == [100.0, 300.0]


def test_path_ids_follow_the_scenarios_identity_policy(scenario: Scenario):
    world = scenario.build()
    src, dst = busiest_scope(world)
    paths = world.paths_for(src, dst)

    structural = world.path_ids(paths)
    crypto = scenario.with_(identity_policy="crypto_bound").build().path_ids(paths)

    assert structural == [p.structural_id for p in paths]
    assert crypto == [p.segment_id for p in paths]
