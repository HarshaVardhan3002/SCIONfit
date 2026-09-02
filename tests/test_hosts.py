"""M3: the host population, and the arrow from an advisory back into the world.

The claims being tested are physical, not cosmetic. Hosts are counts rather
than objects, so a split is a multinomial draw and concentrates at ``1/sqrt(N)``
-- a test that asserted a fixed tolerance would pass at every N and prove
nothing, so the envelope is computed from the claim itself. An advisory lands
late, because the network does not wait for the model. And a defector
population is not a rounding error: it changes the answer, which is why it
exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.core.hosts import (
    HostParams,
    HostPopulation,
    concentration_bound,
)
from scionarena.core.scenario import Scenario, TopologySpec

# --------------------------------------------------------------------------
# fixtures


def scenario(**changes) -> Scenario:
    fields = {
        "name": "m3",
        "seed": 7,
        "topology": TopologySpec(tier="smoke"),
        "duration_s": 600.0,
        "step_s": 1.0,
    }
    return Scenario(**{**fields, **changes})


def a_scope(world) -> tuple[int, int]:
    """The first (src, dst) in index order with at least three paths."""
    for src in range(world.topology.n_ases):
        for dst in range(world.topology.n_ases):
            if src != dst and len(world.paths_for(src, dst, limit=4)) >= 3:
                return src, dst
    pytest.skip("no multi-path scope in the smoke tier")


# --------------------------------------------------------------------------
# hosts are counts, and counts concentrate


@pytest.mark.parametrize("n_hosts", [100, 1_000, 10_000])
def test_the_realised_split_sits_inside_the_one_over_sqrt_n_envelope(n_hosts: int) -> None:
    """ADR 0009's central claim: sampling N hosts is one multinomial draw.

    If this fails the deviation is not noise, it is a bug in the draw: the
    envelope widens with smaller N precisely so that it cannot be passed by
    being sloppy at 100 and lucky at 10,000.
    """
    world = scenario().build()
    src, dst = a_scope(world)
    world.add_scope(src, dst, params=HostParams(n_hosts=n_hosts, mbps_per_host=1.0))
    state = world.hosts.scope(src, dst)
    assert state is not None
    weights = dict(zip(state.path_ids, [0.5, 0.3, 0.2], strict=False))
    world.publish_advisory(src, dst, weights)
    world.step(5.0)

    bound = concentration_bound(state.intended, n_hosts)
    assert state.deviation() <= bound, (
        f"realised split off by {state.deviation():.4f} at N={n_hosts}, envelope {bound:.4f}"
    )


def test_the_envelope_actually_narrows_with_n() -> None:
    """Guards the guard: an envelope that did not shrink would test nothing."""
    intended = [0.5, 0.5]
    assert concentration_bound(intended, 10_000) < concentration_bound(intended, 100) / 5.0


def test_a_scope_with_no_hosts_offers_nothing() -> None:
    world = scenario().build()
    src, dst = a_scope(world)
    world.add_scope(src, dst, params=HostParams(n_hosts=0))
    world.step(2.0)
    assert float(world.hosts.offered().sum()) == 0.0


# --------------------------------------------------------------------------
# the advisory lands late


def test_an_advisory_lands_at_now_plus_latency_and_not_before() -> None:
    """Invariant 3, made physical. The decision cannot reach its own inputs."""
    world = scenario().build()
    src, dst = a_scope(world)
    world.add_scope(src, dst, params=HostParams(n_hosts=1_000, mbps_per_host=1.0))
    state = world.hosts.scope(src, dst)
    assert state is not None
    before = state.intended.copy()

    lands_at = world.publish_advisory(src, dst, {state.path_ids[0]: 1.0}, latency_s=20.0)
    assert lands_at == pytest.approx(world.now + 20.0)

    world.step(10.0)
    assert np.allclose(state.intended, before), "advisory applied early"
    world.step(15.0)
    assert state.intended[0] == pytest.approx(1.0), "advisory never applied"
    assert world.n_advisories_applied == 1


def test_a_late_advisory_is_applied_to_a_world_that_moved() -> None:
    """The point of the delay, not just the mechanism of it."""
    world = scenario().build()
    src, dst = a_scope(world)
    world.add_scope(src, dst, params=HostParams(n_hosts=500, mbps_per_host=50.0))
    state = world.hosts.scope(src, dst)
    assert state is not None
    world.publish_advisory(src, dst, {state.path_ids[0]: 1.0}, latency_s=60.0)
    at_decision = world.links.cost().copy()
    world.step(70.0)
    assert not np.allclose(at_decision, world.links.cost()), (
        "the world was static across the delay, so this scenario cannot show "
        "the effect of decision latency at all"
    )


# --------------------------------------------------------------------------
# defectors


def test_a_defector_population_changes_the_outcome() -> None:
    """Invariant 6 applied to hosts: a knob that changes nothing measures nothing."""
    results = {}
    for fraction in (0.0, 0.2):
        world = scenario().build()
        src, dst = a_scope(world)
        world.add_scope(
            src,
            dst,
            params=HostParams(n_hosts=1_000, mbps_per_host=20.0, defector_fraction=fraction),
        )
        state = world.hosts.scope(src, dst)
        assert state is not None
        uniform = dict.fromkeys(state.path_ids, 1.0)
        world.publish_advisory(src, dst, uniform)
        world.step(30.0)
        results[fraction] = state.counts.copy()

    assert not np.array_equal(results[0.0], results[0.2])
    # 20% ignoring a uniform advisory and piling onto one path has to show up as
    # a more concentrated split, not merely a different one.
    assert results[0.2].max() > results[0.0].max()


def test_defectors_are_counted_as_they_move() -> None:
    world = scenario().build()
    src, dst = a_scope(world)
    world.add_scope(src, dst, params=HostParams(n_hosts=1_000, defector_fraction=0.2))
    world.step(1.0)
    assert world.hosts.n_defector_moves == 200


# --------------------------------------------------------------------------
# path sets moving under a population


def test_weight_naming_paths_that_are_gone_is_recorded_not_renormalised() -> None:
    """Identity amnesia, made observable rather than silently absorbed."""
    world = scenario().build()
    src, dst = a_scope(world)
    state = world.add_scope(src, dst, params=HostParams(n_hosts=100))
    world.publish_advisory(src, dst, {state.path_ids[0]: 1.0})
    world.step(2.0)

    unknown = max(state.path_ids) + 12345
    world.hosts.publish(src, dst, {unknown: 1.0}, t=world.now)
    assert state.stale_weight == pytest.approx(1.0)
    assert state.intended == pytest.approx(np.full(state.n_paths, 1.0 / state.n_paths))


def test_a_surviving_advisory_carries_over_a_refresh() -> None:
    world = scenario().build()
    src, dst = a_scope(world)
    state = world.add_scope(src, dst, params=HostParams(n_hosts=100))
    world.publish_advisory(src, dst, {state.path_ids[0]: 1.0})
    world.step(2.0)
    world.refresh_scopes()
    after = world.hosts.scope(src, dst)
    assert after is not None
    if state.path_ids[0] in after.path_ids:
        index = after.path_ids.index(state.path_ids[0])
        assert after.intended[index] == pytest.approx(1.0)
        assert after.stale_weight == pytest.approx(0.0)


# --------------------------------------------------------------------------
# resampling


def test_a_population_does_not_all_reconsider_at_once() -> None:
    """``resample_s`` is a rate, not a period: no sawtooth nobody chose."""
    world = scenario(step_s=1.0).build()
    src, dst = a_scope(world)
    state = world.add_scope(
        src, dst, params=HostParams(n_hosts=10_000, resample_s=100.0, mbps_per_host=1.0)
    )
    world.publish_advisory(src, dst, {state.path_ids[0]: 1.0})
    world.step(1.0)
    # Against the live population, not against the hosts already placed: a
    # scope resampling at 1/100 per second has only decided where a hundredth
    # of itself is going after one second, and the rest is not yet sending.
    moved_in_one_step = state.counts[0] / state.n_live
    assert 0.0 < moved_in_one_step < 0.5, "the whole population moved in one step"
    world.step(400.0)
    assert state.counts[0] / state.n_live > 0.9


# --------------------------------------------------------------------------
# determinism


def test_the_same_seed_gives_the_same_hosts() -> None:
    digests = []
    for _ in range(2):
        world = scenario().build()
        src, dst = a_scope(world)
        state = world.add_scope(src, dst, params=HostParams(n_hosts=2_000))
        world.publish_advisory(src, dst, {state.path_ids[0]: 0.7, state.path_ids[1]: 0.3})
        world.step(50.0)
        digests.append(world.hosts.digest())
    assert digests[0] == digests[1]


def test_host_draws_do_not_depend_on_how_the_time_was_stepped() -> None:
    """Invariant 4 against invariant 3.

    A model whose probes cost it three seconds advances the clock in different
    increments from one whose probes are free. If the draw were keyed on a step
    counter, that alone would reroll every host, and two runs of the same model
    would differ because of how expensive its calls happened to be.
    """
    states = []
    for chunks in ([10.0], [1.0] * 10):
        world = scenario().build()
        src, dst = a_scope(world)
        state = world.add_scope(src, dst, params=HostParams(n_hosts=5_000, resample_s=1.0))
        world.publish_advisory(src, dst, {state.path_ids[0]: 0.6, state.path_ids[1]: 0.4})
        for dt in chunks:
            world.step(dt)
        states.append(state.counts.copy())
    assert np.array_equal(states[0], states[1])


def test_two_populations_with_different_seeds_differ() -> None:
    """Determinism is not a constant function."""
    counts = []
    for seed in (1, 2):
        world = scenario(seed=seed).build()
        src, dst = a_scope(world)
        state = world.add_scope(src, dst, params=HostParams(n_hosts=1_000))
        world.step(5.0)
        counts.append(state.counts.copy())
    assert not np.array_equal(counts[0], counts[1])


# --------------------------------------------------------------------------
# parameters


def test_an_sla_mix_that_does_not_sum_to_one_is_normalised() -> None:
    params = HostParams(sla_mix=(("bulk", 2.0), ("interactive", 2.0)))
    assert dict(params.sla_mix) == {"bulk": 0.5, "interactive": 0.5}


@pytest.mark.parametrize(
    "changes",
    [
        {"n_hosts": -1},
        {"mbps_per_host": -1.0},
        {"defector_fraction": 1.5},
        {"defector_kind": "sabotage"},
        {"resample_s": 0.0},
    ],
)
def test_impossible_populations_are_refused(changes: dict) -> None:
    with pytest.raises(ValueError):
        HostParams(**changes)


def test_a_population_reports_what_it_is_offering() -> None:
    world = scenario().build()
    src, dst = a_scope(world)
    world.add_scope(src, dst, params=HostParams(n_hosts=100, mbps_per_host=10.0))
    world.step(2.0)
    summary = world.hosts.summary()
    assert summary["scopes"] == 1
    assert summary["hosts"] == 100
    assert summary["offered_gbps"] > 0.0


def test_a_population_with_no_scopes_is_harmless() -> None:
    world = scenario().build()
    empty = HostPopulation(world.topology, seed=1, params=HostParams())
    assert empty.n_scopes == 0
    assert empty.step(0.0, 1.0, world.links).sum() == 0.0


# --------------------------------------------------------------------------
# the mechanism ladder (ADR 0015)
#
# Every rung defaults to the behaviour that existed before it, so the first
# test here is the one that protects M3's and M4's recorded results: a default
# population must draw exactly what it drew before the knobs existed.


def test_a_population_that_asks_for_no_discipline_is_the_population_we_had() -> None:
    """Names the risk: six new knobs on the object every M3 and M4 number was
    measured against. If a default draw moves, every result in docs/evidence is
    silently invalidated and nothing fails."""
    assert not HostParams().disciplined
    assert HostParams(resample_s=30.0, defector_fraction=0.3).disciplined is False
    assert HostParams(k_paths=2).disciplined
    assert HostParams(timer_jitter=0.5).disciplined


def _placed(n_hosts: int, seconds: float = 200.0, **params) -> tuple:
    """One scope, one advisory over three paths, run to steady state."""
    world = scenario(step_s=1.0).build()
    src, dst = a_scope(world)
    state = world.add_scope(
        src, dst, params=HostParams(n_hosts=n_hosts, mbps_per_host=1.0, **params)
    )
    ids = state.path_ids[:3]
    world.publish_advisory(src, dst, {ids[0]: 0.5, ids[1]: 0.3, ids[2]: 0.2})
    world.step(seconds)
    return world, state, ids


def test_a_selector_that_will_use_one_path_uses_the_best_one() -> None:
    """Paths-per-selector, which ADR 0015 makes the same operation as the
    epsilon-set: a mask on the advisory, renormalised."""
    _, state, ids = _placed(4_000, k_paths=1)
    realised = state.realised()

    assert realised[ids[0]] == pytest.approx(1.0, abs=0.02)
    assert realised.get(ids[2], 0.0) == pytest.approx(0.0, abs=0.01)


def test_the_mass_a_disciplined_selector_refuses_shows_up_as_deviation() -> None:
    """It must not be renormalised out of existence: the report has to be able
    to say that a large deviation was the population's discipline rather than a
    badly behaved model. ADR 0015 rejects truncating the path set for exactly
    this reason."""
    _, state, _ = _placed(4_000, k_paths=1)

    assert state.deviation() == pytest.approx(0.5, abs=0.05), "0.5 of the advisory unused"
    assert state.effective().sum() == pytest.approx(1.0)
    assert state.intended.sum() == pytest.approx(1.0), "the advisory itself is untouched"


def test_an_epsilon_set_drops_the_paths_it_is_told_to() -> None:
    _, state, ids = _placed(4_000, eps_set=0.6)  # keeps >= 0.6 * 0.5 = 0.3
    realised = state.realised()

    assert realised.get(ids[2], 0.0) == pytest.approx(0.0, abs=0.01), "0.2 < 0.3, dropped"
    assert realised[ids[0]] == pytest.approx(0.5 / 0.8, abs=0.03)


def _after_a_swap(hysteresis: float) -> float:
    """Settle on (0.5, 0.3, 0.2), then ask for the top two to trade places.

    The gap a mover has to clear is 0.2, so a margin either side of that
    decides whether the population moves at all.
    """
    world, state, ids = _placed(4_000, hysteresis=hysteresis, resample_s=1.0)
    before = state.counts.copy()
    world.publish_advisory(state.src, state.dst, {ids[0]: 0.3, ids[1]: 0.5, ids[2]: 0.2})
    world.step(100.0)
    return float(np.abs(state.counts - before).sum()) / max(1, int(before.sum()))


def test_hysteresis_holds_a_population_where_it_is() -> None:
    """A margin wider than the gap means nobody clears it, so a population that
    has settled stays settled however often it reconsiders."""
    assert _after_a_swap(0.5) < 0.05, "a margin of 0.5 let a gap of 0.2 move the population"


def test_a_margin_that_can_be_cleared_is_cleared() -> None:
    """The companion to the test above: a hysteresis that blocks everything is
    also satisfied by an implementation that never moves anyone."""
    assert _after_a_swap(0.05) > 0.2


def test_dwell_bars_a_host_that_just_moved_from_moving_again() -> None:
    world, state, ids = _placed(4_000, resample_s=1.0, dwell_s=30.0, seconds=1.0)

    assert state.locked > 0, "the hosts that just moved are not locked"
    assert state.locked <= state.n_live
    world.step(60.0)
    assert state.locked <= state.n_live


def test_a_synchronised_population_moves_all_at_once_and_a_jittered_one_does_not() -> None:
    """``timer_jitter=0`` is a fleet restarted by one deploy. The difference is
    the whole point of the rung: a model stable against jittered selectors and
    unstable against synchronised ones has found a real edge."""
    moved = {}
    for jitter in (0.0, 1.0):
        world = scenario(step_s=1.0).build()
        src, dst = a_scope(world)
        state = world.add_scope(
            src,
            dst,
            params=HostParams(
                n_hosts=4_000, mbps_per_host=1.0, resample_s=50.0, timer_jitter=jitter
            ),
        )
        world.publish_advisory(src, dst, {state.path_ids[0]: 1.0})
        world.step(1.0)
        moved[jitter] = state.counts[0] / max(1, state.n_live)

    assert moved[0.0] == pytest.approx(0.0, abs=0.01), "the shared timer fired early"
    assert 0.0 < moved[1.0] < 0.2, "independent timers move a fraction per step"


def test_a_jittered_mirror_reaches_the_population_over_its_window() -> None:
    """Rung 4. An advisory currently lands on every host in the same instant;
    a real mirror reaches its readers over a window."""
    world = scenario(step_s=1.0).build()
    src, dst = a_scope(world)
    state = world.add_scope(
        src, dst, params=HostParams(n_hosts=8_000, mbps_per_host=1.0, mirror_jitter_s=100.0)
    )
    world.publish_advisory(src, dst, {state.path_ids[0]: 1.0})
    world.step(1.0)
    early = state.counts[0] / max(1, state.n_live)
    assert state.mirror_frac < 0.2
    world.step(150.0)

    assert state.mirror_frac == 1.0
    assert state.counts[0] / max(1, state.n_live) > early + 0.3


def test_the_ladder_is_reproducible_from_the_seed() -> None:
    """Invariant 4. Every rung carries state across steps -- the dwell ledger,
    the mirror -- and carried state is where determinism goes to die."""
    digests = []
    for _ in range(2):
        world, state, _ = _placed(
            2_000,
            k_paths=2,
            hysteresis=0.02,
            dwell_s=20.0,
            timer_jitter=0.3,
            mirror_jitter_s=40.0,
            resample_s=10.0,
            seconds=120.0,
        )
        digests.append(world.hosts.digest())

    assert digests[0] == digests[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"k_paths": 0},
        {"eps_set": 1.5},
        {"hysteresis": -0.1},
        {"dwell_s": -1.0},
        {"timer_jitter": 2.0},
        {"mirror_jitter_s": -1.0},
    ],
)
def test_impossible_discipline_is_refused(changes: dict) -> None:
    with pytest.raises(ValueError):
        HostParams(**changes)
