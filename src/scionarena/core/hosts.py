"""The host population. Where advice becomes traffic.

An advisory is a distribution over paths. This module is what turns it into
Mbps on interfaces, and it is the half of the loop the model does not get to
see. Hosts sample independently from the published distribution and nothing
else: they cannot read the link state, so a model that publishes badly cannot
be rescued by hosts that know better. Defectors are the bounded exception and
they read only what a host could measure for itself.

Hosts are counts, not objects. One scope is a handful of numpy arrays over its
path set and a population size, and sampling ten thousand hosts is one
``multinomial`` draw. That is a performance decision at the realistic tier --
a hundred scopes times ten thousand host objects per step does not fit in a
10 ms budget -- and it is also what makes the ``1/sqrt(N)`` concentration
exact rather than reproduced by a loop. See ADR 0009.

Determinism is counter-based, as it is in :mod:`scionarena.core.linkstate`:
every draw is a function of ``(seed, scope, step index)`` and of nothing that
happened before it. A population that threaded one RNG through its steps would
depend on how often it was asked, and two runs that stepped through time
differently would diverge while both being correct.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from scionarena.core.linkstate import LinkState
from scionarena.core.segments import Path
from scionarena.core.topology import Topology
from scionarena.core.trace import TraceHash

__all__ = [
    "DEFECTOR_KINDS",
    "HostParams",
    "ScopeState",
    "HostPopulation",
    "concentration_bound",
]

#: PAS v3 adversary taxonomy. Only the first is implemented; M6 fills in the
#: rest. The hook is here now so that the loop, the scenario schema and the
#: report all carry the field, and adding a class later is one branch rather
#: than a change to four files.
DEFECTOR_KINDS = ("greedy",)


@dataclass(frozen=True, slots=True)
class HostParams:
    """What a scope's population is like.

    ``mbps_per_host`` is per host and the default is deliberately large: the
    synthetic topology's customer links are 1-10 Gbps, so a population offering
    a few hundred Mbps would never congest anything and the closed loop would
    be a loop in name only.
    """

    n_hosts: int = 100
    mbps_per_host: float = 20.0
    #: Fraction of the population that ignores the advisory. Rounded down, so a
    #: tiny fraction of a small population is zero defectors rather than one.
    defector_fraction: float = 0.0
    defector_kind: str = "greedy"
    #: Birth-death churn, per second. Both zero means a fixed population, which
    #: is the default because a moving N makes every other measurement noisier
    #: and should be asked for deliberately.
    arrivals_per_s: float = 0.0
    departures_per_s: float = 0.0
    #: Fractions by SLA name. Normalised on construction. The mix does not
    #: change how a host samples yet -- it is carried so that the scope knows
    #: what it is serving and M6's per-class scoring has something to read.
    sla_mix: tuple[tuple[str, float], ...] = (("bulk", 1.0),)
    #: How often a host reconsiders. Longer than the step means most hosts stay
    #: where they were, which damps the loop; that is a property of the
    #: population, not of the model, and scoring has to know which it got.
    resample_s: float = 1.0

    def __post_init__(self) -> None:
        if self.n_hosts < 0:
            raise ValueError(f"n_hosts must not be negative, got {self.n_hosts}")
        if self.mbps_per_host < 0.0:
            raise ValueError(f"mbps_per_host must not be negative, got {self.mbps_per_host}")
        if not 0.0 <= self.defector_fraction <= 1.0:
            raise ValueError(f"defector_fraction must be in [0, 1], got {self.defector_fraction}")
        if self.defector_kind not in DEFECTOR_KINDS:
            raise ValueError(
                f"unknown defector_kind {self.defector_kind!r}; "
                f"implemented kinds are {list(DEFECTOR_KINDS)} and the rest arrive in M6"
            )
        if self.arrivals_per_s < 0.0 or self.departures_per_s < 0.0:
            raise ValueError("churn rates must not be negative")
        if self.resample_s <= 0.0:
            raise ValueError(f"resample_s must be positive, got {self.resample_s}")
        mix = tuple((str(name), float(w)) for name, w in self.sla_mix)
        if not mix:
            raise ValueError("sla_mix must name at least one SLA")
        total = sum(w for _, w in mix)
        if total <= 0.0:
            raise ValueError("sla_mix weights must sum to something positive")
        object.__setattr__(self, "sla_mix", tuple((n, w / total) for n, w in mix))

    @property
    def n_defectors(self) -> int:
        return int(self.n_hosts * self.defector_fraction)

    @property
    def offered_mbps(self) -> float:
        """What the whole population offers, if every host is up."""
        return self.n_hosts * self.mbps_per_host


@dataclass
class ScopeState:
    """One (src, dst) population and the path set it is spread over.

    Everything per-path is a parallel array indexed the same way as
    ``path_ids``. ``ifaces`` and ``owner`` are the flattened egress interfaces
    of every path and which path each one belongs to, which is what makes
    "scatter this scope's load onto the network" one ``bincount`` instead of a
    loop over paths.
    """

    src: int
    dst: int
    params: HostParams
    #: Path identifiers under the scenario's identity policy, in path order.
    path_ids: tuple[int, ...] = ()
    ifaces: NDArray[np.int64] = field(default_factory=lambda: np.zeros(0, np.int64))
    owner: NDArray[np.int64] = field(default_factory=lambda: np.zeros(0, np.int64))
    #: What the model asked for, aligned to ``path_ids``. Uniform until told.
    intended: NDArray[np.float64] = field(default_factory=lambda: np.zeros(0, np.float64))
    #: What the hosts actually did, aligned to ``path_ids``.
    counts: NDArray[np.int64] = field(default_factory=lambda: np.zeros(0, np.int64))
    #: The compliant half of ``counts``, kept apart because a population that
    #: reconsiders slowly carries its compliant placement across steps and the
    #: defectors' placement is redecided from scratch every step.
    compliant: NDArray[np.int64] = field(default_factory=lambda: np.zeros(0, np.int64))
    #: Live population. Moves only if the params say it churns.
    n_live: int = 0
    #: Fraction of the last advisory whose paths no longer exist. Nonzero means
    #: the model is advising about a world that has been re-signed out from
    #: under it -- under ``crypto_bound`` that is every refresh cycle.
    stale_weight: float = 0.0
    #: Simulated time the current advisory was applied, or -inf if none was.
    advised_at_s: float = float("-inf")
    n_resamples: int = 0

    @property
    def key(self) -> tuple[int, int]:
        return (self.src, self.dst)

    @property
    def n_paths(self) -> int:
        return len(self.path_ids)

    @property
    def offered_mbps(self) -> float:
        return self.n_live * self.params.mbps_per_host

    def realised(self) -> dict[int, float]:
        """Realised share per path id. Sums to 1 while anybody is sending."""
        total = int(self.counts.sum())
        if total <= 0:
            return {}
        return {pid: float(c) / total for pid, c in zip(self.path_ids, self.counts, strict=True)}

    def intended_shares(self) -> dict[int, float]:
        return dict(zip(self.path_ids, (float(w) for w in self.intended), strict=True))

    def deviation(self) -> float:
        """Largest gap between what was asked for and what happened.

        The quantity the ``1/sqrt(N)`` criterion is stated about. With a
        compliant population it is sampling noise; with defectors it is not,
        and telling those apart is the point of measuring it.
        """
        if self.n_paths == 0 or self.counts.sum() <= 0:
            return 0.0
        realised = self.counts / self.counts.sum()
        return float(np.abs(realised - self.intended).max())

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "n_paths": self.n_paths,
            "n_live": self.n_live,
            "offered_mbps": round(self.offered_mbps, 6),
            "stale_weight": round(self.stale_weight, 6),
            "deviation": round(self.deviation(), 6),
            "advised_at_s": self.advised_at_s if np.isfinite(self.advised_at_s) else None,
            "n_resamples": self.n_resamples,
        }


class HostPopulation:
    """Every scope's hosts, and the load they put on the network.

    Owned by :class:`~scionarena.core.scenario.Substrate`, which steps it
    between the segments and the link state: hosts have to write their demand
    before anything asks the link state what it costs, or every scope reads the
    previous step's congestion and the loop runs a step behind itself.
    """

    def __init__(
        self,
        topology: Topology,
        *,
        seed: int = 0,
        params: HostParams | None = None,
        identity_policy: str = "structural",
    ) -> None:
        self.topology = topology
        self.seed = seed
        self.defaults = params or HostParams()
        self.identity_policy = identity_policy
        self.scopes: dict[tuple[int, int], ScopeState] = {}
        self._offered = np.zeros(topology.n_ifaces, dtype=np.float64)
        self._step = 0
        #: Milliseconds since the start of the run, as an integer. Every draw is
        #: keyed on this rather than on a step counter, so a population reaches
        #: the same instant with the same sample however it got there -- a model
        #: whose probes cost it three seconds does not thereby reroll the hosts.
        self._stamp = 0
        #: Scopes whose population changed hands this step, for the report.
        self.n_defector_moves = 0
        self._shares: dict[int, float] | None = None
        self._shares_at = -1

    # ------------------------------------------------------------------ sizes

    @property
    def n_scopes(self) -> int:
        return len(self.scopes)

    @property
    def n_hosts(self) -> int:
        return sum(s.n_live for s in self.scopes.values())

    def __repr__(self) -> str:
        return (
            f"HostPopulation({self.n_scopes} scopes, {self.n_hosts} hosts, "
            f"{self._offered.sum() / 1000.0:.1f} Gbps offered)"
        )

    # ----------------------------------------------------------------- scopes

    def add_scope(
        self,
        src: int,
        dst: int,
        paths: Sequence[Path],
        *,
        params: HostParams | None = None,
    ) -> ScopeState:
        """Register a scope, or replace the one that was there.

        The path set is a snapshot. It goes stale the way everything else does;
        :meth:`refresh` is what a caller uses when the path server has changed
        its mind, and it deliberately does not happen on its own.
        """
        scope = ScopeState(src=src, dst=dst, params=params or self.defaults)
        scope.n_live = scope.params.n_hosts
        self.scopes[(src, dst)] = scope
        self._install_paths(scope, paths)
        return scope

    def scope(self, src: int, dst: int) -> ScopeState | None:
        return self.scopes.get((src, dst))

    def _install_paths(self, scope: ScopeState, paths: Sequence[Path]) -> None:
        """Flatten a path set into the arrays the hot loop uses.

        Egress interfaces only, matching :meth:`LinkState.path_metrics`: a path
        of h hops crosses h links and occupies one direction of each.
        """
        previous = scope.intended_shares()
        self._shares_at = -1  # the ids just changed under the cache
        scope.path_ids = tuple(p.path_id(self.identity_policy) for p in paths)
        if paths:
            per_path = [np.asarray(p.ifaces[::2], dtype=np.int64) for p in paths]
            scope.ifaces = np.concatenate(per_path) if per_path else np.zeros(0, np.int64)
            scope.owner = np.repeat(
                np.arange(len(paths), dtype=np.int64),
                np.array([a.size for a in per_path], dtype=np.int64),
            )
        else:
            scope.ifaces = np.zeros(0, np.int64)
            scope.owner = np.zeros(0, np.int64)
        scope.counts = np.zeros(scope.n_paths, dtype=np.int64)
        scope.compliant = np.zeros(scope.n_paths, dtype=np.int64)
        scope.intended = self._carry_over(scope, previous)

    def _carry_over(self, scope: ScopeState, previous: Mapping[int, float]) -> NDArray[np.float64]:
        """Re-align an advisory onto a path set that has moved under it.

        Weight naming paths that are gone is lost, and how much was lost is
        recorded rather than renormalised away: under ``crypto_bound`` a
        re-signing renames every path and the whole advisory evaporates, which
        is the identity-amnesia failure made physical.
        """
        if scope.n_paths == 0:
            scope.stale_weight = 1.0 if previous else 0.0
            return np.zeros(0, dtype=np.float64)
        if not previous:
            scope.stale_weight = 0.0
            return np.full(scope.n_paths, 1.0 / scope.n_paths, dtype=np.float64)
        kept = np.array([previous.get(pid, 0.0) for pid in scope.path_ids], dtype=np.float64)
        total_before = sum(max(0.0, w) for w in previous.values())
        survived = float(kept.sum())
        scope.stale_weight = 0.0 if total_before <= 0.0 else max(0.0, 1.0 - survived / total_before)
        if survived <= 0.0:
            return np.full(scope.n_paths, 1.0 / scope.n_paths, dtype=np.float64)
        return kept / survived

    def refresh(self, src: int, dst: int, paths: Sequence[Path]) -> ScopeState | None:
        """Point a scope at a new path set, carrying over what still applies."""
        scope = self.scopes.get((src, dst))
        if scope is None:
            return None
        self._install_paths(scope, paths)
        return scope

    # --------------------------------------------------------------- advisory

    def publish(
        self,
        src: int,
        dst: int,
        weights: Mapping[int, float],
        *,
        t: float = 0.0,
    ) -> ScopeState | None:
        """Install a distribution. Already delayed by whoever called this.

        The delay lives on the clock, not here: :class:`Substrate` schedules
        this for ``now + decision_latency`` so that an advisory cannot be
        applied to the state it was computed from. See ADR 0009.
        """
        scope = self.scopes.get((src, dst))
        if scope is None or scope.n_paths == 0:
            return None
        vector = np.array([max(0.0, float(weights.get(pid, 0.0))) for pid in scope.path_ids])
        asked = sum(max(0.0, float(w)) for w in weights.values())
        landed = float(vector.sum())
        scope.stale_weight = 0.0 if asked <= 0.0 else max(0.0, 1.0 - landed / asked)
        if landed <= 0.0:
            # Every path named is one this scope does not have. Uniform is the
            # honest fallback: the hosts have to send somewhere, and pretending
            # the advisory applied would hide the failure.
            scope.intended = np.full(scope.n_paths, 1.0 / scope.n_paths)
        else:
            scope.intended = vector / landed
        scope.advised_at_s = t
        return scope

    # ------------------------------------------------------------------ steps

    def step(self, t: float, dt_s: float, links: LinkState) -> NDArray[np.float64]:
        """Churn, resample, and return the offered load per interface.

        Called once per substrate step, before the link state's metrics are
        read. Defectors decide from ``links`` as it is *now*, which is the state
        the previous step produced -- what a host could have measured, not what
        its own move is about to cause.
        """
        self._step += 1
        self._stamp = int(round(t * 1000.0))
        self.n_defector_moves = 0
        self._offered = np.zeros(links.n_ifaces, dtype=np.float64)
        cost = links.cost() if self._needs_cost() else None
        for index, scope in enumerate(sorted(self.scopes.values(), key=lambda s: s.key)):
            self._churn(scope, index, dt_s)
            self._resample(scope, index, t, dt_s, cost)
            self._accumulate(scope)
        return self._offered

    def _needs_cost(self) -> bool:
        return any(s.params.defector_fraction > 0.0 for s in self.scopes.values())

    def _rng(self, index: int, salt: int) -> np.random.Generator:
        """Counter-based, so a draw depends on when it is, not on what was asked."""
        return np.random.default_rng([self.seed, 0x405, index, self._stamp, salt])

    def _churn(self, scope: ScopeState, index: int, dt_s: float) -> None:
        p = scope.params
        if p.arrivals_per_s <= 0.0 and p.departures_per_s <= 0.0:
            return
        rng = self._rng(index, 1)
        leaving = rng.binomial(scope.n_live, min(1.0, p.departures_per_s * dt_s))
        joining = rng.poisson(p.arrivals_per_s * dt_s)
        scope.n_live = max(0, scope.n_live - int(leaving) + int(joining))

    def _resample(
        self,
        scope: ScopeState,
        index: int,
        t: float,
        dt_s: float,
        cost: NDArray[np.float64] | None,
    ) -> None:
        if scope.n_paths == 0 or scope.n_live <= 0:
            scope.counts = np.zeros(scope.n_paths, dtype=np.int64)
            scope.compliant = scope.counts.copy()
            return
        rng = self._rng(index, 2)
        n_defect = int(scope.n_live * scope.params.defector_fraction)
        n_comply = scope.n_live - n_defect

        # A population that reconsiders every 30 s does not all reconsider at
        # once; a fraction of it does, every step. Modelling it as "everyone,
        # rarely" would put a sawtooth in the load that no host chose.
        fraction = min(1.0, dt_s / scope.params.resample_s)
        if fraction >= 1.0:
            compliant = rng.multinomial(n_comply, scope.intended).astype(np.int64)
        else:
            movers = int(rng.binomial(n_comply, fraction))
            held = self._thin(scope.compliant, n_comply - movers, rng)
            compliant = held + rng.multinomial(movers, scope.intended).astype(np.int64)

        counts = compliant.copy()
        if n_defect > 0 and cost is not None:
            # Redecided from scratch every step: a defector is not loyal to the
            # path it took last time, it is loyal to whatever is cheapest now,
            # and that is what makes a defector population a stampede.
            counts[self._greedy_path(scope, cost)] += n_defect
            self.n_defector_moves += n_defect

        scope.compliant = compliant
        scope.counts = counts
        scope.n_resamples += 1

    def _thin(
        self, counts: NDArray[np.int64], keep: int, rng: np.random.Generator
    ) -> NDArray[np.int64]:
        """Keep ``keep`` of the hosts already placed, in their current proportions."""
        total = int(counts.sum())
        if keep <= 0 or total <= 0:
            return np.zeros(counts.size, dtype=np.int64)
        if keep >= total:
            return counts.copy()
        return rng.multinomial(keep, counts / total).astype(np.int64)

    def _greedy_path(self, scope: ScopeState, cost: NDArray[np.float64]) -> int:
        """The cheapest path by the link state a host could have measured.

        Ties break on the lowest index, deterministically, because a defector
        population that broke ties at random would smear its own stampede and
        look better behaved than it is.
        """
        per_path = np.bincount(scope.owner, weights=cost[scope.ifaces], minlength=scope.n_paths)
        return int(np.argmin(per_path))

    def _accumulate(self, scope: ScopeState) -> None:
        if scope.n_paths == 0 or scope.ifaces.size == 0:
            return
        mbps = scope.counts.astype(np.float64) * scope.params.mbps_per_host
        np.add.at(self._offered, scope.ifaces, mbps[scope.owner])

    # ------------------------------------------------------------------ views

    def offered(self) -> NDArray[np.float64]:
        """Per-interface Mbps from hosts alone. Read-only by convention."""
        return self._offered

    def shares(self) -> dict[int, float]:
        """Realised share per path id across every scope.

        Keyed by path id rather than by scope because that is how the exposure
        layer asks: it knows path ids and deliberately does not know which
        scope the substrate filed them under.

        Cached per step. The exposure layer asks once per tool call and the
        answer only changes when the population moves, and at a hundred scopes
        rebuilding the dictionary on every question was a fifth of the run.
        """
        if self._shares_at == self._step and self._shares is not None:
            return self._shares
        out: dict[int, float] = {}
        for scope in self.scopes.values():
            out.update(scope.realised())
        self._shares, self._shares_at = out, self._step
        return out

    def carrying(self) -> set[int]:
        """Path ids with at least one host on them."""
        return {
            pid
            for scope in self.scopes.values()
            for pid, count in zip(scope.path_ids, scope.counts, strict=True)
            if count > 0
        }

    def summary(self) -> dict[str, Any]:
        return {
            "scopes": self.n_scopes,
            "hosts": self.n_hosts,
            "offered_gbps": round(float(self._offered.sum()) / 1000.0, 6),
            "defector_moves": self.n_defector_moves,
            "max_deviation": round(
                max((s.deviation() for s in self.scopes.values()), default=0.0), 6
            ),
            "stale_weight": round(
                max((s.stale_weight for s in self.scopes.values()), default=0.0), 6
            ),
        }

    # ------------------------------------------------------------ determinism

    def digest(self) -> str:
        return (
            TraceHash(label="hosts")
            .update(
                {
                    "seed": self.seed,
                    "policy": self.identity_policy,
                    "step": self._step,
                    "scopes": [
                        {
                            "key": list(scope.key),
                            "paths": list(scope.path_ids),
                            "counts": scope.counts.tolist(),
                            "intended": [round(float(w), 9) for w in scope.intended],
                            "n_live": scope.n_live,
                        }
                        for scope in sorted(self.scopes.values(), key=lambda s: s.key)
                    ],
                }
            )
            .short(32)
        )


def concentration_bound(intended: Iterable[float], n_hosts: int, sigmas: float = 4.0) -> float:
    """The ``1/sqrt(N)`` envelope the realised split is expected to sit inside.

    The binomial standard deviation of the worst path's share, times a
    tolerance. Stated as a function rather than a literal in a test because it
    is a claim about the world -- independent sampling concentrates at this
    rate -- and a test that hardcoded 0.05 would pass for the wrong reason.
    """
    if n_hosts <= 0:
        return 1.0
    worst = max((p * (1.0 - p) for p in intended), default=0.25)
    return sigmas * float(np.sqrt(worst / n_hosts))
