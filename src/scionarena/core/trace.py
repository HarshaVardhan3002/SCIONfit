"""Deterministic trace hashing. The tripwire for invariant 4.

Same seed plus same model plus same version means the same trace, byte for
byte, for the substrate. This module is how that claim is checked rather than
asserted: everything the substrate emits is folded into a digest, and a test
pins the digest.

The hash is over a *canonical* rendering, not over ``repr``. Two things make
that necessary:

* mapping iteration order must not change the digest, so keys are sorted;
* ``float`` needs one spelling per value, so ``-0.0`` folds to ``0.0`` and
  every NaN folds to the same token.

Order of ``update`` calls *is* significant. A trace is a sequence.

    >>> h = TraceHash()
    >>> _ = h.update({"t": 0.0, "path": "p1"})
    >>> _ = h.update({"t": 1.0, "path": "p2"})
    >>> h.short()
    '8f72b68083c6'
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["TraceHash", "canonical"]

_SEPARATOR = b"\x1e"  # record separator; keeps adjacent fields from merging


def canonical(value: Any) -> str:
    """Render ``value`` as the one string that stands for it in a trace.

    Supports the shapes a substrate event is made of: scalars, sequences,
    mappings, sets, and dataclasses. Anything else raises rather than falling
    back to ``repr``, because a ``repr`` containing a memory address would
    make a trace non-reproducible while still looking like it hashed fine.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if value == 0.0:
            return "0.0"  # -0.0 and 0.0 are the same measurement
        return repr(value)  # round-trip exact since 3.1
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, bytes):
        return f"b{value.hex()}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
        return f"{type(value).__name__}{canonical(fields)}"
    if isinstance(value, Mapping):
        items = sorted((canonical(k), canonical(v)) for k, v in value.items())
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    if isinstance(value, frozenset | set):
        return "{" + ",".join(sorted(canonical(v) for v in value)) + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    raise TypeError(
        f"{type(value).__name__} has no canonical form; a trace may not contain it. "
        "Convert it to a dataclass, mapping, sequence or scalar first."
    )


class TraceHash:
    """An incremental digest over a sequence of events.

    Incremental because a realistic-tier run emits far more events than fit in
    memory, and the point of the hash is to be computable while the run is
    still going.
    """

    def __init__(self, *, algorithm: str = "sha256", label: str = "") -> None:
        self.algorithm = algorithm
        self._digest = hashlib.new(algorithm)
        self._count = 0
        if label:
            self._digest.update(label.encode("utf-8") + _SEPARATOR)

    def update(self, event: Any) -> TraceHash:
        """Fold one event in. Returns self so calls chain."""
        self._digest.update(canonical(event).encode("utf-8") + _SEPARATOR)
        self._count += 1
        return self

    def extend(self, events: Iterable[Any]) -> TraceHash:
        for event in events:
            self.update(event)
        return self

    @property
    def count(self) -> int:
        """How many events have been folded in."""
        return self._count

    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    def short(self, n: int = 12) -> str:
        """The first ``n`` hex characters. What goes in a report or a filename."""
        return self._digest.hexdigest()[:n]

    def __repr__(self) -> str:
        return f"TraceHash({self.algorithm}, events={self._count}, {self.short()})"
