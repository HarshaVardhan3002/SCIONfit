"""Observation feeds. Raw records, in order, and nothing else.

Three feeds:

``beacons``
    One record per segment that was re-signed, carrying both identifiers. The
    feed a model needs to notice that its per-path memory is being invalidated
    every refresh cycle -- and the feed it will not be reading when it does not.

``telemetry``
    One record per path that carried traffic during a step. Sparse by
    construction: a path nobody sends on is not measured. The set of measured
    paths is biased towards what the model recommended, which is a property of
    the problem rather than a defect in the generator, and is not corrected.

``path_server``
    One record per subscribed scope whose path set changed, with the ids that
    appeared and disappeared. A path can vanish through AS policy filtering with
    no link going anywhere.

Invariant 1 in one sentence: **nothing in this module computes an average.**
There is no windowing, no rate, no "recent" anything, no derived field. If a
model wants a moving average it keeps one. Adding a convenience aggregate here
would make the M8 memory experiments measure the harness instead of the model.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "STREAMS",
    "RawEvent",
    "Subscription",
    "EventLog",
    "matches",
]

#: The feeds a model may subscribe to. A name outside this set is a rejected
#: call, not a subscription that silently never delivers.
STREAMS = ("beacons", "telemetry", "path_server")

#: Rough wire size of a record, used to charge bandwidth. Not measured with
#: ``sys.getsizeof``: what is being charged is what a real feed would put on
#: the wire, not what CPython happens to allocate.
BYTES_PER_FIELD = 24
BYTES_PER_RECORD = 48


@dataclass(frozen=True, slots=True)
class RawEvent:
    """One thing that happened, as it happened.

    ``data`` is whatever the feed produces, unprocessed. Consumers are welcome
    to find it inconvenient.
    """

    t: float
    stream: str
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def nbytes(self) -> int:
        return BYTES_PER_RECORD + BYTES_PER_FIELD * len(self.data)

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return {"t": round(self.t, 9), "stream": self.stream, "kind": self.kind, **self.data}


def matches(event: RawEvent, filt: Mapping[str, Any] | None) -> bool:
    """Does this record pass a subscription filter?

    The filter is a flat mapping of field to required value, or to a collection
    of accepted values. ``kind`` and ``stream`` are matched against the record's
    own attributes, everything else against ``data``. Deliberately dumb: a
    filter language is a summarisation surface, and the answer to those is no.
    """
    if not filt:
        return True
    for key, want in filt.items():
        if key == "stream":
            have: Any = event.stream
        elif key == "kind":
            have = event.kind
        else:
            if key not in event.data:
                return False
            have = event.data[key]
        if isinstance(want, (list, tuple, set, frozenset)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


@dataclass
class Subscription:
    """A model's standing interest in one feed.

    Holds its own cursor into the log. Records are charged to the budget as they
    are delivered, which is what "ongoing bandwidth" costs.
    """

    handle: str
    stream: str
    filt: Mapping[str, Any] | None
    created_s: float
    cursor: int = 0
    delivered: int = 0
    nbytes: int = 0
    #: Records the bandwidth budget could not pay for. A subscription starves
    #: rather than silently thinning out, and the count says by how much.
    dropped: int = 0
    #: Records that arrived and have not yet been handed to the model.
    queue: list[RawEvent] = field(default_factory=list)

    def accept(self, event: RawEvent) -> bool:
        return event.stream == self.stream and matches(event, self.filt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "stream": self.stream,
            "filter": dict(self.filt) if self.filt else None,
            "created_s": round(self.created_s, 9),
            "delivered": self.delivered,
            "nbytes": self.nbytes,
            "dropped": self.dropped,
            "queued": len(self.queue),
        }


class EventLog:
    """Append-only, ordered, complete.

    This is the object M4 reads for operational metrics and M8 reads for memory
    tests, so it keeps everything. Trimming it would be a summarisation with
    extra steps.
    """

    def __init__(self) -> None:
        self._events: list[RawEvent] = []

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[RawEvent]:
        return iter(self._events)

    def __getitem__(self, index: int) -> RawEvent:
        return self._events[index]

    def append(self, event: RawEvent) -> None:
        self._events.append(event)

    def extend(self, events: Iterable[RawEvent]) -> None:
        self._events.extend(events)

    def since(self, cursor: int) -> Sequence[RawEvent]:
        return self._events[cursor:]

    def select(
        self,
        *,
        filt: Mapping[str, Any] | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = None,
    ) -> list[RawEvent]:
        """The raw records in a time range, in order, optionally filtered.

        ``limit`` truncates from the *oldest* end of the range, not the newest:
        asking for the last 10 of a busy hour should not silently answer with the
        first 10 and let the model believe nothing happened afterwards.
        """
        out = [
            e
            for e in self._events
            if (since is None or e.t >= since) and (until is None or e.t <= until)
            if matches(e, filt)
        ]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    @property
    def nbytes(self) -> int:
        return sum(e.nbytes for e in self._events)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._events:
            key = f"{e.stream}.{e.kind}"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))
