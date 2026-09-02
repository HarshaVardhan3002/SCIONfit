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

from collections import deque
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

    # ------------------------------------------------ the mechanism ladder
    # ADR 0015. Every default below is the behaviour that existed before the
    # rung was added, so a scenario that does not ask for discipline gets the
    # population M3 and M4 were measured against, draw for draw.

    #: Rung 3, and the paths-per-selector axis: consider at most this many of
    #: the advised paths, by weight. ``None`` is "all of them".
    k_paths: int | None = None
    #: Rung 3: and only those weighing at least this fraction of the best one.
    #: 0.0 keeps everything; 1.0 keeps the argmax alone.
    eps_set: float = 0.0
    #: Rung 3: switch only if the destination outweighs the incumbent by this
    #: much. In units of advised weight, so 0.05 is five points of share.
    hysteresis: float = 0.0
    #: Rung 3: having switched, a host will not switch again for this long.
    dwell_s: float = 0.0
    #: Rung 3: 1.0 is independent timers -- every host reconsiders with
    #: probability ``dt / resample_s`` each step, which is what the population
    #: has always done. 0.0 is the synchronised herd: the whole scope
    #: reconsiders together when the clock crosses a multiple of ``resample_s``.
    timer_jitter: float = 1.0
    #: Rung 4, the jittered mirror: seconds over which a newly published
    #: advisory reaches the population. 0.0 is instantly and simultaneously.
    mirror_jitter_s: float = 0.0

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
        if self.k_paths is not None and self.k_paths < 1:
            raise ValueError(f"k_paths must be at least 1 or None, got {self.k_paths}")
        if not 0.0 <= self.eps_set <= 1.0:
            raise ValueError(f"eps_set must be in [0, 1], got {self.eps_set}")
        if self.hysteresis < 0.0:
            raise ValueError(f"hysteresis must not be negative, got {self.hysteresis}")
        if self.dwell_s < 0.0:
            raise ValueError(f"dwell_s must not be negative, got {self.dwell_s}")
        if not 0.0 <= self.timer_jitter <= 1.0:
            raise ValueError(f"timer_jitter must be in [0, 1], got {self.timer_jitter}")
        if self.mirror_jitter_s < 0.0:
            raise ValueError(f"mirror_jitter_s must not be negative, got {self.mirror_jitter_s}")
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
    def disciplined(self) -> bool:
        """Does this population do anything a rung-3 selector does?

        The whole ladder is behind this: a default population must take the
        same branches, and cost the same, as it did before ADR 0015.
        """
        return (
            self.k_paths is not None
            or self.eps_set > 0.0
            or self.hysteresis > 0.0
            or self.dwell_s > 0.0
            or self.timer_jitter < 1.0
        )

    @property
    def offered_mbps(self) -> float:
        """What the whole population offers, if every host is up."""
        return self.n_hosts * self.mbps_per_host


def _considered(weights: NDArray[np.float64], params: HostParams) -> NDArray[np.float64]:
    """The advised distribution, cut down to what a selector will consider.

    ``eps_set`` and ``k_paths`` are one operation (ADR 0015): a mask on the
    advisory, renormalised. It is applied to the *advisory* and never to the
    path set, so the mass a disciplined selector refuses to use shows up in
    ``deviation()`` rather than being renormalised out of existence.
    """
    if weights.size == 0 or (params.k_paths is None and params.eps_set <= 0.0):
        return weights
    kept = weights.astype(np.float64, copy=True)
    best = float(kept.max()) if kept.size else 0.0
    if params.eps_set > 0.0 and best > 0.0:
        kept[kept < params.eps_set * best] = 0.0
    if params.k_paths is not None and params.k_paths < kept.size:
        # Stable, so ties fall to the lower path index and two runs agree.
        kept[np.argsort(-kept, kind="stable")[params.k_paths :]] = 0.0
    total = float(kept.sum())
    # Nothing survived the mask -- every weight was zero. A host still has to
    # send somewhere, so it considers everything, exactly as ``publish`` does.
    return kept / total if total > 0.0 else weights


def _switch(
    movers_by_src: NDArray[np.int64],
    weights: NDArray[np.float64],
    hysteresis: float,
    rng: np.random.Generator,
) -> tuple[NDArray[np.int64], NDArray[np.int64]]:
    """Move hosts that clear the margin. Returns ``(stayed, arrived)``.

    A mover on path *i* accepts a destination *j* only if
    ``w[j] > w[i] + hysteresis``. Under a threshold rule the acceptable set for
    *i* is a prefix of the paths sorted by descending weight, so one sort and
    one prefix sum settle every source at once: acceptance is a vectorised
    binomial, and only the destination draw needs a call per distinct prefix
    length. The obvious ``k x k`` acceptance matrix is 9 M entries at 300 paths
    and 100 scopes, per step. See ADR 0015.
    """
    order = np.argsort(-weights, kind="stable")
    descending = weights[order]
    prefix = np.cumsum(descending)
    # How many weights are strictly greater than w[i] + hysteresis. Negated so
    # the array searchsorted reads is ascending.
    lengths = np.searchsorted(-descending, -(weights + hysteresis), side="left")
    accept_p = np.where(lengths > 0, prefix[np.maximum(lengths - 1, 0)], 0.0)
    accepted = rng.binomial(movers_by_src, np.clip(accept_p, 0.0, 1.0)).astype(np.int64)

    arrived = np.zeros(weights.size, dtype=np.int64)
    for length in np.unique(lengths[accepted > 0]):
        n_moving = int(accepted[lengths == length].sum())
        if n_moving <= 0 or length <= 0:
            continue
        drawn = rng.multinomial(n_moving, descending[:length] / prefix[length - 1])
        np.add.at(arrived, order[:length], drawn.astype(np.int64))
    return movers_by_src - accepted, arrived


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
    #: Rung 4 (ADR 0015). What the part of the population that has not yet seen
    #: the current advisory is still sampling. Equal to ``intended`` whenever
    #: ``mirror_jitter_s`` is zero, which is the default.
    mirrored: NDArray[np.float64] = field(default_factory=lambda: np.zeros(0, np.float64))
    #: How much of the population the current advisory has reached, in [0, 1].
    mirror_frac: float = 1.0
    #: Rung 3: movers per step inside the dwell window, oldest first. Its sum is
    #: how many hosts are barred from moving now because they just moved.
    moved_recently: deque[int] = field(default_factory=deque)
    #: Simulated time the synchronised part of the population last reconsidered.
    last_sync_s: float = float("-inf")

    @property
    def key(self) -> tuple[int, int]:
        return (self.src, self.dst)

    @property
    def n_paths(self) -> int:
        return len(self.path_ids)

    @property
    def offered_mbps(self) -> float:
        return self.n_live * self.params.mbps_per_host

    @property
    def locked(self) -> int:
        """Hosts that moved inside the dwell window and so may not move now."""
        return int(sum(self.moved_recently))

    def blended(self) -> NDArray[np.float64]:
        """What the population believes, mid-mirror.

        ``mirror_frac`` of it has seen the current advisory and the rest is
        still on the one before. At ``mirror_jitter_s = 0`` the fraction is 1
        the instant the advisory lands and this is ``intended``.
        """
        if self.mirror_frac >= 1.0 or self.mirrored.size != self.intended.size:
            return self.intended
        return self.mirror_frac * self.intended + (1.0 - self.mirror_frac) * self.mirrored

    def effective(self) -> NDArray[np.float64]:
        """What the population actually samples: believed, then disciplined.

        The distribution ``concentration_bound`` is about. Feeding it
        ``intended`` instead would fail for a correct implementation the moment
        any rung-3 knob is on -- see ADR 0015.
        """
        return _considered(self.blended(), self.params)

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
            # What the population was doing, not only what it was told. A large
            # deviation with k_paths=1 is discipline, not a badly behaved model.
            "locked": self.locked,
            "mirror_frac": round(self.mirror_frac, 6),
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
        # The mirror does not survive a path set moving under it: the older
        # belief was over paths that may no longer exist, and carrying it would
        # blend two different worlds.
        scope.mirrored = scope.intended.copy()
        scope.mirror_frac = 1.0

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
        # What the population believes right now, before this advisory starts
        # reaching it. A second publication inside a mirror window blends from
        # where the population actually got to, not from the advisory before.
        believed = scope.blended().copy()
        if landed <= 0.0:
            # Every path named is one this scope does not have. Uniform is the
            # honest fallback: the hosts have to send somewhere, and pretending
            # the advisory applied would hide the failure.
            scope.intended = np.full(scope.n_paths, 1.0 / scope.n_paths)
        else:
            scope.intended = vector / landed
        scope.advised_at_s = t
        if scope.params.mirror_jitter_s > 0.0:
            scope.mirrored = believed
            scope.mirror_frac = 0.0
        else:
            scope.mirrored = scope.intended
            scope.mirror_frac = 1.0
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
        params = scope.params
        n_defect = int(scope.n_live * params.defector_fraction)
        n_comply = scope.n_live - n_defect

        self._advance_mirror(scope, t)
        target = scope.effective()

        if params.disciplined:
            compliant = self._disciplined(scope, rng, n_comply, target, t, dt_s)
        else:
            # A population that reconsiders every 30 s does not all reconsider at
            # once; a fraction of it does, every step. Modelling it as "everyone,
            # rarely" would put a sawtooth in the load that no host chose.
            fraction = min(1.0, dt_s / params.resample_s)
            if fraction >= 1.0:
                compliant = rng.multinomial(n_comply, target).astype(np.int64)
            else:
                movers = int(rng.binomial(n_comply, fraction))
                held = self._thin(scope.compliant, n_comply - movers, rng)
                compliant = held + rng.multinomial(movers, target).astype(np.int64)

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

    def _advance_mirror(self, scope: ScopeState, t: float) -> None:
        """How much of the population the current advisory has reached by now."""
        window = scope.params.mirror_jitter_s
        if window <= 0.0 or not np.isfinite(scope.advised_at_s):
            scope.mirror_frac = 1.0
            return
        scope.mirror_frac = float(np.clip((t - scope.advised_at_s) / window, 0.0, 1.0))

    def _disciplined(
        self,
        scope: ScopeState,
        rng: np.random.Generator,
        n_comply: int,
        target: NDArray[np.float64],
        t: float,
        dt_s: float,
    ) -> NDArray[np.int64]:
        """Rung 3. Who is allowed to reconsider, and what they do about it.

        Four knobs in the order they apply to one host: it may be locked by
        ``dwell_s`` from its last move; if not, it reconsiders on its own timer
        or with the herd, per ``timer_jitter``; it considers only the paths
        ``eps_set`` and ``k_paths`` left in ``target``; and it moves only if the
        destination clears ``hysteresis``. See ADR 0015.
        """
        params = scope.params
        placed = scope.compliant
        total_placed = int(placed.sum())
        eligible = max(0, n_comply - scope.locked)
        movers = self._movers(scope, rng, eligible, t, dt_s)

        if total_placed > 0:
            # Who reconsiders is drawn from where the population is, so the
            # movers' incumbents are known and hysteresis has something to
            # compare against. Clipped to what is actually on each path: the
            # draw is with replacement and a cell can otherwise overrun.
            by_source = np.minimum(
                rng.multinomial(movers, placed / total_placed).astype(np.int64), placed
            )
            held = placed - by_source
        else:
            # Nobody is anywhere yet, so there is no incumbent. Hosts that have
            # not reconsidered are not sending, exactly as an undisciplined
            # population's are not.
            by_source = np.zeros(scope.n_paths, dtype=np.int64)
            held = by_source.copy()

        moving = int(by_source.sum()) if total_placed > 0 else movers
        if params.hysteresis > 0.0 and total_placed > 0:
            stayed, arrived = _switch(by_source, target, params.hysteresis, rng)
        else:
            stayed = np.zeros(scope.n_paths, dtype=np.int64)
            arrived = rng.multinomial(moving, target).astype(np.int64)
        self._remember_movers(scope, int(arrived.sum()), dt_s)

        # A population that shrank cannot keep everyone it had placed. One that
        # grew does not place its new hosts until they reconsider, which is what
        # an undisciplined population does too.
        room = max(0, n_comply - moving)
        if int(held.sum()) > room:
            held = self._thin(held, room, rng)
        return held + stayed + arrived

    def _movers(
        self, scope: ScopeState, rng: np.random.Generator, eligible: int, t: float, dt_s: float
    ) -> int:
        """How many of the eligible reconsider this step.

        ``timer_jitter`` splits the population between independent timers --
        which is what it has always had -- and one shared timer that fires when
        the clock crosses a multiple of ``resample_s``. The synchronised end is
        a fleet restarted by one deploy, not a strawman.
        """
        params = scope.params
        if eligible <= 0:
            return 0
        independent = int(round(eligible * params.timer_jitter))
        fraction = min(1.0, dt_s / params.resample_s)
        movers = int(rng.binomial(independent, fraction))
        together = eligible - independent
        if together > 0 and self._sync_fires(scope, t, dt_s):
            movers += together
            scope.last_sync_s = t
        return movers

    @staticmethod
    def _sync_fires(scope: ScopeState, t: float, dt_s: float) -> bool:
        """Did the shared timer cross a multiple of ``resample_s`` this step?"""
        period = scope.params.resample_s
        if dt_s >= period:
            return True
        return int((t + 1e-9) // period) > int((t - dt_s + 1e-9) // period)

    def _remember_movers(self, scope: ScopeState, moved: int, dt_s: float) -> None:
        """Push this step's movers into the dwell ledger and age it.

        Length ``ceil(dwell_s / dt)``, so the sum is exactly the number of hosts
        that moved inside the window. Which individuals they are is not
        represented and does not need to be: hosts in a scope are exchangeable.
        """
        window = scope.params.dwell_s
        if window <= 0.0:
            scope.moved_recently.clear()
            return
        scope.moved_recently.append(int(moved))
        depth = max(1, int(np.ceil(window / dt_s)))
        while len(scope.moved_recently) > depth:
            scope.moved_recently.popleft()

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
                            # Ladder state (ADR 0015). Two runs can coincide on
                            # counts for a step while differing in how much of
                            # the population has seen the advisory, or in how
                            # much of it is barred from moving.
                            "mirror_frac": round(scope.mirror_frac, 9),
                            "locked": scope.locked,
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
