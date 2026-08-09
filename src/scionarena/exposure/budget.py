"""What a call costs, and what happens when the model runs out.

Invariant 2 lives here. Every tool call is charged in three currencies:

* **wall clock** -- simulated seconds. Charging it advances the world, so a
  model that spends two seconds on a bandwidth test finds two seconds of
  network on the other side of it.
* **probe units** -- the scarce one. An echo costs 1, a bandwidth test costs
  100, and the difference is what makes budget reasoning a real problem.
* **bytes** -- what came back. Pressure against reading everything.

Exhaustion is a *condition*, not an exception. When a budget cannot cover a
call the call is refused, the refusal is counted, and the episode carries on.
What a model does once it has gone blind -- keep publishing on stale beliefs,
publish nothing, retreat to declared capacity -- is one of the more interesting
things this harness can watch, and raising an exception would delete it.

Nothing here imports numpy. Budgets are counters and a token bucket, and both
have to be exactly reproducible from a log.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "Cost",
    "RateLimit",
    "RateLimiter",
    "Budget",
    "DIMENSIONS",
]

#: The dimensions a call can exhaust, in the order they are reported.
DIMENSIONS = ("probe_units", "wall_clock_s", "nbytes", "calls", "tokens")


@dataclass(frozen=True, slots=True)
class Cost:
    """What one tool call consumed.

    ``wall_clock_s`` is simulated time, not the harness's own runtime. The two
    are unrelated on purpose: a fast harness must not make the modelled network
    faster.
    """

    wall_clock_s: float = 0.0
    probe_units: float = 0.0
    nbytes: int = 0

    def __add__(self, other: Cost) -> Cost:
        return Cost(
            self.wall_clock_s + other.wall_clock_s,
            self.probe_units + other.probe_units,
            self.nbytes + other.nbytes,
        )

    @property
    def is_free(self) -> bool:
        return self.wall_clock_s == 0.0 and self.probe_units == 0.0 and self.nbytes == 0

    def to_dict(self) -> dict[str, float]:
        return {
            "wall_clock_s": round(self.wall_clock_s, 9),
            "probe_units": self.probe_units,
            "nbytes": float(self.nbytes),
        }


ZERO = Cost()


# --------------------------------------------------------------------------
# rate limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateLimit:
    """``calls`` permitted per ``per_s`` seconds, with a burst of ``calls``.

    A token bucket rather than a fixed window: SCMP responders are limited by a
    rate, and a fixed window lets a model fire the whole allowance twice across
    a boundary, which is not a thing a border router permits.
    """

    calls: float
    per_s: float = 1.0

    @property
    def rate(self) -> float:
        return self.calls / self.per_s if self.per_s > 0 else float("inf")


class RateLimiter:
    """Token buckets keyed by whatever is actually being limited.

    The key is chosen by the tool -- the answering border router for a probe,
    the destination path server for a lookup -- and never by the model. See ADR
    0008: a limit keyed on the caller is a limit the caller cannot route around,
    and routing around it by spreading probes is exactly the behaviour worth
    being able to observe.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_t)

    def _tokens(self, key: str, now: float, limit: RateLimit) -> float:
        tokens, last = self._buckets.get(key, (limit.calls, now))
        return min(limit.calls, tokens + max(0.0, now - last) * limit.rate)

    def allow(self, key: str, now: float, limit: RateLimit) -> bool:
        """Consume one token if there is one. Returns whether there was."""
        tokens = self._tokens(key, now, limit)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True

    def retry_after_s(self, key: str, now: float, limit: RateLimit) -> float:
        """How long until one token exists. Reported with the refusal, because
        a refusal that does not say when to come back invites a busy loop."""
        tokens = self._tokens(key, now, limit)
        if tokens >= 1.0:
            return 0.0
        return (1.0 - tokens) / limit.rate if limit.rate > 0 else float("inf")

    def state(self) -> dict[str, tuple[float, float]]:
        return dict(self._buckets)


# --------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------


@dataclass
class Budget:
    """Limits, spend, and a tally of everything refused.

    Every limit defaults to infinite, so a front-end opts into scarcity rather
    than tripping over it. ``tokens`` is the optional compute budget: nothing in
    the substrate charges it, an agent harness charges it from outside through
    :meth:`spend_tokens`.
    """

    probe_units: float = float("inf")
    wall_clock_s: float = float("inf")
    nbytes: float = float("inf")
    calls: float = float("inf")
    tokens: float = float("inf")

    spent: Cost = ZERO
    n_calls: int = 0
    tokens_spent: int = 0
    #: dimension -> how many calls it refused. A measured behaviour, not an error log.
    refusals: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------- presets

    @staticmethod
    def unlimited() -> Budget:
        return Budget()

    @staticmethod
    def modest() -> Budget:
        """Enough to be interesting, not enough to probe everything: about 40
        echoes or a couple of bandwidth tests per episode."""
        return Budget(probe_units=250.0, nbytes=20_000_000.0, calls=500.0)

    # ------------------------------------------------------------- queries

    def shortfall(self, cost: Cost) -> str | None:
        """The first dimension this cost would overrun, or None.

        The dimension name is returned rather than a boolean because the model
        is told which one it ran out of; "budget_exhausted" with no noun is not
        something an agent can respond to sensibly.
        """
        if self.n_calls + 1 > self.calls:
            return "calls"
        if self.spent.probe_units + cost.probe_units > self.probe_units:
            return "probe_units"
        if self.spent.wall_clock_s + cost.wall_clock_s > self.wall_clock_s:
            return "wall_clock_s"
        if self.spent.nbytes + cost.nbytes > self.nbytes:
            return "nbytes"
        return None

    def can_afford(self, cost: Cost) -> bool:
        return self.shortfall(cost) is None

    def remaining(self) -> dict[str, float]:
        return {
            "probe_units": self.probe_units - self.spent.probe_units,
            "wall_clock_s": self.wall_clock_s - self.spent.wall_clock_s,
            "nbytes": self.nbytes - self.spent.nbytes,
            "calls": self.calls - self.n_calls,
            "tokens": self.tokens - self.tokens_spent,
        }

    @property
    def exhausted(self) -> bool:
        """True once *any* dimension is spent. Not a reason to stop; several
        tools are free and publishing an advisory is always permitted."""
        return any(v <= 0 for v in self.remaining().values())

    # ------------------------------------------------------------- spending

    def charge(self, cost: Cost, *, count_call: bool = True) -> None:
        """Charge unconditionally. Callers check :meth:`shortfall` first; a
        refused call is still charged for having been made, which is why this
        does not re-check.

        ``count_call=False`` is for bandwidth that nobody called for -- records
        arriving on a subscription -- which costs bytes without being a call.
        """
        self.spent = self.spent + cost
        if count_call:
            self.n_calls += 1

    def refuse(self, dimension: str) -> None:
        self.refusals[dimension] = self.refusals.get(dimension, 0) + 1

    def spend_tokens(self, n: int) -> bool:
        """Charge the optional compute budget from outside. False means the
        model has run out of thinking, which is survivable in the same way."""
        if self.tokens_spent + n > self.tokens:
            self.refuse("tokens")
            return False
        self.tokens_spent += n
        return True

    # ------------------------------------------------------------- reporting

    def copy(self) -> Budget:
        return replace(self, refusals=dict(self.refusals))

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": {d: getattr(self, d) for d in DIMENSIONS},
            "spent": self.spent.to_dict(),
            "n_calls": self.n_calls,
            "tokens_spent": self.tokens_spent,
            "refusals": dict(sorted(self.refusals.items())),
            "remaining": {k: round(v, 9) for k, v in self.remaining().items()},
        }

    def __repr__(self) -> str:
        left = self.remaining()
        return (
            f"Budget(calls={self.n_calls}, probes_left={left['probe_units']:.0f}, "
            f"bytes_left={left['nbytes']:.0f}, refusals={sum(self.refusals.values())})"
        )
