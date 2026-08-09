"""The event loop: ordering, determinism, and the shape of invariant 3.

The tests that matter here are the ones about time not running backwards.
:func:`test_scheduling_into_the_past_is_refused` and
:func:`test_a_slow_decision_lands_against_a_world_that_moved` are the two that
would catch a future change quietly reintroducing "pause the world while the
model thinks", which is the bug this module exists to prevent.
"""

from __future__ import annotations

import pytest

from scionarena.core.clock import Clock, Event, Stopwatch


@pytest.fixture
def clock() -> Clock:
    return Clock()


def record(clock: Clock, kind: str) -> list[Event]:
    """Register a handler that keeps every event it saw."""
    seen: list[Event] = []
    clock.on(kind, seen.append)
    return seen


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------


def test_events_run_in_time_order_not_scheduling_order(clock: Clock):
    seen = record(clock, "tick")
    clock.schedule(3.0, "tick", {"i": 3})
    clock.schedule(1.0, "tick", {"i": 1})
    clock.schedule(2.0, "tick", {"i": 2})

    clock.run_until(10.0)

    assert [e.get("i") for e in seen] == [1, 2, 3]


def test_priority_breaks_ties_at_equal_time(clock: Clock):
    """The world updates before anybody observes it, without either knowing."""
    seen: list[str] = []
    clock.on("observe", lambda e: seen.append("observe"))
    clock.on("update", lambda e: seen.append("update"))
    clock.schedule(1.0, "observe", priority=10)
    clock.schedule(1.0, "update", priority=0)

    clock.run_until(1.0)

    assert seen == ["update", "observe"]


def test_equal_time_and_priority_falls_back_to_scheduling_order(clock: Clock):
    seen = record(clock, "tick")
    for i in range(5):
        clock.schedule(1.0, "tick", {"i": i})

    clock.run_until(1.0)

    assert [e.get("i") for e in seen] == [0, 1, 2, 3, 4]


def test_the_clock_stands_on_the_event_it_is_dispatching(clock: Clock):
    seen: list[float] = []
    clock.on("tick", lambda e: seen.append(clock.now))
    clock.schedule(1.5, "tick")
    clock.schedule(4.25, "tick")

    clock.run_until(5.0)

    assert seen == [1.5, 4.25]


def test_time_ends_where_the_step_ended_not_at_the_last_event(clock: Clock):
    """A step with no events still consumed time."""
    clock.schedule(1.0, "tick")
    clock.run_until(7.0)
    assert clock.now == 7.0

    clock.run_until(9.0)
    assert clock.now == 9.0


def test_an_event_exactly_on_the_boundary_runs_in_this_step(clock: Clock):
    seen = record(clock, "tick")
    clock.schedule(1.0, "tick")

    assert clock.run_until(1.0) == 1
    assert len(seen) == 1


def test_a_cascade_completes_within_the_step_that_started_it(clock: Clock):
    """A handler that schedules inside the current window runs now, not next step.

    Deferring it to the next step would be a quiet form of pausing the world:
    the consequence of an event would land later than its own timestamp says.
    """
    seen = record(clock, "hop")

    def bounce(event: Event) -> None:
        n = event.get("n", 0)
        if n < 3:
            clock.schedule(0.1, "hop", {"n": n + 1})

    clock.on("hop", bounce)
    clock.schedule(0.1, "hop", {"n": 0})

    dispatched = clock.run_until(1.0)

    assert dispatched == 4
    assert [e.get("n") for e in seen] == [0, 1, 2, 3]


# --------------------------------------------------------------------------
# time does not run backwards — invariant 3
# --------------------------------------------------------------------------


def test_scheduling_into_the_past_is_refused(clock: Clock):
    clock.run_until(10.0)
    with pytest.raises(ValueError, match="invariant 3"):
        clock.at(9.9, "advisory_applied")


def test_running_backwards_is_refused(clock: Clock):
    clock.run_until(10.0)
    with pytest.raises(ValueError, match="time does not run backwards"):
        clock.run_until(9.0)


def test_advance_to_refuses_to_rewind(clock: Clock):
    clock.advance_to(5.0)
    with pytest.raises(ValueError, match="rewind"):
        clock.advance_to(4.999)


def test_negative_delays_are_refused(clock: Clock):
    with pytest.raises(ValueError, match="negative"):
        clock.schedule(-1.0, "tick")


def test_a_slow_decision_lands_against_a_world_that_moved(clock: Clock):
    """Invariant 3, concretely: 800 ms of thinking means 800 ms of world.

    The advisory is computed from the state at t=1.0 and applied at t=1.8, by
    which time two more world updates have happened. If a future change ever
    makes the harness apply advice against the state it was shown, this test
    fails with the ordering it expected written out.
    """
    order: list[str] = []
    clock.on("world_update", lambda e: order.append(f"world@{clock.now:.1f}"))
    clock.on("advisory_applied", lambda e: order.append(f"advice@{clock.now:.1f}"))
    clock.every(0.5, "world_update")

    def decide(event: Event) -> None:
        thinking = Stopwatch(fixed_s=0.8)
        clock.schedule(thinking.elapsed_s, "advisory_applied", {"decided_at": clock.now})

    clock.on("decide", decide)
    clock.schedule(1.0, "decide")

    clock.run_until(2.0)

    assert order == ["world@0.5", "world@1.0", "world@1.5", "advice@1.8", "world@2.0"]


def test_the_applied_advisory_remembers_when_it_was_decided(clock: Clock):
    seen = record(clock, "advisory_applied")
    clock.at(1.0, "advisory_applied", {"decided_at": 0.2})

    clock.run_until(2.0)

    assert seen[0].at_s - seen[0].get("decided_at") == pytest.approx(0.8)


# --------------------------------------------------------------------------
# repeats and cancellation
# --------------------------------------------------------------------------


def test_a_repeat_fires_on_its_interval(clock: Clock):
    seen = record(clock, "beacon")
    clock.every(30.0, "beacon")

    clock.run_until(100.0)

    assert [e.at_s for e in seen] == [30.0, 60.0, 90.0]


def test_a_repeat_can_be_given_a_first_firing(clock: Clock):
    seen = record(clock, "beacon")
    clock.every(30.0, "beacon", start_s=0.0)

    clock.run_until(60.0)

    assert [e.at_s for e in seen] == [0.0, 30.0, 60.0]


def test_cancelling_a_repeat_stops_the_series_not_one_occurrence(clock: Clock):
    seen = record(clock, "beacon")
    handle = clock.every(30.0, "beacon")

    clock.run_until(60.0)
    clock.cancel(handle)
    clock.run_until(300.0)

    assert [e.at_s for e in seen] == [30.0, 60.0]


def test_a_handler_can_cancel_its_own_series(clock: Clock):
    seen = record(clock, "beacon")
    handle = clock.every(1.0, "beacon")
    clock.on("beacon", lambda e: clock.cancel(handle) if e.at_s >= 3.0 else None)

    clock.run_until(10.0)

    assert [e.at_s for e in seen] == [1.0, 2.0, 3.0]


def test_cancelling_twice_is_not_an_error(clock: Clock):
    handle = clock.schedule(1.0, "tick")
    clock.cancel(handle)
    clock.cancel(handle)

    assert clock.run_until(10.0) == 0


def test_a_cancelled_event_does_not_count_as_pending(clock: Clock):
    clock.schedule(1.0, "tick")
    handle = clock.schedule(2.0, "tick")
    assert clock.pending == 2

    clock.cancel(handle)

    assert clock.pending == 1


def test_a_zero_interval_repeat_is_refused(clock: Clock):
    """It would fill the queue at one instant and never advance time."""
    with pytest.raises(ValueError, match="positive"):
        clock.every(0.0, "spin")


def test_run_all_is_bounded_against_a_repeat(clock: Clock):
    clock.every(1.0, "beacon")
    assert clock.run_all(max_events=10) == 10


def test_max_events_stops_a_step_early_and_leaves_the_rest_queued(clock: Clock):
    for i in range(5):
        clock.schedule(float(i + 1), "tick", {"i": i})

    assert clock.run_until(10.0, max_events=2) == 2
    assert clock.pending == 3


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------


def test_an_event_nobody_handles_is_not_an_error(clock: Clock):
    """A scenario may carry events only one front-end cares about."""
    clock.schedule(1.0, "gym_only")

    assert clock.run_until(2.0) == 1


def test_handlers_run_in_registration_order(clock: Clock):
    seen: list[str] = []
    clock.on("tick", lambda e: seen.append("first"))
    clock.on("tick", lambda e: seen.append("second"))
    clock.schedule(1.0, "tick")

    clock.run_until(1.0)

    assert seen == ["first", "second"]


def test_off_removes_one_handler_and_leaves_the_others(clock: Clock):
    seen: list[str] = []

    def doomed(event: Event) -> None:
        seen.append("doomed")

    clock.on("tick", doomed)
    clock.on("tick", lambda e: seen.append("kept"))
    clock.off("tick", doomed)
    clock.schedule(1.0, "tick")

    clock.run_until(1.0)

    assert seen == ["kept"]


def test_peek_shows_the_next_event_without_running_it(clock: Clock):
    seen = record(clock, "tick")
    clock.schedule(2.0, "tick")
    clock.schedule(1.0, "tick", {"i": 1})

    event = clock.peek()

    assert event is not None and event.get("i") == 1
    assert clock.next_time() == 1.0
    assert seen == []
    assert clock.now == 0.0


def test_run_next_dispatches_exactly_one(clock: Clock):
    clock.schedule(1.0, "tick")
    clock.schedule(2.0, "tick")

    event = clock.run_next()

    assert event is not None and event.at_s == 1.0
    assert clock.now == 1.0
    assert clock.pending == 1


def test_run_next_on_an_idle_clock_returns_none(clock: Clock):
    assert clock.run_next() is None


def test_upcoming_lists_live_events_in_order(clock: Clock):
    clock.schedule(3.0, "tick", {"i": 3})
    handle = clock.schedule(2.0, "tick", {"i": 2})
    clock.schedule(1.0, "tick", {"i": 1})
    clock.cancel(handle)

    assert [e.get("i") for e in clock.upcoming()] == [1, 3]


# --------------------------------------------------------------------------
# determinism — invariant 4
# --------------------------------------------------------------------------


def build(seed_events: list[tuple[float, str, int]]) -> Clock:
    clock = Clock()
    clock.every(30.0, "beacon")
    for at_s, kind, i in seed_events:
        clock.at(at_s, kind, {"i": i})
    clock.run_until(100.0)
    return clock


def test_the_same_schedule_produces_the_same_digest():
    events = [(1.0, "probe", 1), (2.5, "advisory", 2), (77.0, "probe", 3)]

    assert build(events).digest() == build(events).digest()


def test_a_different_schedule_produces_a_different_digest():
    a = build([(1.0, "probe", 1)])
    b = build([(1.0, "probe", 2)])

    assert a.digest() != b.digest()


def test_the_digest_depends_on_order_not_only_on_content():
    """A trace is a sequence. Two runs with the same events at swapped times differ."""
    a = build([(1.0, "probe", 1), (2.0, "advisory", 2)])
    b = build([(2.0, "probe", 1), (1.0, "advisory", 2)])

    assert a.digest() != b.digest()


def test_scheduling_order_does_not_change_the_digest(clock: Clock):
    """Same events, registered in a different order, are the same trace.

    A dict-iteration-order dependency in a scenario loader would otherwise
    produce two byte-different traces for the same world.
    """
    forwards = Clock()
    for at_s in (1.0, 2.0, 3.0):
        forwards.at(at_s, "tick", {"t": at_s})
    forwards.run_until(5.0)

    backwards = Clock()
    for at_s in (3.0, 2.0, 1.0):
        backwards.at(at_s, "tick", {"t": at_s})
    backwards.run_until(5.0)

    assert forwards.digest() == backwards.digest()


def test_the_digest_covers_events_nobody_handled(clock: Clock):
    """Unhandled events are still part of what happened."""
    quiet = Clock()
    quiet.run_until(5.0)

    noisy = Clock()
    noisy.at(1.0, "gym_only")
    noisy.run_until(5.0)

    assert quiet.digest() != noisy.digest()


def test_processed_counts_dispatches(clock: Clock):
    clock.every(1.0, "beacon")
    clock.run_until(5.0)

    assert clock.processed == 5


# --------------------------------------------------------------------------
# stopwatch
# --------------------------------------------------------------------------


def test_a_fixed_stopwatch_ignores_the_machine_it_ran_on():
    """How a determinism test pins a run that would otherwise time itself."""
    with Stopwatch(fixed_s=0.8) as sw:
        sum(range(10_000))

    assert sw.elapsed_s == 0.8


def test_a_measured_stopwatch_measures_something_non_negative():
    with Stopwatch() as sw:
        sum(range(10_000))

    assert sw.elapsed_s >= 0.0


def test_reading_a_stopwatch_that_never_started_is_an_error():
    with pytest.raises(RuntimeError, match="never started"):
        _ = Stopwatch().elapsed_s


def test_a_negative_fixed_time_is_refused():
    with pytest.raises(ValueError, match="non-negative"):
        Stopwatch(fixed_s=-0.1)


def test_a_running_stopwatch_can_be_read_before_it_closes():
    with Stopwatch() as sw:
        first = sw.elapsed_s
        assert first >= 0.0


# --------------------------------------------------------------------------
# rejections
# --------------------------------------------------------------------------


def test_a_non_finite_event_time_is_refused(clock: Clock):
    with pytest.raises(ValueError, match="finite"):
        clock.at(float("inf"), "tick")


def test_a_non_finite_start_is_refused():
    with pytest.raises(ValueError, match="finite"):
        Clock(start_s=float("nan"))


def test_a_clock_can_start_somewhere_other_than_zero():
    clock = Clock(start_s=3_600.0)
    seen = record(clock, "tick")
    clock.schedule(60.0, "tick")

    clock.run_until(3_700.0)

    assert [e.at_s for e in seen] == [3_660.0]
