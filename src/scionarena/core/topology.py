"""The static graph: ASes, ISDs, interfaces, inter-AS links, relationships.

**Immutable after construction.** The physical inter-AS topology does not churn
(ADR 0002). Real topology change is rare, planned, and modelled as an explicit
scheduled event, which here means constructing a new :class:`Topology` through
:meth:`Topology.without_links` rather than mutating this one. Every array is
marked read-only so that "immutable" is enforced by numpy rather than
remembered by a contributor.

Everything is array-backed and indexed by integer id. String identifiers live in
a side table and are used only at the exposure boundary. At the ``realistic``
tier this is 2,000 ASes and 10,000 links; a Python object per link would be two
orders of magnitude more memory and would show up as a step-time regression
rather than as an obvious mistake.

Layout::

    AS       0 .. n_ases-1
    link     0 .. n_links-1        stored once, undirected
    iface    2*link, 2*link + 1    side 0 is link_a's end, side 1 is link_b's

For a parent-child link, ``link_a`` is the **provider** and ``link_b`` the
**customer**. That orientation is what makes up-segment and down-segment
construction a directed walk in :mod:`scionarena.core.segments`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "Topology",
    "REL_CORE",
    "REL_PARENT_CHILD",
    "REL_PEER",
    "REL_NAMES",
    "ROLE_CORE",
    "ROLE_PROVIDER",
    "ROLE_CUSTOMER",
    "ROLE_PEER",
    "ROLE_NAMES",
    "synthetic",
]

# Relationship of a link, read from link_a's side.
REL_CORE: Final = 0
REL_PARENT_CHILD: Final = 1  # link_a is the provider, link_b the customer
REL_PEER: Final = 2
REL_NAMES: Final = ("core", "parent_child", "peering")

# Role of a neighbour, read from the AS doing the asking. This is the view
# segment construction needs: "up" is towards providers, "down" towards
# customers, and a path may not go up again once it has come down.
ROLE_CORE: Final = 0
ROLE_PROVIDER: Final = 1  # the neighbour is my provider; going there is going up
ROLE_CUSTOMER: Final = 2
ROLE_PEER: Final = 3
ROLE_NAMES: Final = ("core", "provider", "customer", "peer")


def _freeze(*arrays: np.ndarray) -> None:
    for arr in arrays:
        arr.flags.writeable = False


@dataclass(frozen=True)
class Topology:
    """A static SCION-shaped inter-AS graph.

    Construct through a generator (:func:`synthetic`, ``from_caida``,
    ``from_dqnsim``) rather than directly; the generators are responsible for
    the invariants that :meth:`validate` checks.
    """

    # --- AS table -----------------------------------------------------------
    as_isd: NDArray[np.int32]
    as_is_core: NDArray[np.bool_]
    as_names: tuple[str, ...]

    # --- link table ---------------------------------------------------------
    link_a: NDArray[np.int32]
    link_b: NDArray[np.int32]
    link_rel: NDArray[np.int8]
    link_capacity_mbps: NDArray[np.float32]
    link_latency_ms: NDArray[np.float32]
    link_mtu: NDArray[np.int32]

    # --- CSR adjacency, both directions, sorted by AS -----------------------
    adj_indptr: NDArray[np.int64]
    adj_neighbour: NDArray[np.int32]
    adj_iface: NDArray[np.int32]
    adj_link: NDArray[np.int32]
    adj_role: NDArray[np.int8]

    seed: int = 0
    generator: str = "unknown"

    _name_to_index: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------ sizes

    @property
    def n_ases(self) -> int:
        return int(self.as_isd.shape[0])

    @property
    def n_links(self) -> int:
        return int(self.link_a.shape[0])

    @property
    def n_ifaces(self) -> int:
        return 2 * self.n_links

    @property
    def n_isds(self) -> int:
        return int(self.as_isd.max()) + 1 if self.n_ases else 0

    @property
    def nbytes(self) -> int:
        """Bytes held by the arrays. What the memory budget is stated against."""
        return sum(
            int(arr.nbytes)
            for arr in (
                self.as_isd,
                self.as_is_core,
                self.link_a,
                self.link_b,
                self.link_rel,
                self.link_capacity_mbps,
                self.link_latency_ms,
                self.link_mtu,
                self.adj_indptr,
                self.adj_neighbour,
                self.adj_iface,
                self.adj_link,
                self.adj_role,
            )
        )

    # ------------------------------------------------------------- name table

    def as_index(self, name: str) -> int:
        """String AS identifier to integer index. Exposure boundary only."""
        try:
            return self._name_to_index[name]
        except KeyError:
            raise KeyError(f"no such AS: {name!r}") from None

    def as_name(self, index: int) -> str:
        return self.as_names[index]

    def iface_name(self, iface: int) -> str:
        """``<AS>#<link>.<side>``. Stable, and readable in a trace."""
        link, side = divmod(int(iface), 2)
        owner = self.link_a[link] if side == 0 else self.link_b[link]
        return f"{self.as_names[int(owner)]}#{link}.{side}"

    # ------------------------------------------------------------- adjacency

    def degree(self, a: int) -> int:
        return int(self.adj_indptr[a + 1] - self.adj_indptr[a])

    def neighbours(self, a: int) -> NDArray[np.int32]:
        lo, hi = self.adj_indptr[a], self.adj_indptr[a + 1]
        return self.adj_neighbour[lo:hi]

    def neighbours_by_role(self, a: int, role: int) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
        """Neighbouring ASes with the given role, and the local interface to each.

        Returns views into the CSR arrays where it can and a filtered copy where
        it cannot. Callers must not write to either; the arrays are read-only.
        """
        lo, hi = self.adj_indptr[a], self.adj_indptr[a + 1]
        mask = self.adj_role[lo:hi] == role
        return self.adj_neighbour[lo:hi][mask], self.adj_iface[lo:hi][mask]

    def providers(self, a: int) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
        return self.neighbours_by_role(a, ROLE_PROVIDER)

    def customers(self, a: int) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
        return self.neighbours_by_role(a, ROLE_CUSTOMER)

    def peers(self, a: int) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
        return self.neighbours_by_role(a, ROLE_PEER)

    def core_neighbours(self, a: int) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
        return self.neighbours_by_role(a, ROLE_CORE)

    # ---------------------------------------------------------------- ifaces

    def as_of_iface(self, iface: int) -> int:
        link, side = divmod(int(iface), 2)
        return int(self.link_a[link] if side == 0 else self.link_b[link])

    def link_of_iface(self, iface: int) -> int:
        return int(iface) // 2

    def far_iface(self, iface: int) -> int:
        """The interface on the other end of the same link."""
        return int(iface) ^ 1

    @property
    def core_ases(self) -> NDArray[np.int32]:
        return np.flatnonzero(self.as_is_core).astype(np.int32)

    def core_ases_of_isd(self, isd: int) -> NDArray[np.int32]:
        return np.flatnonzero(self.as_is_core & (self.as_isd == isd)).astype(np.int32)

    # ------------------------------------------------------ scheduled change

    def without_links(self, link_ids: NDArray[np.int32] | list[int]) -> Topology:
        """The only sanctioned way the physical graph changes: a new topology.

        A scheduled outage or a planned decommission builds one of these. The
        original is untouched, so a trace can hold both and say which was in
        force when.
        """
        keep = np.ones(self.n_links, dtype=bool)
        keep[np.asarray(link_ids, dtype=np.int64)] = False
        return _assemble(
            as_isd=self.as_isd.copy(),
            as_is_core=self.as_is_core.copy(),
            as_names=self.as_names,
            link_a=self.link_a[keep].copy(),
            link_b=self.link_b[keep].copy(),
            link_rel=self.link_rel[keep].copy(),
            link_capacity_mbps=self.link_capacity_mbps[keep].copy(),
            link_latency_ms=self.link_latency_ms[keep].copy(),
            link_mtu=self.link_mtu[keep].copy(),
            seed=self.seed,
            generator=f"{self.generator}+without_links",
        )

    # -------------------------------------------------------------- integrity

    def digest(self) -> str:
        """Content hash of the graph. Two runs with the same seed match here or
        the generator is not deterministic."""
        h = hashlib.sha256()
        for arr in (
            self.as_isd,
            self.as_is_core,
            self.link_a,
            self.link_b,
            self.link_rel,
            self.link_capacity_mbps,
            self.link_latency_ms,
            self.link_mtu,
        ):
            h.update(np.ascontiguousarray(arr).tobytes())
        return h.hexdigest()[:16]

    def validate(self) -> None:
        """Raise if an invariant a generator is responsible for is broken."""
        if self.link_a.shape != self.link_b.shape:
            raise ValueError("link endpoint arrays disagree in length")
        if self.n_links and int(self.link_a.max(initial=0)) >= self.n_ases:
            raise ValueError("link references an AS index that does not exist")
        if np.any(self.link_a == self.link_b):
            raise ValueError("self-loop: an AS cannot peer with itself")
        if int(self.adj_indptr[-1]) != 2 * self.n_links:
            raise ValueError("CSR adjacency does not cover every link end")
        pairs = np.stack(
            [np.minimum(self.link_a, self.link_b), np.maximum(self.link_a, self.link_b)]
        )
        if self.n_links and np.unique(pairs, axis=1).shape[1] != self.n_links:
            raise ValueError("duplicate link between the same pair of ASes")
        core = self.link_rel == REL_CORE
        if np.any(core & ~(self.as_is_core[self.link_a] & self.as_is_core[self.link_b])):
            raise ValueError("core link with a non-core endpoint")

    def __repr__(self) -> str:
        return (
            f"Topology({self.n_ases} ASes, {self.n_links} links, {self.n_isds} ISDs, "
            f"{self.nbytes / 1024**2:.1f} MiB, {self.generator}, seed={self.seed}, "
            f"{self.digest()})"
        )


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def _roles(
    link_a: NDArray[np.int32], link_b: NDArray[np.int32], link_rel: NDArray[np.int8]
) -> tuple[NDArray[np.int8], NDArray[np.int8]]:
    """Role of the far AS, seen from ``link_a`` and from ``link_b``.

    Only parent-child is asymmetric, and getting the asymmetry backwards would
    invert every up-segment in the substrate, so it is spelled out here once.
    """
    role_from_a = np.empty(link_rel.shape, dtype=np.int8)
    role_from_b = np.empty(link_rel.shape, dtype=np.int8)

    role_from_a[link_rel == REL_CORE] = ROLE_CORE
    role_from_b[link_rel == REL_CORE] = ROLE_CORE
    role_from_a[link_rel == REL_PEER] = ROLE_PEER
    role_from_b[link_rel == REL_PEER] = ROLE_PEER
    # link_a is the provider: from a the far end is a customer, from b it is a provider.
    role_from_a[link_rel == REL_PARENT_CHILD] = ROLE_CUSTOMER
    role_from_b[link_rel == REL_PARENT_CHILD] = ROLE_PROVIDER
    return role_from_a, role_from_b


def _assemble(
    *,
    as_isd: NDArray[np.int32],
    as_is_core: NDArray[np.bool_],
    as_names: tuple[str, ...],
    link_a: NDArray[np.int32],
    link_b: NDArray[np.int32],
    link_rel: NDArray[np.int8],
    link_capacity_mbps: NDArray[np.float32],
    link_latency_ms: NDArray[np.float32],
    link_mtu: NDArray[np.int32],
    seed: int,
    generator: str,
) -> Topology:
    """Build the CSR adjacency and freeze everything."""
    n_ases = int(as_isd.shape[0])
    n_links = int(link_a.shape[0])

    role_from_a, role_from_b = _roles(link_a, link_b, link_rel)
    link_ids = np.arange(n_links, dtype=np.int32)

    owner = np.concatenate([link_a, link_b])
    neighbour = np.concatenate([link_b, link_a])
    iface = np.concatenate([2 * link_ids, 2 * link_ids + 1]).astype(np.int32)
    link_of = np.concatenate([link_ids, link_ids])
    role = np.concatenate([role_from_a, role_from_b])

    # Sort by owning AS, then by role, then by neighbour. Deterministic, and it
    # puts every provider of an AS in one contiguous run.
    order = np.lexsort((neighbour, role, owner))
    owner_sorted = owner[order]

    counts = np.bincount(owner_sorted, minlength=n_ases)
    adj_indptr = np.zeros(n_ases + 1, dtype=np.int64)
    np.cumsum(counts, out=adj_indptr[1:])

    topo = Topology(
        as_isd=as_isd,
        as_is_core=as_is_core,
        as_names=as_names,
        link_a=link_a,
        link_b=link_b,
        link_rel=link_rel,
        link_capacity_mbps=link_capacity_mbps,
        link_latency_ms=link_latency_ms,
        link_mtu=link_mtu,
        adj_indptr=adj_indptr,
        adj_neighbour=neighbour[order].astype(np.int32),
        adj_iface=iface[order],
        adj_link=link_of[order].astype(np.int32),
        adj_role=role[order].astype(np.int8),
        seed=seed,
        generator=generator,
        _name_to_index={name: i for i, name in enumerate(as_names)},
    )
    _freeze(
        topo.as_isd,
        topo.as_is_core,
        topo.link_a,
        topo.link_b,
        topo.link_rel,
        topo.link_capacity_mbps,
        topo.link_latency_ms,
        topo.link_mtu,
        topo.adj_indptr,
        topo.adj_neighbour,
        topo.adj_iface,
        topo.adj_link,
        topo.adj_role,
    )
    return topo


# --------------------------------------------------------------------------
# the synthetic generator
# --------------------------------------------------------------------------

#: Fraction of ASes in an ISD that are core. Real ISDs are far more centralised
#: than a random graph; this is the knob that controls it.
CORE_FRACTION: Final = 0.04

#: Customer-provider depth below the core. Three levels is enough to produce
#: paths of realistic length without making the tree a chain.
N_LEVELS: Final = 3


def _as_name(isd: int, index: int) -> str:
    """SCION-shaped ``<ISD>-ff00:0:<hex>``. Cosmetic; the integer id is the truth."""
    return f"{isd + 1}-ff00:0:{index:x}"


def _dedup_pairs(
    a: NDArray[np.int64], b: NDArray[np.int64], n_ases: int
) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.int64]]:
    """Drop self-loops and duplicate pairs, keeping first occurrence order.

    Returns the deduplicated endpoints and the indices kept, so the caller can
    carry per-link attributes through.
    """
    keep = a != b
    a, b = a[keep], b[keep]
    kept_idx = np.flatnonzero(keep)
    lo = np.minimum(a, b)
    hi = np.maximum(a, b)
    key = lo * n_ases + hi
    _, first = np.unique(key, return_index=True)
    first.sort()  # np.unique sorts by key; restore insertion order
    return a[first], b[first], kept_idx[first]


def synthetic(
    *,
    n_ases: int,
    n_links: int,
    seed: int = 0,
    n_isds: int | None = None,
) -> Topology:
    """A parameterised SCION-shaped topology. Deterministic in ``seed``.

    Structure, in the order it is built:

    1. ASes are split into ISDs; a small fraction of each ISD is core.
    2. Core ASes of one ISD are meshed; ISD cores are linked in a ring with
       chords, which is what makes an inter-ISD path possible at all.
    3. Non-core ASes sit at one of :data:`N_LEVELS` levels and each attaches to
       one or two providers strictly above it. That guarantees connectivity and
       a cycle-free customer-provider graph.
    4. Peering links are added between ASes at the same level until the link
       target is met.

    This is not a model of how the internet grew. It is a graph with the
    structural properties path construction depends on -- a core, a
    customer-provider DAG, and peering shortcuts -- generated fast enough to
    build 20,000 ASes in a test.
    """
    if n_ases < 2:
        raise ValueError("a topology needs at least two ASes")
    rng = np.random.default_rng(seed)

    n_isds = max(1, round(n_ases / 250)) if n_isds is None else n_isds
    n_isds = max(1, min(n_isds, n_ases // 2))

    # 1. ISD assignment: contiguous blocks, so an ISD's ASes are a slice.
    as_isd = (np.arange(n_ases) * n_isds // n_ases).astype(np.int32)

    # Core ASes: the first few of each ISD block, at least two where the ISD is
    # big enough to have two, otherwise one.
    as_is_core = np.zeros(n_ases, dtype=bool)
    isd_start = np.searchsorted(as_isd, np.arange(n_isds))
    isd_size = np.bincount(as_isd, minlength=n_isds)
    n_core = np.maximum(1, np.minimum(isd_size, np.round(isd_size * CORE_FRACTION))).astype(int)
    n_core = np.where(isd_size >= 4, np.maximum(n_core, 2), n_core)
    for isd in range(n_isds):
        as_is_core[isd_start[isd] : isd_start[isd] + n_core[isd]] = True

    # Level of each non-core AS: 1..N_LEVELS, spread evenly through the block.
    level = np.zeros(n_ases, dtype=np.int8)
    non_core = np.flatnonzero(~as_is_core)
    if non_core.size:
        rank_in_isd = non_core - isd_start[as_isd[non_core]] - n_core[as_isd[non_core]]
        size_non_core = (isd_size - n_core)[as_isd[non_core]]
        level[non_core] = 1 + (rank_in_isd * N_LEVELS // np.maximum(1, size_non_core)).astype(
            np.int8
        )

    src_parts: list[NDArray[np.int64]] = []
    dst_parts: list[NDArray[np.int64]] = []
    rel_parts: list[NDArray[np.int8]] = []

    def add(src: NDArray[np.int64], dst: NDArray[np.int64], rel: int) -> None:
        src_parts.append(np.asarray(src, dtype=np.int64))
        dst_parts.append(np.asarray(dst, dtype=np.int64))
        rel_parts.append(np.full(len(src), rel, dtype=np.int8))

    # 2a. Intra-ISD core mesh. Full mesh where the core is small; where it is
    # not, a star on the ISD's first core AS plus a ring, because a full mesh of
    # 800 core ASes is 320k links and a bare ring has a diameter no bounded core
    # segment search can cross.
    cores_by_isd = [np.flatnonzero(as_is_core & (as_isd == i)) for i in range(n_isds)]
    for core in cores_by_isd:
        if core.size < 2:
            continue
        if core.size <= 8:
            i, j = np.triu_indices(core.size, k=1)
            add(core[i], core[j], REL_CORE)
        else:
            hub = np.full(core.size - 1, core[0])
            add(hub, core[1:], REL_CORE)
            add(core, np.roll(core, 1), REL_CORE)
            chords = rng.integers(0, core.size, size=(2, core.size))
            add(core[chords[0]], core[chords[1]], REL_CORE)

    # 2b. Inter-ISD core links. A ring over ISDs plus a chord across the
    # diameter and a random extra, so the number of ISD hops between any two
    # ISDs stays small. A bare ring over 80 ISDs would put two ISDs 40 core hops
    # apart, and every path between them would then fail to materialise -- which
    # looks exactly like a policy filter and is a bug, not a scenario.
    if n_isds > 1:
        hubs = np.array([c[0] for c in cores_by_isd if c.size], dtype=np.int64)
        for isd in range(n_isds):
            here = cores_by_isd[isd]
            if here.size == 0:
                continue
            for other in {(isd + 1) % n_isds, (isd + n_isds // 2) % n_isds}:
                there = cores_by_isd[other]
                if other == isd or there.size == 0:
                    continue
                k = min(3, here.size, there.size)
                add(
                    rng.choice(here, k, replace=False),
                    rng.choice(there, k, replace=False),
                    REL_CORE,
                )
        # Hub to hub, so the ISD-level graph has a short diameter regardless of
        # which core AS a segment happens to arrive at.
        if hubs.size > 2:
            add(hubs, np.roll(hubs, hubs.size // 2), REL_CORE)

    # 3. Customer-provider attachment, level by level.
    for lvl in range(1, N_LEVELS + 1):
        children = np.flatnonzero(level == lvl)
        if children.size == 0:
            continue
        # Providers are drawn from the level above, in the same ISD. Sampling
        # by rank within the parent pool keeps this vectorised.
        for isd in range(n_isds):
            kids = children[as_isd[children] == isd]
            if kids.size == 0:
                continue
            pool = np.flatnonzero((as_isd == isd) & ((level == lvl - 1) if lvl > 1 else as_is_core))
            if pool.size == 0:
                pool = np.flatnonzero((as_isd == isd) & as_is_core)
            if pool.size == 0:
                continue
            first = pool[rng.integers(0, pool.size, size=kids.size)]
            add(first, kids, REL_PARENT_CHILD)
            # Multi-homing. This is the knob that sets how many distinct
            # up-segments an AS has, and therefore how many end-to-end paths a
            # (src, dst) scope resolves to. Single-homing everywhere yields a
            # handful of paths per pair; the realistic tier wants 100-300.
            # ASSUMPTION(Q5): the degree distribution is plausible rather than
            # fitted to CAIDA. from_caida() will replace it with measurement.
            for probability in (0.8, 0.55, 0.3):
                multi = kids[rng.random(kids.size) < probability]
                if multi.size:
                    add(pool[rng.integers(0, pool.size, size=multi.size)], multi, REL_PARENT_CHILD)
            # Some ASes buy transit straight from the core, skipping a level.
            # Real stubs do this constantly and it shortens up-segments.
            if lvl > 1:
                direct = kids[rng.random(kids.size) < 0.4]
                if direct.size:
                    core_pool = np.flatnonzero((as_isd == isd) & as_is_core)
                    if core_pool.size:
                        add(
                            core_pool[rng.integers(0, core_pool.size, size=direct.size)],
                            direct,
                            REL_PARENT_CHILD,
                        )

    src = np.concatenate(src_parts) if src_parts else np.zeros(0, dtype=np.int64)
    dst = np.concatenate(dst_parts) if dst_parts else np.zeros(0, dtype=np.int64)
    rel = np.concatenate(rel_parts) if rel_parts else np.zeros(0, dtype=np.int8)
    src, dst, kept = _dedup_pairs(src, dst, n_ases)
    rel = rel[kept]

    # 4. Peering to reach the link target. Peers are same-level ASes, which is
    # where peering actually happens; a customer does not peer with a tier-1.
    # Sampling within a level rather than filtering across all ASes matters:
    # filtering discards the great majority of candidates and the generator
    # then undershoots the target by a third.
    pools = [np.flatnonzero(level == lvl) for lvl in range(1, N_LEVELS + 1)]
    pools = [p for p in pools if p.size >= 2]
    for _ in range(4):  # a few rounds; each is a vectorised batch, not a retry loop
        shortfall = n_links - src.size
        if shortfall <= 0 or not pools:
            break
        weights = np.array([p.size for p in pools], dtype=float)
        weights /= weights.sum()
        p_src, p_dst = [src], [dst]
        p_rel = [rel]
        for pool, share in zip(pools, weights, strict=True):
            want = int(shortfall * share * 1.5) + 8
            a = pool[rng.integers(0, pool.size, size=want)]
            b = pool[rng.integers(0, pool.size, size=want)]
            p_src.append(a)
            p_dst.append(b)
            p_rel.append(np.full(a.size, REL_PEER, dtype=np.int8))
        cand_src = np.concatenate(p_src)
        cand_dst = np.concatenate(p_dst)
        cand_rel = np.concatenate(p_rel)
        cand_src, cand_dst, kept = _dedup_pairs(cand_src, cand_dst, n_ases)
        cand_rel = cand_rel[kept]
        take = min(cand_src.size, n_links)
        src, dst, rel = cand_src[:take], cand_dst[:take], cand_rel[:take]

    n = src.size
    link_a = src.astype(np.int32)
    link_b = dst.astype(np.int32)

    # Attributes. Core links are fat and long-haul; customer links are thin.
    capacity = np.select(
        [rel == REL_CORE, rel == REL_PEER],
        [
            rng.choice(np.array([40_000.0, 100_000.0, 400_000.0], dtype=np.float32), size=n),
            rng.choice(np.array([10_000.0, 40_000.0], dtype=np.float32), size=n),
        ],
        default=rng.choice(np.array([1_000.0, 10_000.0], dtype=np.float32), size=n),
    ).astype(np.float32)

    inter_isd = as_isd[link_a] != as_isd[link_b]
    latency = np.where(
        inter_isd,
        rng.uniform(8.0, 45.0, size=n),
        rng.uniform(0.5, 12.0, size=n),
    ).astype(np.float32)

    mtu = rng.choice(np.array([1400, 1472, 8900], dtype=np.int32), size=n).astype(np.int32)

    names = tuple(_as_name(int(as_isd[i]), i) for i in range(n_ases))

    topo = _assemble(
        as_isd=as_isd,
        as_is_core=as_is_core,
        as_names=names,
        link_a=link_a,
        link_b=link_b,
        link_rel=rel.astype(np.int8),
        link_capacity_mbps=capacity,
        link_latency_ms=latency,
        link_mtu=mtu,
        seed=seed,
        generator=f"synthetic(n_ases={n_ases}, n_links={n_links})",
    )
    topo.validate()
    return topo
