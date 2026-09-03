"""The portable scenario schema, and the engine that runs one.

A scenario says what world to build and what happens to it. The same file has
to drive four backends -- replay, analytical, ``scion-dqn-sim``, real hardware
through ``linkd`` -- because portability across tiers is the project's
contribution and it dies if each tier grows its own format. ADR 0007 records
the shape and why.

The rule that governs what goes in the file: not "would a user set this" but
"could two runs differ in it and still be described as the same scenario". The
path search caps and the congestion constants both fail that test if left out,
and neither is something anyone would think to write down, so both are in.

    >>> scenario = Scenario.for_tier("smoke", duration_s=600.0)
    >>> world = scenario.build()
    >>> world.step()
    0
    >>> round(world.now, 1)
    1.0

:class:`Scenario` is a description and is cheap. :class:`Substrate` is the
world and is not. Keeping them apart is what lets M6 hash, diff and validate a
scenario without building 2,000 ASes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path as FilePath
from typing import Any, Final

import numpy as np

from scionarena.core.clock import Clock, Event
from scionarena.core.hosts import HostParams, HostPopulation, ScopeState
from scionarena.core.linkstate import BackgroundParams, LinkParams, LinkState
from scionarena.core.segments import (
    IDENTITY_POLICIES,
    BeaconPolicy,
    FilterPolicy,
    Path,
    SegmentStore,
)
from scionarena.core.tiers import TIERS, Tier
from scionarena.core.tiers import tier as tier_by_name
from scionarena.core.topology import Topology, synthetic
from scionarena.core.trace import TraceHash

__all__ = [
    "SCHEMA_VERSION",
    "EVENT_KINDS",
    "EXTENSION_PREFIX",
    "TopologySpec",
    "SearchSpec",
    "ProbeLimits",
    "TimelineEvent",
    "Disturbance",
    "Scenario",
    "Substrate",
]

#: Bumped when a change to the schema is not backward compatible.
SCHEMA_VERSION: Final = 1

#: Timeline event kinds the substrate implements. Chosen against ``linkd``'s
#: REST surface -- bandwidth, latency, loss, up/down -- so that M9 has a
#: mechanical mapping rather than a translation problem.
EVENT_KINDS: Final = (
    "link_degrade",  # health multiplier on one link, one or both directions
    "link_restore",  # undo degradation, one link or all of them
    "as_policy_filter",  # segments vanish from path-server answers; graph untouched
    "as_policy_unfilter",
    "demand_surge",  # exogenous-but-scheduled load, in Mbps, on a scope
    "demand_clear",
    "topology_change",  # the rare, planned, expensive one: rebuilds the world
    "advisory_apply",  # M3: a published advisory reaching the hosts, late
)

#: A front-end may carry its own events through the timeline under this prefix.
#: They reach the clock and whatever registered for them, and the substrate
#: does not need to know what a gym wrapper wants.
EXTENSION_PREFIX: Final = "x:"

#: Priorities, so that the world updates before anybody observes it without
#: either side knowing about the other.
PRIORITY_WORLD: Final = 0
PRIORITY_SCENARIO: Final = 10
PRIORITY_OBSERVE: Final = 20


def _check_unknown(kind: str, given: Mapping[str, Any], known: Iterable[str]) -> None:
    """Raise on any key we do not recognise.

    A misspelled key that is silently ignored produces a run that claims to be
    one thing and is another, and nothing in the output says so.
    """
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise ValueError(f"unknown {kind} key(s) {unknown}; known keys are {sorted(known)}")


def _from_mapping(cls: type[Any], data: Mapping[str, Any], label: str) -> Any:
    names = {f.name for f in fields(cls)}
    _check_unknown(label, data, names)
    return cls(**data)


@dataclass(frozen=True, slots=True)
class TopologySpec:
    """Where the static graph comes from.

    ``tier`` is the scale band and sets the defaults for everything else; an
    explicit ``n_ases``/``n_links`` overrides it, which is how a scenario says
    "realistic-shaped but half the size" without inventing a fifth tier.
    """

    source: str = "synthetic"
    tier: str = "dev"
    n_ases: int | None = None
    n_links: int | None = None
    n_isds: int | None = None
    #: Offset added to the scenario seed, so two scenarios can share a seed and
    #: differ in topology, or differ in seed and share a topology.
    seed_offset: int = 0
    #: For ``source="caida"`` or ``"dqnsim"``. Unused by ``"synthetic"``.
    path: str | None = None

    def __post_init__(self) -> None:
        if self.source not in ("synthetic", "caida", "dqnsim"):
            raise ValueError(f"unknown topology source {self.source!r}")
        if self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}; known tiers are {sorted(TIERS)}")
        if self.source != "synthetic" and not self.path:
            raise ValueError(f"topology source {self.source!r} needs a path")

    @property
    def resolved_tier(self) -> Tier:
        return tier_by_name(self.tier)

    def build(self, seed: int) -> Topology:
        if self.source != "synthetic":
            raise NotImplementedError(
                f"topology source {self.source!r} is not built yet: from_caida and "
                "from_dqnsim are the open half of M1 deliverable 1. The schema "
                "carries them so scenarios written now stay valid."
            )
        band = self.resolved_tier
        return synthetic(
            n_ases=self.n_ases if self.n_ases is not None else band.n_ases,
            n_links=self.n_links if self.n_links is not None else band.n_links,
            n_isds=self.n_isds if self.n_isds is not None else band.n_isds,
            seed=seed + self.seed_offset,
        )


@dataclass(frozen=True, slots=True)
class ProbeLimits:
    """How much probing the deployment permits, per limited resource.

    These were three constants in ``exposure/tools.py`` presented as though they
    were protocol facts. They are not. The SCMP specification says only that
    SCMP *may* be subject to rate limiting, and the audited router implements no
    limiter at all -- there is no token bucket anywhere in the tree. Open
    question Q6 asked what the real limit is; the answer is that there isn't
    one, and what a deployment applies is a per-deployment operational choice
    that may be zero.

    So they belong to the scenario, beside :class:`BeaconPolicy` and
    ``LinkParams``, for the reason ADR 0007 gives: two runs made under different
    assumptions about how expensive information is are not the same experiment,
    and with the numbers buried in the exposure layer nothing in either run's
    output said they differed. Every budget result the harness has produced is
    conditional on these, and now says so.

    The defaults are the old constants, so nothing moves until a scenario asks.
    Note that they are stated as plain numbers rather than as ``RateLimit``:
    that type lives in ``exposure`` and ``core`` may not import upwards.
    """

    #: echoes per second, answered by the border router
    scmp_calls: float = 5.0
    scmp_per_s: float = 1.0
    #: bandwidth tests, which are far dearer because they are real traffic
    bwtest_calls: float = 1.0
    bwtest_per_s: float = 30.0
    #: path-server lookups, per destination AS
    path_server_calls: float = 2.0
    path_server_per_s: float = 1.0

    def __post_init__(self) -> None:
        for name in ("scmp", "bwtest", "path_server"):
            calls = float(getattr(self, f"{name}_calls"))
            per_s = float(getattr(self, f"{name}_per_s"))
            if calls < 0.0:
                raise ValueError(f"{name} allowance cannot be negative")
            if per_s <= 0.0:
                raise ValueError(f"{name} window must be positive")


@dataclass(frozen=True, slots=True)
class SearchSpec:
    """The caps on path composition.

    These belong in the scenario because they change measured path diversity:
    two runs that differ in beam width are not comparable, and nothing in
    either run's output says they differed. Left unset, they follow the tier,
    which is what ``SegmentStore.for_tier`` picks.
    """

    max_up_per_as: int | None = None
    max_up_hops: int = 5
    max_core_per_pair: int | None = None
    max_core_hops: int = 6
    core_beam: int = 32
    max_peering_joins: int | None = None
    max_paths_per_scope: int | None = None
    path_cache_size: int = 4096

    def kwargs(self) -> dict[str, int]:
        """Only the caps the scenario pinned. The rest come from the tier."""
        out: dict[str, int] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None:
                out[f.name] = int(value)
        return out


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One scheduled thing. Data, not a callable: it has to survive a file.

    ``params`` is deliberately untyped at this level. Each kind validates its
    own in :class:`Substrate`, at build time rather than when it fires, so a
    malformed event in hour nine of a run is caught in the first second.
    """

    at_s: float
    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)
    #: Free text carried into the trace. For the human reading the report.
    note: str = ""

    def __post_init__(self) -> None:
        if self.at_s < 0.0:
            raise ValueError(f"event time must not be negative, got {self.at_s}")
        if self.kind not in EVENT_KINDS and not self.kind.startswith(EXTENSION_PREFIX):
            raise ValueError(
                f"unknown event kind {self.kind!r}; known kinds are {list(EVENT_KINDS)}, "
                f"or prefix it {EXTENSION_PREFIX!r} to carry a front-end's own event"
            )
        object.__setattr__(self, "params", dict(self.params))


#: Which population a :class:`Disturbance` draws its targets from. A kind not
#: listed here has nothing to sample, so it is scheduled once with whatever
#: parameters it was given.
_DRAWS_FROM: Final = {
    "link_degrade": "links",
    "as_policy_filter": "ases",
}


@dataclass(frozen=True, slots=True)
class Disturbance:
    """A scheduled fault, described against the topology's *shape*.

    A :class:`TimelineEvent` names one link by index, which is right for a
    hand-written scenario and impossible for an axis. ``Tier.n_links`` is
    documented as a target the generator hits within a few, so an axis value
    written against one tier's indices is refused at another -- and an axis whose
    meaning depends on the tier is not an axis (ADR 0022).

    So a disturbance says *when* as a fraction of the run and *how much* as a
    fraction of the links or ASes, and :meth:`Substrate._install_timeline`
    expands it into ordinary events once the topology exists. Which links is
    drawn from the scenario seed, so the same seed gets the same bad day and both
    halves of a parity pair face it.
    """

    kind: str
    #: When, as a fraction of the run. 0.0 is before the model has seen
    #: anything, 1.0 is after the last decision; the interesting range is the
    #: middle, and a value outside [0, 1] is refused.
    at_frac: float
    #: Of the links (or ASes) this kind draws from. Ignored when the kind draws
    #: from nothing.
    fraction: float = 0.0
    #: An absolute count, used instead of ``fraction`` when it is non-zero. For
    #: a scenario that wants "one link" regardless of tier.
    count: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS and not self.kind.startswith(EXTENSION_PREFIX):
            raise ValueError(
                f"unknown disturbance kind {self.kind!r}; known kinds are {list(EVENT_KINDS)}"
            )
        if not 0.0 <= self.at_frac <= 1.0:
            raise ValueError(
                f"at_frac is a fraction of the run and must be in [0, 1], got {self.at_frac}"
            )
        if not 0.0 <= self.fraction <= 1.0:
            raise ValueError(f"fraction must be in [0, 1], got {self.fraction}")
        if self.count < 0:
            raise ValueError("count must not be negative")

    def _entropy(self, seed: int, at_s: float, want: int) -> list[int]:
        """Seed material for the draw. Two things it has to get right.

        It must fit: a seed is masked into range rather than handed to
        ``default_rng`` raw, which refuses anything negative or over 128 bits.
        ``Scenario`` refuses a negative seed outright, so the mask here is a
        floor and not the policy.

        And it must separate two disturbances that differ only in their
        parameters. The first version mixed in ``len(self.kind)``, so
        "degrade a tenth of the links mildly" and "degrade a tenth of the links
        hard", scheduled together, drew **exactly the same links** -- a tenth
        degraded twice rather than a fifth degraded, and the result file reads
        identically either way. The params go into the hash.
        """
        material = f"{self.kind}|{at_s}|{want}|{sorted(self.params.items())}"
        tag = int(hashlib.sha256(material.encode()).hexdigest()[:12], 16)
        return [abs(int(seed)) & 0xFFFF_FFFF_FFFF, tag]

    def expand(self, topology: Topology, duration_s: float, seed: int) -> tuple[TimelineEvent, ...]:
        """The concrete events this disturbance becomes on *this* topology.

        Rounded to a whole second because a fault at 731.4 s reads as a
        measurement rather than a decision, and the recovery window is measured
        on a grid of whole seconds anyway.
        """
        at_s = round(self.at_frac * duration_s, 3)
        draws = _DRAWS_FROM.get(self.kind)
        if draws is None:
            return (
                TimelineEvent(at_s=at_s, kind=self.kind, params=dict(self.params), note=self.note),
            )
        total = topology.n_links if draws == "links" else topology.n_ases
        want = self.count if self.count else int(round(self.fraction * total))
        want = max(0, min(total, want))
        if want == 0:
            return ()
        rng = np.random.default_rng(self._entropy(seed, at_s, want))
        picked = sorted(int(i) for i in rng.choice(total, size=want, replace=False))
        field_name = "link" if draws == "links" else "as_"
        return tuple(
            TimelineEvent(
                at_s=at_s,
                kind=self.kind,
                params={field_name: index, **self.params},
                note=self.note,
            )
            for index in picked
        )


@dataclass(frozen=True, slots=True)
class Scenario:
    """A world, and what happens to it. The unit two people compare results on."""

    name: str = "unnamed"
    seed: int = 0
    duration_s: float = 3_600.0
    #: How much simulated time one :meth:`Substrate.step` covers.
    step_s: float = 1.0
    #: Which identifier ``path_id`` aliases. ``structural`` is the documented
    #: default because it is what the deployed stack does: Q1 is resolved, and
    #: ``snet.Fingerprint`` hashes the interface sequence alone. ``crypto_bound``
    #: stays available -- it is what a model keying on the raw path or on segment
    #: IDs effectively sees, which is the hazard R4 is re-aimed at.
    identity_policy: str = "structural"
    topology: TopologySpec = field(default_factory=TopologySpec)
    beaconing: BeaconPolicy = field(default_factory=BeaconPolicy)
    search: SearchSpec = field(default_factory=SearchSpec)
    link: LinkParams = field(default_factory=LinkParams)
    #: What probing costs the model in permission, as opposed to in budget.
    #: Per-deployment rather than a protocol constant -- see :class:`ProbeLimits`.
    probe_limits: ProbeLimits = field(default_factory=ProbeLimits)
    background: BackgroundParams = field(default_factory=BackgroundParams)
    #: The population every scope gets unless the caller overrides it. In the
    #: file for the same reason the congestion constants are: two runs that
    #: differ in how many hosts act on the advice are not the same experiment,
    #: and nothing in either run's output would say they differed.
    hosts: HostParams = field(default_factory=HostParams)
    timeline: tuple[TimelineEvent, ...] = ()
    #: Faults described against the topology's shape rather than its indices,
    #: expanded into ``timeline`` events at build time (ADR 0022). This is what
    #: the ``scenario`` axis carries, because an axis cannot know a link index.
    disturbances: tuple[Disturbance, ...] = ()
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_VERSION:
            raise ValueError(
                f"scenario schema {self.schema} was written for a different version of "
                f"scionarena; this one speaks schema {SCHEMA_VERSION}"
            )
        if self.identity_policy not in IDENTITY_POLICIES:
            raise ValueError(
                f"identity_policy must be one of {list(IDENTITY_POLICIES)}, "
                f"not {self.identity_policy!r}"
            )
        if self.seed < 0:
            # Caught here rather than where it lands. A negative seed reached
            # ``np.random.default_rng`` in three unrelated places -- topology
            # generation, the host population, a disturbance's draw -- and each
            # raised "expected non-negative integer" from inside numpy, naming
            # neither the field nor the scenario. Refused rather than folded to
            # its absolute value: a seed is the identity of a world, and quietly
            # giving -42 and 42 the same one makes two runs that were meant to
            # differ compare as though they agreed.
            raise ValueError(f"seed must not be negative, got {self.seed}")
        if self.duration_s <= 0.0:
            raise ValueError("duration must be positive")
        if not 0.0 < self.step_s <= self.duration_s:
            raise ValueError("step must be positive and no longer than the run")
        late = [e for e in self.timeline if e.at_s > self.duration_s]
        if late:
            raise ValueError(
                f"{len(late)} timeline event(s) are scheduled after the run ends "
                f"({self.duration_s}s); the first is {late[0].kind!r} at {late[0].at_s}s"
            )
        object.__setattr__(self, "timeline", tuple(sorted(self.timeline, key=lambda e: e.at_s)))

    # ------------------------------------------------------------ conveniences

    @classmethod
    def for_tier(cls, name: str, **changes: Any) -> Scenario:
        """A default scenario at one scale band."""
        return cls(name=f"{name}-default", topology=TopologySpec(tier=name), **changes)

    def with_(self, **changes: Any) -> Scenario:
        """A copy with fields replaced. Scenarios are frozen; experiments are not."""
        return replace(self, **changes)

    def then(self, *events: TimelineEvent) -> Scenario:
        """A copy with more timeline events, re-sorted."""
        return replace(self, timeline=self.timeline + tuple(events))

    @property
    def n_steps(self) -> int:
        return int(round(self.duration_s / self.step_s))

    # --------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        """Every field, defaults included, so the file says what ran."""
        data = asdict(self)
        data["timeline"] = [
            {"at_s": e.at_s, "kind": e.kind, "params": dict(e.params), "note": e.note}
            for e in self.timeline
        ]
        data["disturbances"] = [
            {
                "kind": d.kind,
                "at_frac": d.at_frac,
                "fraction": d.fraction,
                "count": d.count,
                "params": dict(d.params),
                "note": d.note,
            }
            for d in self.disturbances
        ]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scenario:
        _check_unknown("scenario", data, {f.name for f in fields(cls)})
        kwargs = dict(data)
        for key, spec in (
            ("topology", TopologySpec),
            ("beaconing", BeaconPolicy),
            ("search", SearchSpec),
            ("link", LinkParams),
            ("probe_limits", ProbeLimits),
            ("background", BackgroundParams),
            ("hosts", HostParams),
        ):
            if key in kwargs:
                kwargs[key] = _from_mapping(spec, kwargs[key], key)
        if "timeline" in kwargs:
            kwargs["timeline"] = tuple(
                _from_mapping(TimelineEvent, e, "timeline event") for e in kwargs["timeline"]
            )
        if "disturbances" in kwargs:
            kwargs["disturbances"] = tuple(
                _from_mapping(Disturbance, d, "disturbance") for d in kwargs["disturbances"]
            )
        return cls(**kwargs)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Scenario:
        return cls.from_dict(json.loads(text))

    def save(self, path: str | FilePath) -> None:
        FilePath(path).write_text(self.to_json() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | FilePath) -> Scenario:
        return cls.from_json(FilePath(path).read_text(encoding="utf-8"))

    def digest(self, n: int = 16) -> str:
        """Content hash. What a result file carries to prove what it ran."""
        return TraceHash(label="scenario").update(self.to_dict()).short(n)

    # ------------------------------------------------------------------ build

    def build(self) -> Substrate:
        return Substrate(self)


class Substrate:
    """The world: topology, segments, link state and a clock, stepped together.

    One object owns the four so that a step cannot advance three of them and
    forget the fourth -- which is the kind of bug that shows up as a model
    mysteriously learning the future.
    """

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.clock = Clock(label=f"substrate:{scenario.name}")
        #: Bumped by a scheduled topology change. Anything caching interface
        #: ids across one is holding stale ids.
        self.generation = 0
        self._surges: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._next_surge = 0
        #: Load a probe is putting on the network *while it runs*. Held here
        #: rather than added straight to the link state, because ``_apply_load``
        #: rebuilds demand from scratch and would otherwise wipe an in-flight
        #: probe's contribution -- after which releasing it subtracts from a
        #: baseline that never carried it. See :meth:`hold_probe_load`.
        self._probe_load: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._next_probe_load = 0
        #: Advisories that have reached the hosts. Published-but-not-yet-applied
        #: is the difference between this and what the session logged, and that
        #: difference is decision latency.
        self.n_advisories_applied = 0
        #: Ticks taken. One per ``scenario.step_s`` of simulated time, plus one
        #: wherever a caller stopped part way through a step.
        self.ticks = 0
        #: When the hosts last offered load. Grid-aligned; the gap to ``now`` is
        #: what they are charged for next time.
        self._loaded_at = 0.0
        #: Instrumentation hooks, run at the end of every tick. Written to by
        #: whoever is watching; the substrate does not know what they do and
        #: nothing the model can reach may register one.
        self.taps: list[Callable[[Substrate], None]] = []
        self.topology = scenario.topology.build(scenario.seed)
        self.segments = self._new_segments()
        self.links = self._new_links()
        self.hosts = self._new_hosts()
        self._install_timeline()

    def _new_segments(self) -> SegmentStore:
        return SegmentStore.for_tier(
            self.topology,
            self.scenario.topology.resolved_tier,
            seed=self.scenario.seed,
            policy=self.scenario.beaconing,
            t0=self.clock.now,
            **self.scenario.search.kwargs(),
        )

    def _new_hosts(self) -> HostPopulation:
        return HostPopulation(
            self.topology,
            seed=self.scenario.seed,
            params=self.scenario.hosts,
            identity_policy=self.scenario.identity_policy,
        )

    def _new_links(self) -> LinkState:
        return LinkState(
            self.topology,
            seed=self.scenario.seed,
            params=self.scenario.link,
            background=self.scenario.background,
            t0=self.clock.now,
        )

    # ------------------------------------------------------------------ sizes

    @property
    def now(self) -> float:
        return self.clock.now

    @property
    def identity_policy(self) -> str:
        return self.scenario.identity_policy

    def __repr__(self) -> str:
        return (
            f"Substrate({self.scenario.name!r}, t={self.now:.1f}s, "
            f"{self.topology.n_ases} ASes, {self.segments.n_segments} segments, "
            f"generation={self.generation})"
        )

    # --------------------------------------------------------------- timeline

    def _install_timeline(self) -> None:
        for kind in EVENT_KINDS:
            self.clock.on(kind, self._apply)
        expanded: list[TimelineEvent] = list(self.scenario.timeline)
        for disturbance in self.scenario.disturbances:
            expanded.extend(
                disturbance.expand(self.topology, self.scenario.duration_s, self.scenario.seed)
            )
        #: What this world *does*, as against ``scenario.timeline``, which is what
        #: was written by hand. Read by ``instrument`` so a recovery metric knows
        #: when the bad day started; nothing the model can reach may read it.
        self.timeline: tuple[TimelineEvent, ...] = tuple(
            sorted(expanded, key=lambda e: (e.at_s, e.kind))
        )
        for event in self.timeline:
            self._validate(event)
            self.clock.at(
                event.at_s,
                event.kind,
                {"note": event.note, **event.params},
                priority=PRIORITY_SCENARIO,
            )

    def _validate(self, event: TimelineEvent) -> None:
        """Check an event's parameters now, not in hour nine of the run."""
        if event.kind.startswith(EXTENSION_PREFIX):
            return
        required: dict[str, tuple[str, ...]] = {
            "link_degrade": ("link", "factor"),
            "link_restore": (),
            "as_policy_filter": ("as_",),
            "as_policy_unfilter": (),
            "demand_surge": ("mbps",),
            "demand_clear": (),
            "topology_change": ("links",),
            "advisory_apply": ("src", "dst", "weights"),
        }
        missing = [k for k in required[event.kind] if k not in event.params]
        if missing:
            raise ValueError(f"{event.kind} at {event.at_s}s is missing parameter(s) {missing}")
        if event.kind == "link_degrade":
            link = int(event.params["link"])
            if not 0 <= link < self.topology.n_links:
                raise ValueError(
                    f"link_degrade at {event.at_s}s names link {link}, and the topology "
                    f"has {self.topology.n_links}"
                )
        if event.kind == "as_policy_filter":
            as_ = int(event.params["as_"])
            if not 0 <= as_ < self.topology.n_ases:
                raise ValueError(
                    f"as_policy_filter at {event.at_s}s names AS {as_}, and the topology "
                    f"has {self.topology.n_ases}"
                )
        if event.kind == "demand_surge":
            scope = event.params.get("scope")
            if scope is not None and len(scope) != 2:
                raise ValueError("demand_surge scope is a (src, dst) pair")

    def _apply(self, event: Event) -> None:
        handler = {
            "link_degrade": self._on_link_degrade,
            "link_restore": self._on_link_restore,
            "as_policy_filter": self._on_policy_filter,
            "as_policy_unfilter": self._on_policy_unfilter,
            "demand_surge": self._on_demand_surge,
            "demand_clear": self._on_demand_clear,
            "topology_change": self._on_topology_change,
            "advisory_apply": self._on_advisory_apply,
        }[event.kind]
        handler(event)

    def _on_link_degrade(self, event: Event) -> None:
        self.links.degrade(
            int(event.get("link")),
            float(event.get("factor")),
            direction=event.get("direction"),
        )

    def _on_link_restore(self, event: Event) -> None:
        link = event.get("link")
        self.links.restore(None if link is None else int(link))

    def _on_policy_filter(self, event: Event) -> None:
        """Segments vanish from path-server answers. The graph does not move.

        This is the failure the corrected domain model cares about: a path
        becomes unavailable without anything physical happening, so a model
        that reasons about availability as if it were connectivity is wrong in
        a way no link metric reveals.

        Installed as a standing policy rather than applied to the segments that
        exist right now, because core segments are discovered lazily and the
        ones composed after this event have to be caught too.
        """
        self.segments.apply_filter_policy(
            FilterPolicy.seeded(
                int(event.get("as_")),
                float(event.get("fraction", 1.0)),
                seed=self.scenario.seed,
                at_s=event.at_s,
            )
        )

    def _on_policy_unfilter(self, event: Event) -> None:
        self.segments.unfilter_segments()

    def _on_demand_surge(self, event: Event) -> None:
        """Scheduled load, for a scenario that wants congestion without hosts.

        M3 replaces this as the *normal* source of demand -- hosts acting on
        advice -- but a surge nobody chose is still part of the world, and
        conformance scenarios need one before the host population exists.
        """
        mbps = float(event.get("mbps"))
        scope = event.get("scope")
        if scope is None:
            ifaces = np.arange(self.links.n_ifaces)
        else:
            src, dst = int(scope[0]), int(scope[1])
            paths = self.paths_for(src, dst)
            if not paths:
                return
            ifaces = np.unique(np.concatenate([np.asarray(p.ifaces, np.int64) for p in paths]))
            mbps = mbps / max(len(paths), 1)
        self.links.add_demand(ifaces, np.full(ifaces.size, mbps, dtype=np.float64))
        self._surges[self._next_surge] = (ifaces, np.full(ifaces.size, mbps, dtype=np.float64))
        self._next_surge += 1

    def _on_demand_clear(self, event: Event) -> None:
        self.links.clear_demand()
        self._surges.clear()

    def _on_advisory_apply(self, event: Event) -> None:
        """An advisory reaching the hosts, however long after it was decided.

        Nothing here knows how long that was, and that is the point: the delay
        was spent on the clock, so between the decision and this handler the
        world went on stepping. See ADR 0009.
        """
        weights = {int(k): float(v) for k, v in dict(event.get("weights", {})).items()}
        self.hosts.publish(int(event.get("src")), int(event.get("dst")), weights, t=self.clock.now)
        self.n_advisories_applied += 1

    def _on_topology_change(self, event: Event) -> None:
        """The rare, planned one. Rebuilds segments and link state.

        ``without_links`` renumbers links and interfaces, so nothing indexed by
        them survives. That the rebuild is expensive is the correct incentive:
        ADR 0002 says physical change is rare and planned, and a cheap version
        of it would invite using it as the background churn it is not.
        """
        links = [int(x) for x in event.get("links", ())]
        scopes = [(s.src, s.dst, s.params) for s in self.hosts.scopes.values()]
        self.topology = self.topology.without_links(links)
        self.segments = self._new_segments()
        self.links = self._new_links()
        self.segments.advance_to(self.clock.now)
        self.links.advance_to(self.clock.now)
        # The populations survive; their path sets do not, because the
        # interface ids they were built from no longer mean what they meant.
        self.hosts = self._new_hosts()
        for src, dst, params in scopes:
            self.add_scope(src, dst, params=params)
        self._surges.clear()
        self._probe_load.clear()
        self.generation += 1

    # ------------------------------------------------------------------ steps

    def step(self, dt_s: float | None = None) -> int:
        """Advance one step: dispatch what falls due, then sync the substrate.

        Returns the number of events dispatched. The clock leads and the rest
        follow it, rather than each carrying its own idea of the time.

        A step longer than ``scenario.step_s`` is subdivided into ticks on the
        global grid rather than taken in one jump. That matters once the loop is
        closed: a probe that costs the model four seconds is four seconds of
        network, during which hosts resample four times and the load moves four
        times. Taking it as a single jump would let an expensive call buy the
        model a frozen world, which is invariant 3 backwards. Aligning the ticks
        to a grid rather than to wherever the caller happened to stop also keeps
        two runs that spent their time differently sampling the same instants.

        The order within a tick is load-bearing. Events first, so an advisory
        that fell due is in force before anybody acts on it. Then segments,
        because a scope's path set can change and the hosts spread over whatever
        it is now. Then hosts, who put their traffic on the network. Then the
        link state, which is what that traffic did. Hosts before links: the
        other way round, every scope reads the previous tick's congestion and
        the whole loop runs a tick behind itself.
        """
        step = self.scenario.step_s if dt_s is None else dt_s
        target = self.clock.now + step
        dispatched = 0
        while self.clock.now < target - 1e-9:
            dispatched += self._tick(self._next_tick(target))
        return dispatched

    def _next_tick(self, target: float) -> float:
        grid = self.scenario.step_s
        k = math.floor(self.clock.now / grid + 1e-9) + 1
        return min(k * grid, target)

    def _tick(self, t: float) -> int:
        dispatched = self.clock.run_until(t)
        self.segments.advance_to(t)
        if self._on_grid(t):
            self._apply_load(t, t - self._loaded_at)
            self._loaded_at = t
        self.links.advance_to(t)
        self.ticks += 1
        for tap in self.taps:
            tap(self)
        return dispatched

    def _on_grid(self, t: float) -> bool:
        """Whether ``t`` is a point where the population gets to act.

        A caller that stops mid-grid -- which is every tool call, since a call
        costs whatever it costs and not a whole step -- still moves the clock
        and the link state, but does not make the hosts resample. Otherwise a
        model that made twenty calls in a step would have shaken the population
        twenty times by doing nothing but asking questions, and the load series
        would carry the model's call pattern in it. The resample rate is a
        property of the hosts.
        """
        grid = self.scenario.step_s
        return abs(t / grid - round(t / grid)) < 1e-9

    def hold_probe_load(self, ifaces: Sequence[int] | np.ndarray, mbps: float) -> int:
        """Put a probe's own traffic on the network until it is released.

        A bandwidth test measures a link it is itself loading, and that load has
        to survive :meth:`_apply_load` rebuilding demand underneath it. Adding it
        to the link state directly does not: a grid tick inside the probe's two
        seconds wipes the contribution, and the matching subtraction then lands
        on a baseline that never carried it, leaving the interface at minus the
        probe's own rate until the next tick. See
        ``tests/test_exposure.py::test_a_bandwidth_probe_straddling_a_grid_tick_leaves_no_negative_load``.
        """
        handle = self._next_probe_load
        self._next_probe_load += 1
        which = np.asarray(ifaces, dtype=np.int64)
        rate = np.full(which.size, float(mbps), dtype=np.float64)
        self._probe_load[handle] = (which, rate)
        self.links.add_demand(which, rate)
        return handle

    def release_probe_load(self, handle: int) -> None:
        """Take a probe's traffic back off. Safe whether or not a grid tick
        rebuilt demand while it was in flight, because a rebuild includes it.

        A no-op if a topology change dropped the hold while the probe was in
        flight: the interface ids it was placed on no longer mean what they
        meant, and subtracting from whatever now wears those numbers would be
        worse than leaving it.
        """
        held = self._probe_load.pop(handle, None)
        if held is None:
            return
        which, rate = held
        self.links.add_demand(which, -rate)

    def _apply_load(self, t: float, dt_s: float) -> None:
        """Hosts offer, surges and in-flight probes add, and the total is
        written in one go.

        Recomputed from scratch rather than adjusted, so nothing accumulates a
        rounding error over a 3,600-second run and reports it as a finding.
        """
        if not self.hosts.scopes and not self._surges and not self._probe_load:
            return
        total = self.hosts.step(t, dt_s, self.links).copy()
        for ifaces, mbps in self._surges.values():
            np.add.at(total, ifaces, mbps)
        for ifaces, mbps in self._probe_load.values():
            np.add.at(total, ifaces, mbps)
        self.links.set_all_demand(total)

    def run(self, until_s: float | None = None) -> int:
        """Step to ``until_s``, or to the scenario's duration. Returns steps taken."""
        target = self.scenario.duration_s if until_s is None else until_s
        steps = 0
        while self.clock.now < target - 1e-9:
            self.step(min(self.scenario.step_s, target - self.clock.now))
            steps += 1
        return steps

    # ------------------------------------------------------------------ views

    def paths_for(self, src: int, dst: int, *, limit: int | None = None) -> list[Path]:
        """Paths for one scope. Materialised lazily; nobody enumerates all of them."""
        return self.segments.paths_for(src, dst, limit=limit)

    def path_ids(self, paths: Sequence[Path]) -> list[int]:
        """Whichever identifier this scenario's policy says ``path_id`` means."""
        policy = self.scenario.identity_policy
        return [p.path_id(policy) for p in paths]

    # ------------------------------------------------------------- closed loop

    def add_scope(
        self,
        src: int,
        dst: int,
        *,
        limit: int | None = None,
        params: HostParams | None = None,
    ) -> ScopeState:
        """Put a host population on one (src, dst) pair.

        Scopes are registered by whoever is running the experiment rather than
        by the scenario, because which pairs are interesting is a property of
        the run and not of the world. What the population is like *is* a
        property of the world, and lives in ``scenario.hosts``.
        """
        return self.hosts.add_scope(src, dst, self.paths_for(src, dst, limit=limit), params=params)

    def refresh_scopes(self, *, limit: int | None = None) -> int:
        """Re-materialise every scope's path set. Returns how many changed.

        Deliberately explicit. The path set going stale under a population is a
        real thing that happens, and a substrate that silently re-derived it
        every step would delete the failure this project is trying to observe.
        """
        changed = 0
        for src, dst in list(self.hosts.scopes):
            before = self.hosts.scopes[(src, dst)].path_ids
            scope = self.hosts.refresh(src, dst, self.paths_for(src, dst, limit=limit))
            if scope is not None and scope.path_ids != before:
                changed += 1
        return changed

    def publish_advisory(
        self,
        src: int,
        dst: int,
        weights: Mapping[int, float],
        *,
        latency_s: float = 0.0,
    ) -> float:
        """Schedule an advisory to reach the hosts at ``now + latency_s``.

        Returns the time it will land. Zero latency still goes through the
        queue: it lands at the top of the next step, ahead of the hosts, rather
        than reaching into the population from inside the decision. The clock
        refuses to schedule into the past, so no amount of arithmetic elsewhere
        can apply a decision to the state it was computed from (invariant 3).
        """
        at = self.clock.now + max(0.0, float(latency_s))
        self.clock.at(
            at,
            "advisory_apply",
            {
                "src": int(src),
                "dst": int(dst),
                "weights": {int(k): float(v) for k, v in weights.items()},
            },
            priority=PRIORITY_WORLD,
        )
        return at

    # ------------------------------------------------------------- determinism

    def digest(self) -> str:
        """One hash over the whole world and everything that happened to it.

        This is the M1 acceptance criterion: same seed, two processes, same
        string. It covers the scenario, the graph, the segments, the link state
        and the event trace, so a difference in any of them shows up here.
        """
        return (
            TraceHash(label="substrate")
            .update(
                {
                    "scenario": self.scenario.digest(),
                    "topology": self.topology.digest(),
                    "segments": self.segments.digest(),
                    "links": self.links.digest(),
                    "hosts": self.hosts.digest(),
                    "clock": self.clock.digest(),
                    "generation": self.generation,
                    "t": round(self.now, 9),
                }
            )
            .short(32)
        )
