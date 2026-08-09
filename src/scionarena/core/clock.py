"""Simulated time and the event queue. Where invariant 3 stops being a promise.

The network does not wait for the model. That sentence is easy to agree with
and easy to violate by accident: any code that computes a state, hands it to a
model, and then applies the answer to *that* state has quietly paused the
world. This module makes the violation hard to write.

Two mechanisms do it.

**Time only moves forward.** :meth:`Clock.advance_to` refuses to go backwards
and :meth:`Clock.at` refuses to schedule into the past. A decision taken at
``t`` cannot be applied at ``t - epsilon``, so the "apply against the state I
was shown" bug is a raised exception rather than an optimistic result.

**Latency is scheduled, not slept.** A model that thinks for 800 ms does not
block the world for 800 ms; the caller measures the thinking with a
:class:`Stopwatch` and schedules the consequence at ``now + 0.8``. The world
keeps stepping in between, which is the whole point: the advisory lands against
a network that moved.

Events are data, never closures. Handlers are registered by kind:

    >>> clock = Clock()
    >>> seen = []
    >>> _ = clock.on("beacon", lambda e: seen.append(e.at_s))
    >>> _ = clock.every(30.0, "beacon")
    >>> clock.run_until(70.0)
    2
    >>> seen
    [30.0, 60.0]

That split is not stylistic. A queue holding callables cannot be hashed into a
trace, cannot be serialised into a scenario file, and cannot be compared
between two processes -- and all three are things M1 has to deliver.
"""

from __future__ import annotations

import heapq
import math
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from scionarena.core.trace import TraceHash

__all__ = ["Event", "Clock", "Stopwatch", "Handler"]

Payload = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happens at one simulated time.

    ``priority`` breaks ties at equal ``at_s``: lower runs first. It exists so
    that "the world updates" can be ordered ahead of "somebody observes the
    world" without either side knowing about the other. ``seq`` breaks the
    remaining ties in scheduling order, which is what makes two processes agree
    on the order of simultaneous events.
    """

    at_s: float
    kind: str
    payload: Payload = field(default_factory=dict)
    seq: int = 0
    priority: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.at_s):
            raise ValueError(f"event time must be finite, got {self.at_s}")

    @property
    def key(self) -> tuple[float, int, int]:
        """The total order. Never compare events directly; payloads are dicts."""
        return (self.at_s, self.priority, self.seq)

    def get(self, name: str, default: Any = None) -> Any:
        return self.payload.get(name, default)


Handler = Callable[[Event], None]


class Clock:
    """A priority queue of events over a simulated clock.

    The clock is simulated and deterministic. Real elapsed time enters only
    where a caller deliberately charges it (see :class:`Stopwatch`), because
    invariant 4 permits model nondeterminism and forbids the world's.
    """

    def __init__(self, *, start_s: float = 0.0, label: str = "clock") -> None:
        if not math.isfinite(start_s):
            raise ValueError(f"start time must be finite, got {start_s}")
        self._now = float(start_s)
        # (order key, push counter, event). The push counter is there so that
        # heapq never has to compare two Events: they hold dicts and are not
        # orderable, and a TypeError deep inside the queue would be a puzzle.
        self._heap: list[tuple[tuple[float, int, int], int, Event]] = []
        self._handlers: dict[str, list[Handler]] = {}
        self._repeats: dict[int, float] = {}
        self._cancelled: set[int] = set()
        self._pushes = 0
        self._next_seq = 0
        self._processed = 0
        self._trace = TraceHash(label=label)

    # ---- inspection -----------------------------------------------------

    @property
    def now(self) -> float:
        """Current simulated time, in seconds since the start of the run."""
        return self._now

    @property
    def pending(self) -> int:
        """Events still queued, cancellations excluded."""
        return len(self._heap) - sum(1 for *_, e in self._heap if e.seq in self._cancelled)

    @property
    def processed(self) -> int:
        """Events dispatched so far. Folded into the digest in this order."""
        return self._processed

    def peek(self) -> Event | None:
        """The next event that will actually run, without running it."""
        while self._heap and self._heap[0][2].seq in self._cancelled:
            *_, dead = heapq.heappop(self._heap)
            self._forget(dead.seq)
        return self._heap[0][2] if self._heap else None

    def next_time(self) -> float | None:
        event = self.peek()
        return None if event is None else event.at_s

    def __repr__(self) -> str:
        return f"Clock(t={self._now:.3f}s, pending={self.pending}, processed={self._processed})"

    # ---- registration ---------------------------------------------------

    def on(self, kind: str, handler: Handler) -> Handler:
        """Register ``handler`` for ``kind``. Returns it, so it works as a decorator.

        Several handlers may share a kind; they run in registration order. An
        event whose kind has no handler is not an error -- it is dispatched to
        nobody and still recorded in the trace, which is how a scenario can
        carry an event that only one front-end cares about.
        """
        self._handlers.setdefault(kind, []).append(handler)
        return handler

    def off(self, kind: str, handler: Handler | None = None) -> None:
        """Remove one handler, or every handler for ``kind``."""
        if handler is None:
            self._handlers.pop(kind, None)
            return
        handlers = self._handlers.get(kind)
        if handlers and handler in handlers:
            handlers.remove(handler)

    # ---- scheduling -----------------------------------------------------

    def at(
        self, t_s: float, kind: str, payload: Payload | None = None, *, priority: int = 0
    ) -> int:
        """Schedule ``kind`` at absolute time ``t_s``. Returns a cancellation handle.

        Scheduling into the past raises. That is the load-bearing half of
        invariant 3: a consequence cannot be applied before its cause, so a
        decision computed from stale state cannot be back-dated onto the state
        it was computed from.
        """
        if t_s < self._now:
            raise ValueError(
                f"cannot schedule {kind!r} at t={t_s:.6f}s, which is "
                f"{self._now - t_s:.6f}s before now ({self._now:.6f}s); "
                "the network does not wait for the model (invariant 3)"
            )
        seq = self._next_seq
        self._next_seq += 1
        event = Event(at_s=float(t_s), kind=kind, payload=payload or {}, seq=seq, priority=priority)
        self._push(event)
        return seq

    def _push(self, event: Event) -> None:
        heapq.heappush(self._heap, (event.key, self._pushes, event))
        self._pushes += 1

    def schedule(
        self, delay_s: float, kind: str, payload: Payload | None = None, *, priority: int = 0
    ) -> int:
        """Schedule ``kind`` at ``now + delay_s``. Negative delays raise."""
        if delay_s < 0.0:
            raise ValueError(f"delay must not be negative, got {delay_s}")
        return self.at(self._now + delay_s, kind, payload, priority=priority)

    def every(
        self,
        interval_s: float,
        kind: str,
        payload: Payload | None = None,
        *,
        start_s: float | None = None,
        priority: int = 0,
    ) -> int:
        """Schedule ``kind`` every ``interval_s``, first firing at ``now + interval_s``.

        The handle stays valid for the life of the repeat, so one
        :meth:`cancel` stops the series rather than one occurrence.
        """
        if not (interval_s > 0.0 and math.isfinite(interval_s)):
            raise ValueError(f"repeat interval must be finite and positive, got {interval_s}")
        first = self._now + interval_s if start_s is None else start_s
        seq = self.at(first, kind, payload, priority=priority)
        self._repeats[seq] = float(interval_s)
        return seq

    def cancel(self, handle: int) -> None:
        """Cancel a scheduled event or a repeating series. Cancelling twice is fine."""
        self._cancelled.add(handle)
        self._repeats.pop(handle, None)

    def _forget(self, seq: int) -> None:
        self._cancelled.discard(seq)

    # ---- running --------------------------------------------------------

    def run_until(self, t_s: float, *, max_events: int | None = None) -> int:
        """Dispatch every event up to and including ``t_s``, then set ``now`` to ``t_s``.

        Returns the number of events dispatched. Handlers may schedule further
        events; any that land at or before ``t_s`` run in this same call, in
        time order, which is what makes a cascade behave the way it would in a
        continuous world rather than being deferred a step.

        ``now`` ends at ``t_s`` even when the queue empties early. A step that
        contained no events still consumed time.
        """
        if t_s < self._now:
            raise ValueError(
                f"cannot run to t={t_s:.6f}s from now={self._now:.6f}s; time does not run backwards"
            )
        dispatched = 0
        while max_events is None or dispatched < max_events:
            event = self.peek()
            if event is None or event.at_s > t_s:
                break
            self._run_one()
            dispatched += 1
        self._now = float(t_s)
        return dispatched

    def run_next(self) -> Event | None:
        """Dispatch the single next event, advancing ``now`` to it. None if idle."""
        if self.peek() is None:
            return None
        return self._run_one()

    def run_all(self, *, max_events: int = 1_000_000) -> int:
        """Drain the queue. Bounded, because a repeating event never empties one."""
        dispatched = 0
        while dispatched < max_events and self.peek() is not None:
            self._run_one()
            dispatched += 1
        return dispatched

    def _run_one(self) -> Event:
        *_, event = heapq.heappop(self._heap)
        self._now = event.at_s
        interval = self._repeats.get(event.seq)
        if interval is not None:
            # Reschedule before dispatch: a handler that cancels the series
            # must win, and a handler that raises must not silently stop it.
            following = Event(
                at_s=event.at_s + interval,
                kind=event.kind,
                payload=event.payload,
                seq=event.seq,
                priority=event.priority,
            )
            self._push(following)
        self._processed += 1
        # Deliberately no ``seq``. The digest is over what happened, in the
        # order it happened; scheduling order is an implementation detail and
        # folding it in would make a scenario loader's dict iteration order
        # change the trace hash of an identical world.
        self._trace.update({"t": event.at_s, "kind": event.kind, "payload": dict(event.payload)})
        for handler in self._handlers.get(event.kind, ()):
            handler(event)
        return event

    def advance_to(self, t_s: float) -> None:
        """Move time forward without dispatching anything.

        For substrate components that are stepped directly rather than through
        the queue. It still refuses to rewind.
        """
        if t_s < self._now:
            raise ValueError(f"cannot rewind from {self._now:.6f}s to {t_s:.6f}s")
        self._now = float(t_s)

    # ---- determinism ----------------------------------------------------

    def digest(self) -> str:
        """Hash of every event dispatched, in order. Two processes must agree."""
        return self._trace.hexdigest()

    def short_digest(self, n: int = 12) -> str:
        return self._trace.short(n)

    def upcoming(self) -> Iterator[Event]:
        """Queued events in time order. Inspection only; drains nothing."""
        live = [e for *_, e in self._heap if e.seq not in self._cancelled]
        yield from sorted(live, key=lambda e: e.key)


class Stopwatch:
    """Measures *real* seconds so a caller can charge them to simulated time.

    This is the only place real time is read, and it never touches the clock by
    itself. The caller decides what the measurement means::

        with Stopwatch() as sw:
            advisory = model.advise(...)
        clock.schedule(sw.elapsed_s, "advisory_applied", {"scope": scope})

    Written that way the world runs during the model's thinking, and a slow
    model is worse than a fast one for the reason it should be: its answer
    arrives later, against a network that has moved on.

    ``fixed_s`` replaces the measurement with a constant, which is how a
    determinism test pins a run that would otherwise depend on the speed of the
    machine it ran on.
    """

    __slots__ = ("fixed_s", "_start", "_elapsed")

    def __init__(self, *, fixed_s: float | None = None) -> None:
        if fixed_s is not None and (fixed_s < 0.0 or not math.isfinite(fixed_s)):
            raise ValueError(f"fixed elapsed time must be finite and non-negative, got {fixed_s}")
        self.fixed_s = fixed_s
        self._start: float | None = None
        self._elapsed: float | None = None

    def __enter__(self) -> Stopwatch:
        self._start = time.perf_counter()
        self._elapsed = None
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._start is not None
        self._elapsed = time.perf_counter() - self._start

    @property
    def elapsed_s(self) -> float:
        """Real seconds inside the ``with`` block, or ``fixed_s`` if one was given."""
        if self.fixed_s is not None:
            return self.fixed_s
        if self._elapsed is None:
            if self._start is None:
                raise RuntimeError("stopwatch was never started")
            return time.perf_counter() - self._start  # read while still running
        return self._elapsed

    def __repr__(self) -> str:
        state = "fixed" if self.fixed_s is not None else "measured"
        try:
            return f"Stopwatch({state}, {self.elapsed_s * 1e3:.3f}ms)"
        except RuntimeError:
            return "Stopwatch(unstarted)"
