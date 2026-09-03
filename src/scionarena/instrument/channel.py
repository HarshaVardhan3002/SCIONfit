"""The live channel: frames out of the worker processes, and marked gaps.

The only genuinely new mechanism Phase 7 needs, and the one place where two
invariants meet head-on.

**Invariant 3 -- the network does not wait for the model.** It must not wait for a
browser either. Every write here is :meth:`Queue.put_nowait`, and a full queue
*drops the frame and counts the drop*. A channel that applied backpressure to the
run would be a channel that changed the run it was watching, and every number it
showed would then belong to a different experiment.

**Invariant 1 -- the harness never summarises for the model.** The channel is
write-only from the worker and read-only from the server. Nothing a model can
reach holds a handle on it, so watching a run cannot change it, so a live result
still reproduces from its seed.

A dropped frame is **marked, never interpolated.** Every frame carries a sequence
number per cell; a reader that sees the sequence jump knows exactly how many it
missed and says so. A smooth line drawn through a hole is a lie about a system
whose whole subject is instability.

Cells run in a process pool (ADR 0014, and deliberately), so this is a
``multiprocessing`` queue rather than a shared object.

It lives in ``instrument`` rather than in ``cockpit`` because ``bench`` is what
writes to it and a front-end may not import the interface that watches it. Like
everything else here it imports nothing from ``exposure``, which is what lets it
be read back off a queue by a process that never built a world.
"""

from __future__ import annotations

import multiprocessing
import queue
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["Frame", "LiveChannel", "FrameLog", "DEFAULT_DEPTH"]

#: How many frames the queue holds before it starts dropping. Deep enough that a
#: browser polling at a human rate keeps up with a smoke-tier sweep, shallow
#: enough that a stalled reader cannot make the pool hold a backlog it will
#: never deliver.
DEFAULT_DEPTH = 2_000


@dataclass(frozen=True, slots=True)
class Frame:
    """One decision round, as it happened. Plain data, and small on purpose.

    Everything here is a number the round already computed. Nothing is derived,
    averaged or re-scaled on the way out: a frame is evidence, and a frame that
    had been processed would be the harness summarising -- for a reader rather
    than for the model, but through the same door.
    """

    cell_id: str
    model: str
    #: Per-cell, monotonic from zero. The only thing that lets a reader tell a
    #: quiet run from a dropped frame.
    seq: int
    #: Simulated seconds.
    t: float
    #: The round's decision latency, in simulated seconds. What the model cost.
    latency_s: float
    #: Which decision round. Carried beside ``t`` rather than derived from it,
    #: because a round that overran covers several samples and the two axes stop
    #: agreeing exactly when a reader most wants to know that they have.
    cycle: int = 0
    #: What it published: how many advisories, and how concentrated. 1.0 is a
    #: one-hot ranking; near 1/n is round robin.
    advisories: int = 0
    max_weight: float = 0.0
    #: What it was shown: tool calls made and telemetry records taken this round.
    calls: int = 0
    records: int = 0
    #: What the world did back, on the sample the round ended on.
    mean_cost_ms: float = 0.0
    best_cost_ms: float = 0.0
    deviation: float = 0.0
    #: Advisory weights this round, per scope, so the lens can show what moved
    #: rather than only that something did. Capped by the emitter.
    weights: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LiveChannel:
    """The write end. Held by a worker; never by a model.

    ``None`` for the queue is the normal case: an unwatched sweep pays one
    attribute lookup per round for the ability to be watched.
    """

    def __init__(self, sink: Any | None = None) -> None:
        self._sink = sink
        self._seq: dict[str, int] = {}
        #: Frames this worker could not hand over. Reported so the interface can
        #: say how much it is not seeing.
        self.dropped = 0

    @property
    def live(self) -> bool:
        return self._sink is not None

    def emit(self, cell_id: str, model: str, **fields: Any) -> None:
        """Offer one frame. Never blocks, never raises."""
        if self._sink is None:
            return
        seq = self._seq.get(cell_id, 0)
        self._seq[cell_id] = seq + 1
        frame = Frame(cell_id=cell_id, model=model, seq=seq, **fields)
        try:
            self._sink.put_nowait(frame.to_dict())
        except (queue.Full, ValueError, OSError):
            # Full, or the reader has gone. Both are the same decision: the run
            # continues and the viewer misses a frame.
            self.dropped += 1


class FrameLog:
    """The read end. Drains the queue into a bounded history the page can page through.

    Bounded because a realistic-tier sweep is tens of thousands of rounds and a
    browser asks for the last few hundred. What falls out of the back is gone,
    and :meth:`gaps` still knows it was there, because the sequence numbers do.
    """

    def __init__(self, depth: int = DEFAULT_DEPTH, keep: int = 4_000) -> None:
        self.queue: Any = multiprocessing.Manager().Queue(maxsize=depth)
        self._frames: deque[dict[str, Any]] = deque(maxlen=keep)
        self._last_seq: dict[str, int] = {}
        #: Frames the sequence numbers prove were produced and never arrived.
        self.missing = 0
        self._offset = 0

    def drain(self) -> int:
        """Pull everything waiting. Returns how many frames arrived."""
        taken = 0
        while True:
            try:
                frame = self.queue.get_nowait()
            except (queue.Empty, EOFError, OSError):
                break
            cell = str(frame.get("cell_id", ""))
            seq = int(frame.get("seq", 0))
            previous = self._last_seq.get(cell)
            if previous is not None and seq > previous + 1:
                # Marked, and never filled in. The gap is a fact about the run.
                gap = seq - previous - 1
                self.missing += gap
                frame = {**frame, "gap_before": gap}
            self._last_seq[cell] = seq
            if len(self._frames) == self._frames.maxlen:
                self._offset += 1
            self._frames.append(frame)
            taken += 1
        return taken

    def since(self, index: int) -> tuple[int, list[dict[str, Any]]]:
        """Frames after ``index``, and the index to ask for next.

        The index counts every frame this log has ever held, so a reader that
        falls behind the ring buffer is handed the oldest it still has rather
        than silently restarted at zero.
        """
        start = max(0, index - self._offset)
        rows = list(self._frames)[start:]
        return self._offset + len(self._frames), rows

    def __len__(self) -> int:
        return len(self._frames)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._frames)
