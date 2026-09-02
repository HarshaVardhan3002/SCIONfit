"""Beaconed path segments: construction, expiry, re-signing, identity.

This is where the corrected domain model lives (ADR 0002). The physical graph in
:mod:`scionarena.core.topology` does not change. What changes constantly is the
*beaconed segment*: it is signed, it carries an expiry, and it is periodically
re-beaconed with fresh cryptographic material describing **exactly the same
sequence of interfaces**.

Two identifiers, on every segment and on every path composed from segments::

    structural_id   hash of the ordered interface sequence — survives re-signing
    segment_id      tied to the current material     — changes on every refresh

A model that keys per-path memory on ``segment_id`` throws away everything it
knew on every refresh cycle while its outputs still look plausible. That is
identity amnesia, it is invisible from the outside, and probe R4 exists to catch
it. Which identifier ``path_id`` aliases at the exposure boundary is chosen by
``identity_policy``; **neither is the blessed default** until open question Q1 is
answered, so nothing here may assume one.

What is deliberately *not* modelled: beaconing message by message. A real
control plane floods PCBs hop by hop several times a minute. Simulating that
buys nothing a model can observe. The process is abstracted; its consequences —
segments appear, expire, are replaced, and can be filtered away by policy — are
not.

Scale. Up-segments are built once per AS at construction, because that is what
beaconing genuinely does and it is bounded work. Core segments and end-to-end
paths are **materialised lazily per scope and cached**, because at the
``realistic`` tier there are two million ordered AS pairs and nobody enumerates
them.
"""

from __future__ import annotations

import hashlib
import struct
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Final, Self

import numpy as np
from numpy.typing import NDArray

from .tiers import Tier
from .topology import ROLE_CORE, ROLE_PEER, ROLE_PROVIDER, Topology

__all__ = [
    "Segment",
    "Path",
    "SegmentStore",
    "BeaconPolicy",
    "FilterPolicy",
    "IdentityPolicy",
    "SEG_UP",
    "SEG_CORE",
    "SEG_DOWN",
    "SEG_NAMES",
    "structural_hash",
]

SEG_UP: Final = 0
SEG_CORE: Final = 1
SEG_DOWN: Final = 2
SEG_NAMES: Final = ("up", "core", "down")

#: ``path_id`` aliases the structural identifier, or the crypto-bound one.
IdentityPolicy = str  # "structural" | "crypto_bound"
IDENTITY_POLICIES: Final = ("structural", "crypto_bound")


def structural_hash(ifaces: Sequence[int]) -> int:
    """Stable 64-bit hash of an ordered interface sequence.

    Not :func:`hash`: that is salted per process, so a structural id built from
    it would differ between two runs of the same seed and silently break
    invariant 4.
    """
    digest = hashlib.blake2b(
        np.asarray(ifaces, dtype=np.int32).tobytes(), digest_size=8, person=b"scion-st"
    )
    return int.from_bytes(digest.digest(), "big")


def _material_hash(structural_id: int, generation: int, signature_id: int) -> int:
    digest = hashlib.blake2b(
        structural_id.to_bytes(8, "big")
        + generation.to_bytes(4, "big")
        + signature_id.to_bytes(8, "big"),
        digest_size=8,
        person=b"scion-sg",
    )
    return int.from_bytes(digest.digest(), "big")


def _filter_draw(salt: int, structural_id: int) -> float:
    """A stable uniform draw in [0, 1) for one segment under one filter policy.

    Keyed on the *structural* id, so a segment keeps its decision when it is
    re-signed, and a segment composed later gets the decision it would have got
    had it existed when the policy was installed. Keying on the segment id
    would give neither property.
    """
    digest = hashlib.blake2b(
        salt.to_bytes(8, "big") + structural_id.to_bytes(8, "big"),
        digest_size=8,
        person=b"scion-fd",
    )
    return int.from_bytes(digest.digest(), "big") / 2.0**64


@dataclass(frozen=True)
class BeaconPolicy:
    """How often segments are re-beaconed and how long they live.

    ``ASSUMPTION(Q1)``: these intervals are plausible rather than measured. The
    substrate's job is that *something* refreshes on a schedule, so a model can
    be tested against refresh; the exact period is a scenario parameter.
    """

    #: seconds between re-beaconing rounds
    interval_s: float = 300.0
    #: how long a freshly signed segment remains valid
    lifetime_s: float = 21_600.0  # 6 h, the usual SCION segment maximum
    #: fraction of ``interval_s`` by which a segment's refresh is spread out, so
    #: the whole world does not re-sign on the same tick
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.interval_s <= 0:
            raise ValueError("beacon interval must be positive")
        if self.lifetime_s <= self.interval_s:
            raise ValueError("a segment must outlive the interval that refreshes it")
        if not 0.0 <= self.jitter < 1.0:
            raise ValueError("jitter is a fraction of the interval, in [0, 1)")


@dataclass(frozen=True)
class Segment:
    """One beaconed segment: an interface sequence plus its current material."""

    seg_type: int
    #: interface ids, in traversal order; two per inter-AS hop
    ifaces: tuple[int, ...]
    #: AS ids visited, ``len(ifaces) // 2 + 1`` of them
    ases: tuple[int, ...]
    structural_id: int
    generation: int
    signature_id: int
    created_s: float
    expiry_s: float

    @property
    def segment_id(self) -> int:
        """Changes on every re-signing. The identifier that eats memory."""
        return _material_hash(self.structural_id, self.generation, self.signature_id)

    @property
    def origin(self) -> int:
        return self.ases[0]

    @property
    def terminal(self) -> int:
        return self.ases[-1]

    @property
    def hop_count(self) -> int:
        return len(self.ifaces) // 2

    def is_valid_at(self, t: float) -> bool:
        return self.created_s <= t < self.expiry_s

    def reversed_(self) -> Segment:
        """The same interfaces walked the other way, as a down-segment.

        A down-segment is a registered up-segment read in reverse; it carries
        the same material because it is the same beacon.
        """
        return replace(
            self,
            seg_type=SEG_DOWN if self.seg_type == SEG_UP else SEG_UP,
            ifaces=tuple(reversed(self.ifaces)),
            ases=tuple(reversed(self.ases)),
            structural_id=structural_hash(tuple(reversed(self.ifaces))),
        )


@dataclass(frozen=True)
class FilterPolicy:
    """An AS declining to propagate the segments that traverse it.

    Held as a **rule**, not as the set of segments it matched when it was
    installed. Path composition is lazy -- core segments between a pair of core
    ASes are discovered on first ask -- so a policy stored as a set of segment
    ids silently stops applying to everything composed afterwards. That is how
    roughly eleven thousand paths went on traversing a "filtered" AS at the
    ``dev`` tier while :meth:`SegmentStore.filtered` reported the filter as
    applied. See
    ``tests/test_segments.py::test_a_policy_filter_catches_core_segments_discovered_later``.

    ``fraction`` below 1 hides that share of the AS's segments rather than all
    of them, decided per segment by a stable draw so that the answer does not
    depend on which segments happened to exist first.
    """

    as_: int
    fraction: float = 1.0
    #: salt for the partial draw; see :meth:`seeded`
    salt: int = 0

    @classmethod
    def seeded(cls, as_: int, fraction: float, *, seed: int, at_s: float) -> FilterPolicy:
        """A policy whose partial draw is reproducible from the run's seed.

        Packed big-endian rather than through :meth:`numpy.ndarray.tobytes`,
        because native byte order would make the draw host-dependent and
        invariant 4 does not allow that.
        """
        digest = hashlib.blake2b(
            int(seed).to_bytes(8, "big", signed=True)
            + int(as_).to_bytes(8, "big", signed=True)
            + struct.pack(">d", float(at_s)),
            digest_size=8,
            person=b"scion-fs",
        )
        return cls(as_=as_, fraction=fraction, salt=int.from_bytes(digest.digest(), "big"))

    def matches(self, segment: Segment) -> bool:
        if not segment.ifaces or self.as_ not in segment.ases:
            return False
        if self.fraction >= 1.0:
            return True
        return _filter_draw(self.salt, segment.structural_id) < self.fraction


@dataclass(frozen=True)
class Path:
    """An end-to-end path, composed of up / core / down segments.

    Both identifiers are carried. Which one a model is shown as ``path_id`` is
    the exposure layer's decision, driven by ``identity_policy``.
    """

    src: int
    dst: int
    ifaces: tuple[int, ...]
    ases: tuple[int, ...]
    structural_id: int
    segment_id: int
    expiry_s: float
    #: ids of the segments this was composed from, in order
    component_ids: tuple[int, ...]

    @property
    def hop_count(self) -> int:
        return len(self.ifaces) // 2

    @property
    def links(self) -> tuple[int, ...]:
        """Link ids traversed. What the link-state model is indexed by."""
        return tuple(iface // 2 for iface in self.ifaces[::2])

    def path_id(self, policy: IdentityPolicy) -> int:
        if policy == "structural":
            return self.structural_id
        if policy == "crypto_bound":
            return self.segment_id
        raise ValueError(f"identity_policy must be one of {IDENTITY_POLICIES}, not {policy!r}")


class SegmentStore:
    """Builds, ages, re-signs and combines segments over a static topology.

    Not thread-safe and not meant to be: one store belongs to one episode.
    """

    def __init__(
        self,
        topology: Topology,
        *,
        seed: int = 0,
        policy: BeaconPolicy | None = None,
        t0: float = 0.0,
        max_up_per_as: int = 16,
        max_up_hops: int = 5,
        max_core_per_pair: int = 4,
        max_core_hops: int = 6,
        core_beam: int = 32,
        max_peering_joins: int = 3,
        max_paths_per_scope: int = 400,
        path_cache_size: int = 4096,
    ) -> None:
        self.topology = topology
        self.policy = policy or BeaconPolicy()
        self.seed = seed
        self.t = t0
        self.max_up_per_as = max_up_per_as
        self.max_up_hops = max_up_hops
        self.max_core_per_pair = max_core_per_pair
        self.max_core_hops = max_core_hops
        self.core_beam = core_beam
        self.max_peering_joins = max_peering_joins
        self.max_paths_per_scope = max_paths_per_scope

        self._rng = np.random.default_rng(seed)
        #: bumped whenever anything invalidates composed paths
        self.epoch = 0
        self._segments: list[Segment] = []
        #: when each segment is next due for re-beaconing, parallel to _segments
        self._due: list[float] = []
        #: the same, as an array, built on demand and kept in step. The scan for
        #: what is due runs on every tick and the realistic tier has enough
        #: segments that doing it in Python was the substrate's largest cost.
        self._due_arr: NDArray[np.float64] | None = None
        #: earliest of those, so the common "nothing is due" step stays O(1)
        self._next_due = float("inf")
        #: segment ids of the up-segments registered by each AS
        self._up_by_as: list[list[int]] = [[] for _ in range(topology.n_ases)]
        self._core_cache: dict[tuple[int, int], list[int]] = {}
        self._path_cache: OrderedDict[tuple[int, int], list[Path]] = OrderedDict()
        self._path_cache_size = path_cache_size
        #: segments hidden by AS policy filtering. Not a topology change.
        self._filtered: set[int] = set()
        #: the policies that produced them, kept so that segments composed later
        #: are tested too. ``_filtered`` alone cannot do that -- see FilterPolicy.
        self._policies: list[FilterPolicy] = []
        #: segment ids re-signed since the beacon feed last drained
        self._resigned: list[int] = []
        #: (as, role) -> ((neighbour, local iface), ...). The CSR arrays are the
        #: right shape for whole-graph work and the wrong shape for walking one
        #: AS at a time in Python; slicing them per hop cost more than the search
        #: itself. The graph is static, so caching the slices is safe.
        self._adj_cache: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {}

        self._build_up_segments()

    @classmethod
    def for_tier(cls, topology: Topology, tier: Tier, *, seed: int = 0, **kwargs: object) -> Self:
        """A store sized so path counts land in the tier's band.

        The tier says how many paths a (src, dst) scope should resolve to. That
        is a property of the whole substrate -- generator fan-out, beaconing
        breadth and composition all feed it -- so it is set here rather than
        being left to whoever calls the constructor.
        """
        floor, ceiling = tier.paths_per_pair
        defaults: dict[str, object] = {
            "max_paths_per_scope": ceiling,
            "max_up_per_as": max(4, min(16, ceiling // 8)),
            "max_core_per_pair": 2 if ceiling <= 30 else 4,
            "max_peering_joins": 1 if ceiling <= 30 else 3,
        }
        defaults.update(kwargs)
        return cls(topology, seed=seed, **defaults)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ sizes

    def __len__(self) -> int:
        return len(self._segments)

    @property
    def n_segments(self) -> int:
        return len(self._segments)

    def segment(self, seg_id: int) -> Segment:
        return self._segments[seg_id]

    def __repr__(self) -> str:
        return (
            f"SegmentStore({self.n_segments} segments, t={self.t:.1f}s, "
            f"epoch={self.epoch}, filtered={len(self._filtered)})"
        )

    # ------------------------------------------------------------- registration

    def _register(
        self,
        seg_type: int,
        ifaces: Sequence[int],
        ases: Sequence[int],
        *,
        created_s: float,
    ) -> int:
        structural_id = structural_hash(ifaces)
        seg_id = len(self._segments)
        self._segments.append(
            Segment(
                seg_type=seg_type,
                ifaces=tuple(int(i) for i in ifaces),
                ases=tuple(int(a) for a in ases),
                structural_id=structural_id,
                generation=0,
                signature_id=self._new_signature(),
                created_s=created_s,
                expiry_s=created_s + self.policy.lifetime_s,
            )
        )
        due = self._jittered_due(created_s)
        self._due.append(due)
        self._due_arr = None
        self._next_due = min(self._next_due, due)
        # A segment discovered after a policy was installed is still subject to
        # it. Tested here, once, so the hot read path stays a set lookup.
        if self._policies:
            segment = self._segments[seg_id]
            if any(policy.matches(segment) for policy in self._policies):
                self._filtered.add(seg_id)
        return seg_id

    def _new_signature(self) -> int:
        """A signature id stands in for real cryptographic material. We do not
        do crypto here; we model the fact that the material changes."""
        return int(self._rng.integers(0, 2**63 - 1))

    def _jittered_due(self, created_s: float) -> float:
        """When this segment is next re-beaconed. Jittered so the whole world
        does not re-sign on one tick, which would make refresh trivially easy to
        detect from timing alone."""
        spread = self.policy.jitter * self.policy.interval_s
        return float(created_s + self.policy.interval_s + self._rng.uniform(-spread, spread))

    def _adj(self, a: int, role: int) -> tuple[tuple[int, int], ...]:
        key = (a, role)
        cached = self._adj_cache.get(key)
        if cached is None:
            neighbours, ifaces = self.topology.neighbours_by_role(a, role)
            cached = tuple(
                (int(nb), int(iface)) for nb, iface in zip(neighbours, ifaces, strict=True)
            )
            self._adj_cache[key] = cached
        return cached

    # ------------------------------------------------------- up-segment build

    def _build_up_segments(self) -> None:
        """Walk providers from every AS until a core AS is reached.

        Bounded breadth-first: at most ``max_up_per_as`` segments and
        ``max_up_hops`` hops per AS. Real beaconing is also bounded, by the
        number of PCBs an AS propagates.

        A core AS registers a single empty up-segment: it *is* its own entry to
        the core, and representing that as a zero-hop segment keeps path
        composition free of special cases.
        """
        topo = self.topology
        is_core = topo.as_is_core.tolist()
        for a in range(topo.n_ases):
            if is_core[a]:
                self._up_by_as[a].append(self._register(SEG_UP, (), (a,), created_s=self.t))
                continue

            found: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
            # (current AS, ifaces so far, ases so far)
            frontier: list[tuple[int, tuple[int, ...], tuple[int, ...]]] = [(a, (), (a,))]
            for _ in range(self.max_up_hops):
                if not frontier or len(found) >= self.max_up_per_as:
                    break
                next_frontier: list[tuple[int, tuple[int, ...], tuple[int, ...]]] = []
                for here, ifaces, ases in frontier:
                    for provider, local_iface in self._adj(here, ROLE_PROVIDER):
                        if provider in ases:  # no loops; the DAG makes this rare
                            continue
                        hop = (*ifaces, local_iface, local_iface ^ 1)
                        walked = (*ases, provider)
                        if is_core[provider]:
                            found.append((hop, walked))
                            if len(found) >= self.max_up_per_as:
                                break
                        elif len(next_frontier) < self.max_up_per_as:
                            next_frontier.append((provider, hop, walked))
                    if len(found) >= self.max_up_per_as:
                        break
                frontier = next_frontier

            for ifaces, ases in found[: self.max_up_per_as]:
                self._up_by_as[a].append(self._register(SEG_UP, ifaces, ases, created_s=self.t))

    def up_segments(self, a: int) -> list[Segment]:
        return [self._segments[i] for i in self._up_by_as[a] if i not in self._filtered]

    # ----------------------------------------------------- core segment build

    def core_segments(self, c1: int, c2: int) -> list[Segment]:
        """Core segments between two core ASes. Built on first ask, then cached.

        Lazily, because at the stress tier there are 800 core ASes and therefore
        320,000 ordered core pairs, and a run touches a handful of them.
        """
        if c1 == c2:
            return []
        key = (c1, c2)
        cached = self._core_cache.get(key)
        if cached is None:
            cached = self._discover_core_segments(c1, c2)
            self._core_cache[key] = cached
        return [self._segments[i] for i in cached if i not in self._filtered]

    def _discover_core_segments(self, c1: int, c2: int) -> list[int]:
        """Beam search over core links only.

        Width-limited on purpose. A plain breadth-first walk over partial paths
        has a frontier that grows like the core degree to the power of the hop
        limit -- at the realistic tier that was a million interface lookups per
        core pair and it dominated every path query. The beam caps the frontier
        and caps how many times one core AS may be expanded, which costs some
        path diversity between distant cores and buys back two orders of
        magnitude.
        """
        out: list[int] = []
        visits: dict[int, int] = {}
        frontier: list[tuple[int, tuple[int, ...], tuple[int, ...]]] = [(c1, (), (c1,))]
        for _ in range(self.max_core_hops):
            if not frontier or len(out) >= self.max_core_per_pair:
                break
            next_frontier: list[tuple[int, tuple[int, ...], tuple[int, ...]]] = []
            for here, ifaces, ases in frontier:
                for neighbour, local_iface in self._adj(here, ROLE_CORE):
                    if neighbour in ases:
                        continue
                    hop = (*ifaces, local_iface, local_iface ^ 1)
                    walked = (*ases, neighbour)
                    if neighbour == c2:
                        out.append(self._register(SEG_CORE, hop, walked, created_s=self.t))
                        if len(out) >= self.max_core_per_pair:
                            break
                    elif len(next_frontier) < self.core_beam:
                        seen = visits.get(neighbour, 0)
                        if seen < self.max_core_per_pair:
                            visits[neighbour] = seen + 1
                            next_frontier.append((neighbour, hop, walked))
                if len(out) >= self.max_core_per_pair:
                    break
            frontier = next_frontier
        if not out:
            # The beam found nothing. Either the cores really are in different
            # components, or the beam threw away the only route. Fall back to a
            # plain visited-set breadth-first search: it finds one shortest core
            # segment if any exists, and its cost is linear in the core graph
            # rather than exponential in the hop limit. Distinguishing "no path"
            # from "search gave up" matters, because the first is a scenario and
            # the second is a bug that looks like one.
            shortest = self._shortest_core_segment(c1, c2)
            if shortest is not None:
                ifaces, ases = shortest
                out.append(self._register(SEG_CORE, ifaces, ases, created_s=self.t))
        return out

    def _shortest_core_segment(
        self, c1: int, c2: int
    ) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
        parent: dict[int, tuple[int, int]] = {}  # node -> (previous node, iface used)
        seen = {c1}
        frontier = [c1]
        while frontier:
            next_frontier: list[int] = []
            for here in frontier:
                for neighbour, iface in self._adj(here, ROLE_CORE):
                    if neighbour in seen:
                        continue
                    seen.add(neighbour)
                    parent[neighbour] = (here, iface)
                    if neighbour == c2:
                        ifaces: list[int] = []
                        ases = [c2]
                        node = c2
                        while node != c1:
                            previous, used = parent[node]
                            ifaces[:0] = [used, used ^ 1]
                            ases.insert(0, previous)
                            node = previous
                        return tuple(ifaces), tuple(ases)
                    next_frontier.append(neighbour)
            frontier = next_frontier
        return None

    # ------------------------------------------------------------- composition

    def paths_for(self, src: int, dst: int, *, limit: int | None = None) -> list[Path]:
        """End-to-end paths for one scope. Materialised on demand and cached.

        Composition follows SCION's shape: an up-segment from the source to a
        core AS, a core segment if the two core ASes differ, and a down-segment
        to the destination. Two shortcuts are also produced, because without
        them a source and destination under the same provider would route
        through the core, which is neither what SCION does nor what the path
        counts should look like:

        * **shortcut** — the up and down segments meet at a common AS, and both
          are truncated there;
        * **peering** — an AS on the up-segment peers with an AS on the down
          segment, and the path crosses directly.
        """
        if src == dst:
            return []
        key = (src, dst)
        cached = self._path_cache.get(key)
        if cached is not None:
            self._path_cache.move_to_end(key)
            return cached[:limit] if limit else cached

        paths = self._compose(src, dst)
        self._path_cache[key] = paths
        self._path_cache.move_to_end(key)
        while len(self._path_cache) > self._path_cache_size:
            self._path_cache.popitem(last=False)
        return paths[:limit] if limit else paths

    def _compose(self, src: int, dst: int) -> list[Path]:
        ups = self.up_segments(src)
        downs = [seg.reversed_() for seg in self.up_segments(dst)]
        seen: set[int] = set()
        out: list[Path] = []

        def emit(
            ifaces: tuple[int, ...],
            ases: tuple[int, ...],
            components: tuple[Segment, ...],
        ) -> None:
            if len(set(ases)) != len(ases):  # a loop is not a path
                return
            structural_id = structural_hash(ifaces)
            if structural_id in seen:
                return
            seen.add(structural_id)
            out.append(
                Path(
                    src=src,
                    dst=dst,
                    ifaces=ifaces,
                    ases=ases,
                    structural_id=structural_id,
                    segment_id=self._compose_segment_id(structural_id, components),
                    expiry_s=min((c.expiry_s for c in components), default=float("inf")),
                    component_ids=tuple(c.structural_id for c in components),
                )
            )

        for up in ups:
            if len(out) >= self.max_paths_per_scope:
                break
            up_positions = {a: i for i, a in enumerate(up.ases)}
            for down in downs:
                if len(out) >= self.max_paths_per_scope:
                    break
                # 1. shortcut: the segments meet at a common AS
                common = [a for a in down.ases if a in up_positions]
                if common:
                    join = common[0]
                    i = up_positions[join]
                    j = down.ases.index(join)
                    emit(
                        (*up.ifaces[: 2 * i], *down.ifaces[2 * j :]),
                        (*up.ases[: i + 1], *down.ases[j + 1 :]),
                        (up, down),
                    )
                    continue

                # 2. peering: an AS on the way up peers with one on the way down
                for ifaces, ases in self._peering_joins(up, down):
                    emit(ifaces, ases, (up, down))

                # 3. through the core
                if up.terminal == down.origin:
                    emit(
                        (*up.ifaces, *down.ifaces),
                        (*up.ases, *down.ases[1:]),
                        (up, down),
                    )
                else:
                    for core in self.core_segments(up.terminal, down.origin):
                        emit(
                            (*up.ifaces, *core.ifaces, *down.ifaces),
                            (*up.ases, *core.ases[1:], *down.ases[1:]),
                            (up, core, down),
                        )
        return out

    def _peering_joins(
        self, up: Segment, down: Segment
    ) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
        """Peering links joining the up-segment to the down-segment.

        More than one, because for a source and destination in the same ISD the
        peering shortcuts are most of the path diversity there is, and taking
        only the first one halves the path count for exactly the pairs that
        should have the most choice.
        """
        down_positions = {a: i for i, a in enumerate(down.ases)}
        out: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
        for i, a in enumerate(up.ases):
            for peer, iface in self._adj(a, ROLE_PEER):
                j = down_positions.get(peer)
                if j is None:
                    continue
                out.append(
                    (
                        (*up.ifaces[: 2 * i], iface, iface ^ 1, *down.ifaces[2 * j :]),
                        (*up.ases[: i + 1], *down.ases[j:]),
                    )
                )
                if len(out) >= self.max_peering_joins:
                    return out
        return out

    @staticmethod
    def _compose_segment_id(structural_id: int, components: tuple[Segment, ...]) -> int:
        """A path's crypto-bound id folds in every component's material, so any
        one of them being re-signed changes it."""
        digest = hashlib.blake2b(
            structural_id.to_bytes(8, "big")
            + b"".join(c.segment_id.to_bytes(8, "big") for c in components),
            digest_size=8,
            person=b"scion-pt",
        )
        return int.from_bytes(digest.digest(), "big")

    # ------------------------------------------------------------------ churn

    def advance_to(self, t: float) -> int:
        """Advance to time ``t``, re-beaconing whatever is due. Returns the
        number of segments re-signed.

        Re-signing keeps the interface sequence and replaces the material, which
        is the whole point: ``structural_id`` is unchanged, ``segment_id`` is
        not.
        """
        if t < self.t:
            raise ValueError("time does not run backwards")
        self.t = t
        if t < self._next_due:  # the common case, and it must not be O(segments)
            return 0
        if self._due_arr is None:
            self._due_arr = np.array(self._due, dtype=np.float64)
        due = np.flatnonzero(self._due_arr <= t)
        self._resign_all(due, t)
        return int(due.size)

    def rebeacon(self, seg_ids: Sequence[int] | None = None) -> int:
        """Force a re-beaconing round now. Used by probes and scenario events."""
        targets = list(range(len(self._segments))) if seg_ids is None else [int(i) for i in seg_ids]
        self._resign_all(targets, self.t)
        return len(targets)

    def _resign_all(self, seg_ids: Sequence[int] | NDArray[np.intp], t: float) -> None:
        #: Which segments have been re-signed since the feed last drained. The
        #: beacon feed needs to know *which*, not how many, and rescanning every
        #: segment to find out would make a cheap step expensive at the
        #: realistic tier.
        #:
        #: Accumulated, not assigned. Assigning kept only the most recent round,
        #: so every resigning between two drains was lost -- measured at 81% of
        #: them -- and a reader stepping the world a second at a time saw one
        #: tick's worth of a feed it believed was complete. At the corrected 5 s
        #: beacon interval there are sixty times as many rounds to lose.
        self._resigned.extend(int(i) for i in seg_ids)
        for seg_id in seg_ids:
            seg = self._segments[seg_id]
            self._segments[seg_id] = replace(
                seg,
                generation=seg.generation + 1,
                signature_id=self._new_signature(),
                created_s=t,
                expiry_s=t + self.policy.lifetime_s,
            )
            when = self._jittered_due(t)
            self._due[seg_id] = when
            if self._due_arr is not None:
                self._due_arr[seg_id] = when
        if len(seg_ids):
            self._next_due = (
                float(self._due_arr.min())
                if self._due_arr is not None
                else (min(self._due) if self._due else float("inf"))
            )
            self._invalidate()

    def expired(self, t: float | None = None) -> list[int]:
        when = self.t if t is None else t
        return [i for i, seg in enumerate(self._segments) if not seg.is_valid_at(when)]

    # --------------------------------------------------------- policy filtering

    def filter_segments(self, seg_ids: Sequence[int]) -> None:
        """Hide segments from path server answers. **Not** a topology change.

        An AS declining to propagate a segment removes paths without any link
        going anywhere, and a model that infers "link down" from "path gone" is
        wrong in a way worth being able to produce.
        """
        self._filtered.update(int(i) for i in seg_ids)
        self._invalidate()

    def apply_filter_policy(self, policy: FilterPolicy) -> int:
        """Install an AS policy and hide everything already registered that it
        matches. Returns how many that was.

        Segments composed *after* this call are tested as they are registered,
        which is the whole reason a policy is kept rather than expanded into a
        set of ids here.
        """
        self._policies.append(policy)
        hidden = {seg_id for seg_id, seg in enumerate(self._segments) if policy.matches(seg)}
        self._filtered.update(hidden)
        self._invalidate()
        return len(hidden)

    def unfilter_segments(self, seg_ids: Sequence[int] | None = None) -> None:
        """Restore segments. With no argument, restores everything **and**
        removes the policies, so later composition is unfiltered again.

        Naming specific ids leaves the policies in place: it lifts the hiding of
        those segments, not the rule that produced it, and a segment composed
        later that matches a policy is still hidden.
        """
        if seg_ids is None:
            self._filtered.clear()
            self._policies.clear()
        else:
            self._filtered.difference_update(int(i) for i in seg_ids)
        self._invalidate()

    # ------------------------------------------------------------ beacon feed

    def drain_resigned(self) -> list[int]:
        """Every segment re-signed since this was last called, and clear.

        A drain rather than a readable attribute, because the reader owning the
        clear is the only arrangement in which nothing is silently dropped: an
        attribute that the store overwrites each round loses whatever the reader
        did not collect in time, which is exactly what it used to do.
        """
        drained, self._resigned = self._resigned, []
        return drained

    @property
    def resigned_pending(self) -> int:
        """How many re-signings are waiting to be drained. Instrumentation only."""
        return len(self._resigned)

    @property
    def filtered(self) -> frozenset[int]:
        return frozenset(self._filtered)

    @property
    def filter_policies(self) -> tuple[FilterPolicy, ...]:
        return tuple(self._policies)

    # -------------------------------------------------------------- internals

    def _invalidate(self) -> None:
        self.epoch += 1
        self._path_cache.clear()

    # ------------------------------------------------------------- inspection

    def iter_segments(self) -> Iterator[tuple[int, Segment]]:
        yield from enumerate(self._segments)

    def structural_ids(self) -> NDArray[np.uint64]:
        return np.array([s.structural_id for s in self._segments], dtype=np.uint64)

    def segment_ids(self) -> NDArray[np.uint64]:
        return np.array([s.segment_id for s in self._segments], dtype=np.uint64)

    def digest(self) -> str:
        """Content hash over structure only, deliberately excluding material.

        Two runs of the same seed produce the same structure; whether they
        produce the same *material* is a separate question, and one the
        determinism test asks separately.
        """
        h = hashlib.sha256()
        for seg in self._segments:
            h.update(seg.structural_id.to_bytes(8, "big"))
            h.update(bytes((seg.seg_type,)))
        return h.hexdigest()[:16]
