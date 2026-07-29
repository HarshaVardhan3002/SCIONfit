"""Scale tiers. One definition, used by every test, benchmark and scenario.

12 ASes is a demo, not a test. These four numbers are what the substrate is
validated against, and ``realistic`` is the one that matters: every performance
budget in ``core/`` is stated against it.

The tier numbers are a guess pending open question Q3 and are marked
``ASSUMPTION(Q3)`` where they set a budget. They are in one place precisely so
that answering Q3 is a one-line change rather than a search.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Tier", "SMOKE", "DEV", "REALISTIC", "STRESS", "TIERS", "tier"]


@dataclass(frozen=True)
class Tier:
    """One point on the scale ladder.

    ``n_links`` is a target, not a guarantee: a generator hits it within a few
    percent, because the last few links are constrained by what the topology
    will accept.
    """

    name: str
    n_ases: int
    n_links: int
    paths_per_pair: tuple[int, int]
    #: wall-clock budget for building the topology, seconds. None = not gated.
    build_budget_s: float | None = None
    #: wall-clock budget for one substrate step, seconds. None = not gated.
    step_budget_s: float | None = None
    #: resident memory budget, bytes. None = not gated.
    memory_budget_bytes: int | None = None

    @property
    def n_isds(self) -> int:
        """Default ISD count. Roughly one ISD per 250 ASes, at least one."""
        return max(1, round(self.n_ases / 250))

    @property
    def mean_degree(self) -> float:
        return 2.0 * self.n_links / self.n_ases


SMOKE = Tier("smoke", n_ases=20, n_links=60, paths_per_pair=(3, 5))
DEV = Tier("dev", n_ases=200, n_links=800, paths_per_pair=(10, 30))

# ASSUMPTION(Q3): 2,000 ASes / 10,000 links / 100-300 paths per pair is a guess
# at a deployed ISD's scale. The budgets below are the project's performance
# contract and they all hang off it.
REALISTIC = Tier(
    "realistic",
    n_ases=2_000,
    n_links=10_000,
    paths_per_pair=(100, 300),
    build_budget_s=10.0,
    step_budget_s=0.010,
    memory_budget_bytes=2 * 1024**3,
)

# Not gated. Stress exists to prove nothing falls over an order of magnitude up,
# and to record the timings so a later regression is visible.
STRESS = Tier("stress", n_ases=20_000, n_links=100_000, paths_per_pair=(500, 1_000))

TIERS = {t.name: t for t in (SMOKE, DEV, REALISTIC, STRESS)}


def tier(name: str) -> Tier:
    try:
        return TIERS[name]
    except KeyError:
        raise KeyError(f"unknown tier {name!r}; known tiers are {sorted(TIERS)}") from None
