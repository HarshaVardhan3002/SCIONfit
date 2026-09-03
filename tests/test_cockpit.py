"""The cockpit: the live channel, the registries, and what a narrowed run omits.

Two invariants meet head-on in this phase and both are asserted here rather than
assumed.

Invariant 3 -- the network does not wait for the model, and must not wait for a
browser either. Every write to the channel drops rather than blocks, and a
dropped frame is marked and never interpolated.

Invariant 1 -- the harness never summarises for the model. Nothing the interface
computes may reach the model, which means a watched run and an unwatched one must
produce the same trace, byte for byte. That is
``test_watching_a_run_does_not_change_it`` and it is the most important test in
this file.
"""

from __future__ import annotations

import queue

import pytest

from scionarena.bench.axes import AXES
from scionarena.bench.sweep import SweepSpec, plan, run_cell
from scionarena.cockpit.app import Cockpit, spec_from
from scionarena.cockpit.panels import catalogue, estimate_s, selection
from scionarena.core.scenario import Scenario, TopologySpec
from scionarena.exposure.loading import load_model
from scionarena.exposure.loop import LoopConfig, busiest_scopes, run_loop
from scionarena.instrument.channel import FrameLog, LiveChannel
from scionarena.instrument.metrics import FAMILIES, REGISTRY


class _Full:
    """A sink that is always full. What a stalled browser looks like."""

    def put_nowait(self, item: object) -> None:
        raise queue.Full


class _Gone:
    """A sink whose reader has died."""

    def put_nowait(self, item: object) -> None:
        raise OSError("handle is closed")


# --------------------------------------------------------------------------
# the channel drops frames rather than time


def test_a_full_channel_drops_the_frame_and_counts_it() -> None:
    """A channel that applied backpressure would change the run it was watching,
    and every number it showed would belong to a different experiment."""
    channel = LiveChannel(_Full())
    for _ in range(5):
        channel.emit("c1", "M", t=1.0, latency_s=0.1)
    assert channel.dropped == 5


def test_a_reader_that_died_is_the_same_decision_as_a_full_one() -> None:
    channel = LiveChannel(_Gone())
    channel.emit("c1", "M", t=1.0, latency_s=0.1)
    assert channel.dropped == 1


def test_an_unwatched_channel_costs_an_attribute_lookup() -> None:
    channel = LiveChannel(None)
    assert not channel.live
    channel.emit("c1", "M", t=1.0, latency_s=0.1)
    assert channel.dropped == 0


def test_sequence_numbers_are_per_cell_and_start_at_zero() -> None:
    """The only thing that lets a reader tell a quiet run from a dropped frame."""
    sink: list[dict[str, object]] = []

    class Sink:
        def put_nowait(self, item: dict[str, object]) -> None:
            sink.append(item)

    channel = LiveChannel(Sink())
    for _ in range(3):
        channel.emit("a", "M", t=0.0, latency_s=0.0)
    channel.emit("b", "M", t=0.0, latency_s=0.0)
    assert [f["seq"] for f in sink] == [0, 1, 2, 0]


def test_a_gap_is_marked_and_never_filled_in() -> None:
    """A smooth line drawn through a hole is a lie about a system whose whole
    subject is instability."""
    log = FrameLog()
    for seq in (0, 1, 5, 6):
        log.queue.put({"cell_id": "a", "seq": seq, "t": float(seq)})
    log.drain()
    rows = list(log)
    assert log.missing == 3
    assert "gap_before" not in rows[1]
    assert rows[2]["gap_before"] == 3
    assert len(rows) == 4, "nothing was invented to fill the hole"


def test_the_reader_pages_forward_without_restarting() -> None:
    log = FrameLog()
    for seq in range(5):
        log.queue.put({"cell_id": "a", "seq": seq})
    log.drain()
    first, rows = log.since(0)
    assert len(rows) == 5
    for seq in range(5, 8):
        log.queue.put({"cell_id": "a", "seq": seq})
    log.drain()
    second, more = log.since(first)
    assert [r["seq"] for r in more] == [5, 6, 7]
    assert second == 8


# --------------------------------------------------------------------------
# watching does not change the run


def test_watching_a_run_does_not_change_it() -> None:
    """Invariant 1 and invariant 4 together, and the reason the channel is
    strictly downstream of the turn. If watching a run could change it, no live
    result would reproduce from its seed."""
    scenario = Scenario(name="t", seed=11, topology=TopologySpec(tier="smoke"))

    def once(watch: bool) -> str:
        world = scenario.build()
        frames: list[object] = []
        result = run_loop(
            load_model("ema"),
            scenario,
            busiest_scopes(world, 2),
            config=LoopConfig(cycles=14, decision_s=30.0, n_hosts=200),
            world=world,
            on_round=frames.append if watch else None,
        )
        if watch:
            assert len(frames) == 14
        return str(result.report()["digest"])

    assert once(watch=True) == once(watch=False)


def test_a_frame_is_three_deltas_aligned() -> None:
    """The lens. Shown, did, world did back -- and nothing derived, averaged or
    rescaled on the way out, because a frame that had been processed would be
    the harness summarising through a different door."""
    scenario = Scenario(name="t", seed=5, topology=TopologySpec(tier="smoke"))
    world = scenario.build()
    frames: list[dict[str, object]] = []
    run_loop(
        load_model("ema"),
        scenario,
        busiest_scopes(world, 2),
        config=LoopConfig(cycles=10, decision_s=30.0, n_hosts=200),
        world=world,
        on_round=frames.append,
    )
    keys = set(frames[-1])
    assert {"records", "calls"} <= keys, "what it was shown"
    assert {"advisories", "max_weight", "weights"} <= keys, "what it did"
    assert {"mean_cost_ms", "best_cost_ms", "deviation"} <= keys, "what the world did back"
    assert all(f["t"] > 0 for f in frames[1:]), "aligned on one timeline"


def test_a_watched_cell_produces_frames_and_the_same_result() -> None:
    spec = SweepSpec(
        name="watched",
        models=("ema",),
        include_baselines=False,
        tier="smoke",
        cycles=10,
        decision_s=10.0,
        scopes=2,
        repeats=1,
        only=("scenario",),
    )
    cell = plan(spec)[0]
    log = FrameLog()
    watched = run_cell(spec, cell, watch=log.queue)
    plain = run_cell(spec, cell)
    log.drain()
    assert watched.error is None, watched.error
    assert len(log) == 10
    assert watched.metrics["digest"] == plain.metrics["digest"]


def test_a_frame_carries_at_most_two_scopes_of_weights() -> None:
    """A realistic-tier round publishes a hundred advisories, and a frame that
    carried them all would make the channel the slowest thing in the loop --
    which is the failure the drop-on-backpressure design exists to avoid,
    arriving through the front door."""
    scenario = Scenario(name="t", seed=5, topology=TopologySpec(tier="smoke"))
    world = scenario.build()
    frames: list[dict[str, object]] = []
    run_loop(
        load_model("ema"),
        scenario,
        busiest_scopes(world, 6),
        config=LoopConfig(cycles=6, decision_s=30.0, n_hosts=200),
        world=world,
        on_round=frames.append,
    )
    assert max(len(f["weights"]) for f in frames) <= 2  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the interface reads the registries


def test_the_catalogue_holds_every_registered_metric_and_no_list_of_its_own() -> None:
    """The hard requirement of the phase: add a metric in code and the interface
    shows it with no interface change."""
    entries = catalogue()
    assert {m["name"] for m in entries["metrics"]} == set(REGISTRY)
    assert entries["families"] == list(FAMILIES)
    assert {a["name"] for a in entries["axes"]} == set(AXES)
    assert entries["probes"], "the probe registry is rendered too"
    assert entries["baselines"], "and the mandatory baselines, which are the floor"


def test_a_new_metric_appears_without_touching_the_interface() -> None:
    from scionarena.instrument.metrics import MetricInput, metric

    before = len(catalogue()["metrics"])
    try:

        @metric("a_temporary_metric", "operational")
        def temporary(data: MetricInput) -> float | None:
            """Registered by a test and removed by it."""
            return 1.0

        names = {m["name"] for m in catalogue()["metrics"]}
        assert "a_temporary_metric" in names
        assert len(names) == before + 1
    finally:
        REGISTRY.pop("a_temporary_metric", None)


def test_every_metric_declares_a_shape_the_interface_can_render() -> None:
    """There is one renderer per shape and no per-metric layout code, so a metric
    that has not declared its shape has nowhere to be drawn."""
    shapes = {m["shape"] for m in catalogue()["metrics"]}
    assert shapes <= {"scalar", "stratified"}


def test_the_stratified_metrics_are_the_ones_that_return_a_mapping() -> None:
    """Declared rather than inferred: a metric returning a mapping on one run and
    a float on the next would leave the interface with one renderer and two
    shapes and nothing saying which."""
    stratified = {n for n, m in REGISTRY.items() if m.shape == "stratified"}
    assert stratified == {
        "pinball",
        "crps",
        "coverage",
        "coverage_gap",
        "interval_width",
        "conformal_drift",
        "n_scored",
    }


def test_a_direction_of_none_is_a_third_direction_not_a_missing_one() -> None:
    """A coverage gap of +0.4 is as wrong as one of -0.4, and a column sorted
    'lower is better' would rank the most over-confident model top."""
    by_name = {m["name"]: m for m in catalogue()["metrics"]}
    assert by_name["coverage_gap"]["direction"] == "closer to zero"
    assert by_name["coverage"]["direction"] == "higher"
    assert by_name["swing"]["direction"] == "lower"


# --------------------------------------------------------------------------
# a partial run says what it did not run


def test_a_narrowed_selection_names_everything_it_left_out() -> None:
    """A narrowed suite that renders like a full one is exactly the dishonesty
    Phase 4 exists to prevent, moved into the interface where it is easier to
    commit and harder to see."""
    chose = selection(families=["accuracy"], axes=["staleness"])
    assert chose["omitted"]["families"] == [f for f in FAMILIES if f != "accuracy"]
    assert "swing" in chose["omitted"]["metrics"]
    assert "scenario" in chose["omitted"]["axes"]


def test_selecting_nothing_selects_everything_and_omits_nothing() -> None:
    chose = selection()
    assert chose["omitted"] == {"families": [], "metrics": [], "axes": []}


def test_an_unknown_family_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown famil"):
        selection(families=["vibes"])


def test_an_unknown_axis_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown ax"):
        selection(axes=["weather"])


def test_the_estimate_is_told_before_the_run_not_after() -> None:
    """Somebody who clicks 'realistic' deserves to know what that costs
    beforehand, and an order of magnitude is enough to decide on."""
    assert estimate_s(10, "smoke") < estimate_s(10, "dev") < estimate_s(10, "realistic")
    assert estimate_s(8, "dev", workers=4) == pytest.approx(estimate_s(8, "dev") / 4)


def test_a_form_produces_a_spec_and_its_omissions_together() -> None:
    spec, chose = spec_from(
        {"models": "ema,gbdt", "tier": "smoke", "cycles": 20, "axes": "scenario"}
    )
    assert spec.models == ("ema", "gbdt")
    assert spec.only == ("scenario",)
    assert "staleness" in chose["omitted"]["axes"]


# --------------------------------------------------------------------------
# the server's own state


def test_an_idle_cockpit_says_idle_rather_than_nothing(tmp_path) -> None:
    cockpit = Cockpit(tmp_path)
    assert cockpit.status()["run"]["state"] == "idle"
    assert cockpit.results() == {"cells": [], "metrics": []}


def test_the_raw_log_is_available_and_unprocessed(tmp_path) -> None:
    """One click away, never the default. A researcher chasing something nobody
    anticipated needs it, and hiding it would be dishonest about what the
    harness holds."""
    cockpit = Cockpit(tmp_path)
    cockpit.frames.queue.put({"cell_id": "a", "seq": 0, "t": 1.0, "mean_cost_ms": 12.5})
    raw = cockpit.raw()
    assert raw["frames"][0]["mean_cost_ms"] == 12.5, "the number as it was, not a rendering"


def test_two_runs_at_once_are_refused_rather_than_interleaved(tmp_path) -> None:
    cockpit = Cockpit(tmp_path)
    spec, _ = spec_from({"models": "ema", "tier": "smoke", "cycles": 6, "repeats": 1})
    cockpit.start(spec)
    with pytest.raises(RuntimeError, match="already going"):
        cockpit.start(spec)


# ------------------------------------------------- what watching a run may cost


def test_a_watched_round_does_not_walk_the_whole_log() -> None:
    """Names the bug: ``_round_frame`` read the call count out of
    ``session.summary()``, which walks the entire log twice. Once per decision
    round that is quadratic in rounds -- measured at 2.5 ms on a 200-record log
    and 42 ms on a 4,000-record one -- so watching a run would eventually cost
    more than running it, which is exactly what invariant 3 forbids and what the
    drop-on-backpressure channel was built to prevent.

    Asserted by making the expensive call fatal rather than by timing it: a
    wall-clock assertion in pytest is the thing ``CLAUDE.md`` says belongs in the
    benchmark suite, and this is a structural claim anyway.
    """
    from scionarena.exposure import session as session_module

    scenario = Scenario(name="w", seed=5, duration_s=400.0, topology=TopologySpec(tier="smoke"))
    world = scenario.build()
    frames: list[dict[str, object]] = []
    calls = {"n": 0}
    real = session_module.Session.summary

    def counted(self: object) -> object:
        calls["n"] += 1
        return real(self)  # type: ignore[arg-type]

    session_module.Session.summary = counted  # type: ignore[method-assign]
    try:
        run_loop(
            load_model("ema"),
            scenario,
            busiest_scopes(world, 2),
            config=LoopConfig(cycles=12, decision_s=10.0),
            world=world,
            on_round=frames.append,
        )
    finally:
        session_module.Session.summary = real  # type: ignore[method-assign]

    assert len(frames) == 12, "the lens still gets a frame per round"
    assert calls["n"] <= 2, (
        f"summary() ran {calls['n']} times for 12 rounds: the per-round frame is "
        "walking the whole log again, which is quadratic in rounds"
    )


def test_the_estimate_scales_with_rounds_and_not_only_with_cells() -> None:
    """Names the bug: ``estimate_s`` ignored ``cycles`` entirely, so it predicted
    about 32 s for a 108-cell 90-round sweep that took roughly 250 s -- wrong by
    8x, and wrong in the reassuring direction for anyone raising the round count,
    which is the one direction a pre-run estimate must not be wrong in."""
    assert estimate_s(10, "smoke", cycles=180) == pytest.approx(
        10 * estimate_s(10, "smoke", cycles=18)
    )
    assert estimate_s(10, "smoke") < estimate_s(10, "dev") < estimate_s(10, "realistic")
    assert estimate_s(8, "dev", workers=4) == pytest.approx(estimate_s(8, "dev") / 4)


def test_the_form_estimate_uses_the_rounds_the_form_asked_for() -> None:
    from scionarena.cockpit.app import plan_for

    few = plan_for({"models": "ema", "tier": "smoke", "cycles": 10, "axes": "scenario"})
    many = plan_for({"models": "ema", "tier": "smoke", "cycles": 200, "axes": "scenario"})
    assert few["cells"] == many["cells"]
    assert many["estimate_s"] > 10 * few["estimate_s"] * 0.9, (
        "twenty times the rounds must not read as the same wait"
    )


def test_the_page_escapes_every_string_a_loaded_model_supplied() -> None:
    """Names the bug: the page built table rows with ``innerHTML`` from
    ``c.label``, ``c.architecture`` and ``m.name`` -- all of which come from
    whoever wrote the model under test. A model whose declared name carried
    markup ran it in the operator's browser. Served to localhost and to the
    person who chose the model, so the blast radius is small; the fix is one
    function, so the radius is not the argument."""
    from scionarena.cockpit.app import _page

    page = _page()
    assert "const esc = " in page, "the page has no escaper"
    for raw in ('"<tr><td>"+c.label+"', "${m.name}", "${m.family}", "+(c.architecture||"):
        assert raw not in page, f"model-supplied string reaches innerHTML unescaped: {raw}"
    for wrapped in ("esc(c.label)", "esc(m.name)", "esc(c.architecture"):
        assert wrapped in page, f"expected {wrapped} in the rendered page"


def test_the_estimate_uses_the_workers_the_run_will_actually_use() -> None:
    """Names the bug: ``plan_for`` estimated on one core while ``Cockpit._go``
    handed the sweep the whole machine. On a 32-core box the console offered
    '11.5 h' for a realistic sweep that would have taken about 22 minutes --
    wrong by 30x, past the order of magnitude the estimate's own docstring claims
    as its bar, and in the direction that talks an operator out of a run."""
    from scionarena.cockpit.app import plan_for
    from scionarena.cockpit.panels import estimate_s, workers_for

    d = plan_for({"models": "ema", "tier": "dev", "cycles": 60, "axes": "scenario"})
    workers = workers_for("dev", d["cells"])
    assert d["workers"] == workers, "the plan does not say what it assumed"
    assert d["estimate_s"] == pytest.approx(
        round(estimate_s(d["cells"], "dev", workers=workers, cycles=60), 1)
    )
    if workers > 1:
        assert d["estimate_s"] < estimate_s(d["cells"], "dev", cycles=60), (
            "the estimate is still priced for a single core"
        )


def test_smoke_stays_serial_and_says_so() -> None:
    """The one tier where the pool costs more than it saves. Asserted so the
    estimate and the run cannot drift apart again."""
    from scionarena.cockpit.panels import workers_for

    assert workers_for("smoke", 400) == 1
    assert workers_for("dev", 400) >= 1
    assert workers_for("dev", 1) == 1, "never more workers than there are cells"
