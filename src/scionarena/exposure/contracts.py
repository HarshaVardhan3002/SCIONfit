"""The plug.

Every model that wants to be checked by scionfit implements ``PathModel``.
Everything else in this package is built against these types and nothing else.

Design rules, in priority order:

1.  A twenty-line moving-average baseline must be able to implement this.
    If the interface is only implementable by a large neural network it is
    wrong, because then it cannot express the baselines we compare against
    and it cannot express the incumbent (the Path Oracle) either.

2.  A model must be able to declare what it does *not* do.  A pure forecaster
    is a legitimate object of study -- we need to be able to run one and watch
    it fail -- so declaring ``demand_conditioned=False`` yields DECLARED_ABSENT
    on the relevant probes, not a crash and not a silent zero.

3.  Nothing here imports numpy, torch, or a SCION library.  The interface is
    plain Python so that implementing it costs a model author nothing.

4.  This module imports **nothing from the rest of the project**, and in
    particular nothing from ``core``.  That is why the agentic entry point takes
    a ``SessionLike`` protocol declared here rather than the real ``Session``:
    the module a model imports has no edge into the substrate at all, and
    import-linter can prove it.  See ADR 0008.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "InterfaceAttrs",
    "PathRef",
    "TopologySnapshot",
    "Observation",
    "Demand",
    "Dist",
    "Prediction",
    "Advisory",
    "Capabilities",
    "PathModel",
    "SessionLike",
    "ToolUsingModel",
    "SLA",
]

LinkType = str  # "core" | "parent_child" | "peering"


# --------------------------------------------------------------------------
# topology
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceAttrs:
    """What a beacon tells you about one SCION interface.

    Everything here is available for an interface first seen one second ago.
    That is deliberate: a model needing more than this cannot satisfy R4
    (generalise to interfaces not seen during training).
    """

    iface_id: str
    as_id: str
    isd: int
    link_type: LinkType
    declared_bw_mbps: float | None = None
    declared_latency_ms: float | None = None
    mtu: int | None = None


@dataclass(frozen=True)
class PathRef:
    """An end-to-end path: an ordered sequence of interfaces."""

    path_id: str
    src: str
    dst: str
    interfaces: tuple[str, ...]
    expiry_s: float | None = None  # seconds from snapshot time
    mtu: int | None = None

    @property
    def hop_count(self) -> int:
        return len(self.interfaces)


@dataclass(frozen=True)
class TopologySnapshot:
    """The graph as of time ``t``.

    Path sets churn and interfaces appear and disappear.  A model is handed a
    fresh snapshot on every call and must cope with the difference.
    """

    t: float
    interfaces: Mapping[str, InterfaceAttrs]
    paths: tuple[PathRef, ...]

    def paths_for(self, src: str, dst: str) -> tuple[PathRef, ...]:
        return tuple(p for p in self.paths if p.src == src and p.dst == dst)

    def paths_using(self, iface_id: str) -> tuple[PathRef, ...]:
        return tuple(p for p in self.paths if iface_id in p.interfaces)


# --------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One measurement of one path at one time.

    Any metric may be ``None``, meaning *not measured*.  That is not the same
    as measured-and-zero; probe R3 exists to check a model tells them apart.
    """

    t: float
    path_id: str
    latency_ms: float | None = None
    throughput_mbps: float | None = None
    loss: float | None = None
    source: str = "unknown"  # "bbr" | "scmp" | "bwtest" | "beacon" | "idint"


@dataclass(frozen=True)
class Demand:
    """Offered load as a fraction of total traffic for a scope.

    This is the input that makes a model closed-loop capable.  ``per_path``
    should sum to 1 over the paths of one (src, dst) scope; the harness
    normalises and never assumes the model does.

    **Intended and realised are two different things** (ADR 0023).
    ``per_path`` is what was *asked for* -- it is built from a published
    advisory -- and ``realised`` is what telemetry saw. They differ by exactly
    the sampling noise, the defectors, the dwell timers and the hysteresis, and
    ``exposure/streams.py`` has always said so while the probes were handed the
    first and told it was the second: "a model that assumes its advice was
    followed exactly is wrong by exactly that much".
    """

    per_path: Mapping[str, float]
    n_hosts: int = 1
    #: What was actually measured on each path, where anything was. ``None``
    #: means no telemetry at all; a path absent from a non-``None`` mapping
    #: means no telemetry *for that path*, which is ignorance rather than a
    #: measured zero. A probe that treated the two the same would score a model
    #: for a path nobody was using.
    realised: Mapping[str, float] | None = None

    @property
    def intended(self) -> Mapping[str, float]:
        """What was asked for. The same object as ``per_path``, named."""
        return self.per_path

    def gap(self) -> Mapping[str, float]:
        """``realised - intended`` per path, over the paths telemetry covered.

        Empty when nothing was measured. This is the quantity a performativity
        probe is about: how far the world went from where the model pointed it.
        """
        if self.realised is None:
            return {}
        want = self.normalised()
        return {k: v - want.get(k, 0.0) for k, v in self.realised.items()}

    def normalised(self) -> Mapping[str, float]:
        tot = sum(max(0.0, v) for v in self.per_path.values())
        if tot <= 0:
            n = max(1, len(self.per_path))
            return dict.fromkeys(self.per_path, 1.0 / n)
        return {k: max(0.0, v) / tot for k, v in self.per_path.items()}


# --------------------------------------------------------------------------
# predictions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Dist:
    """A predicted quantity.

    A point estimate is expressible (``mean`` only) so baselines can be
    written; ``is_distributional`` is then False and probe R5 records it.
    """

    mean: float | None = None
    quantiles: Mapping[float, float] | None = None

    @property
    def is_distributional(self) -> bool:
        return self.quantiles is not None and len(self.quantiles) >= 3

    @property
    def point(self) -> float:
        if self.mean is not None:
            return self.mean
        if self.quantiles:
            ks = sorted(self.quantiles)
            return self.quantiles[ks[len(ks) // 2]]
        return float("nan")

    @property
    def spread(self) -> float:
        """Interquantile width; 0.0 for a point estimate."""
        if not self.quantiles or len(self.quantiles) < 2:
            return 0.0
        ks = sorted(self.quantiles)
        return float(self.quantiles[ks[-1]] - self.quantiles[ks[0]])

    def quantiles_monotone(self) -> bool:
        if not self.quantiles:
            return True
        ks = sorted(self.quantiles)
        vs = [self.quantiles[k] for k in ks]
        return all(a <= b + 1e-9 for a, b in zip(vs, vs[1:], strict=False))

    @staticmethod
    def point_estimate(v: float) -> Dist:
        return Dist(mean=v)


@dataclass(frozen=True)
class Prediction:
    """What a model says about one path over one horizon."""

    latency_ms: Dist
    throughput_mbps: Dist
    loss: Dist
    confidence: float | None = None  # [0,1]; None = not reported

    def cost(self) -> float:
        """Scalar badness for the monotonicity and demand-sensitivity probes.

        Deliberately crude.  Probes only compare this against itself under a
        controlled change, never across models.
        """
        import math

        lat = self.latency_ms.point
        bw = self.throughput_mbps.point
        ls = self.loss.point
        lat = 0.0 if math.isnan(lat) else lat
        ls = 0.0 if math.isnan(ls) else ls
        inv_bw = 0.0 if (math.isnan(bw) or bw <= 0) else 1000.0 / bw
        return lat + inv_bw + 1000.0 * ls


@dataclass(frozen=True)
class Advisory:
    """What gets published to hosts.

    ``weights`` is a probability distribution over path ids for one scope.
    A model that ranks rather than distributes returns a one-hot, which is
    legal; probe R8 records exactly that.
    """

    weights: Mapping[str, float]
    temperature: float | None = None
    solver_converged: bool | None = None
    solver_iterations: int | None = None
    confidence: float | None = None
    reason: str = ""

    def normalised(self) -> Mapping[str, float]:
        tot = sum(max(0.0, v) for v in self.weights.values())
        if tot <= 0:
            n = max(1, len(self.weights))
            return dict.fromkeys(self.weights, 1.0 / n)
        return {k: max(0.0, v) / tot for k, v in self.weights.items()}

    @property
    def max_weight(self) -> float:
        w = self.normalised()
        return max(w.values()) if w else 1.0

    @property
    def entropy(self) -> float:
        from math import log

        return -sum(v * log(v) for v in self.normalised().values() if v > 0)

    @property
    def normalised_entropy(self) -> float:
        """0 = one-hot (a ranking), 1 = uniform (round robin)."""
        from math import log

        n = len(self.weights)
        if n <= 1:
            return 0.0
        return self.entropy / log(n)


# --------------------------------------------------------------------------
# SLA
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SLA:
    """What the application wants.

    Presets mirror the ScionPathML QoE profiles so their Task 4 can be
    replayed through this interface without redefining anything.
    """

    name: str = "bulk"
    max_rtt_ms: float | None = None
    max_loss: float | None = None
    min_bw_mbps: float | None = None

    @staticmethod
    def presets() -> dict[str, SLA]:
        return {
            "video_conference": SLA("video_conference", 150, 0.02, 1.0),
            "online_gaming": SLA("online_gaming", 50, 0.01, 0.5),
            "file_transfer": SLA("file_transfer", 500, 0.05, 10.0),
            "browsing": SLA("browsing", 300, 0.03, 0.1),
            "streaming": SLA("streaming", 200, 0.01, 5.0),
            "bulk": SLA("bulk"),
        }


# --------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------


@dataclass
class Capabilities:
    """A model's own account of what it does.

    The harness cross-checks every declaration behaviourally.  Declaring a
    capability you do not have is the one thing producing a hard FAIL rather
    than a note: an honest limitation is fine, a false claim is not.
    """

    name: str
    version: str = "0.0.0"
    authors: str = ""

    #: What kind of thing this is, for the grouping the benchmark exists to do:
    #: find the best architecture, then the best variant within it. Conventional
    #: values are ``persistence``, ``ewma``, ``gbdt``, ``gnn``, ``dqn``,
    #: ``llm_api`` and ``llm_local``, but the field is deliberately not
    #: validated against an enumeration -- a list of known architectures living
    #: in the one module every model author imports would make adding an
    #: architecture a change to the contract. An unrecognised tag groups by
    #: itself, which is the right behaviour for something new. Empty means the
    #: model did not say, and a report prints that rather than guessing from the
    #: class name (ADR 0019).
    architecture: str = ""

    distributional: bool = False  # R5
    demand_conditioned: bool = False  # R6
    monotone_in_demand: bool = False  # R7
    emits_assignment: bool = False  # R8
    self_consistent: bool = False  # R9
    staleness_aware: bool = False  # R10
    handles_unseen_interfaces: bool = False  # R4
    composes_unseen_paths: bool = False  # R2
    reports_confidence: bool = False
    #: Defaults to True, which makes it the one declaration a model can claim
    #: by not thinking about it. A stateless model must say so explicitly;
    #: ``scionarena adapt`` checks it behaviourally and found the inherited
    #: default on a shipped baseline the first time it ran.
    stateful: bool = True

    #: Which of the requirement classes of the spec this model is for:
    #: ``latency``, ``bandwidth``, ``loss``, or empty for a model that did not
    #: say. It decides which *layer* the model is allowed to rank on -- a
    #: latency-class model ranks on the static layer plus liveness and lets the
    #: dynamic layer widen its intervals, never reorder them, because ranking on
    #: a fifteen-second-old congestion estimate is routing on lagged load. Probe
    #: R11 checks it (ADR 0023). Empty is NOT_APPLICABLE rather than a failure:
    #: a model that makes no claim has not claimed anything R11 can contradict.
    requirement_class: str = ""

    #: Implements ``act`` and drives itself through the tool registry rather
    #: than being driven through observe/predict/advise.
    uses_tools: bool = False
    #: Decides for itself what to keep from the raw log. A model declaring this
    #: is claiming it will not fall over when the history outgrows its context,
    #: which is what M8 goes looking for.
    manages_own_memory: bool = False
    #: Returns before the deadline it was given. Cross-checked against the
    #: session log, where a late call is flagged rather than refused.
    respects_deadline: bool = False

    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# the model protocol
# --------------------------------------------------------------------------


@runtime_checkable
class PathModel(Protocol):
    """Implement this and scionfit can check you.

    Lifecycle per run::

        m.reset(topo, seed)
        loop:
            m.observe(observations, topo)
            preds = m.predict(topo, paths, horizon_s, demand)
            adv   = m.advise(topo, paths, sla, n_hosts)

    ``predict`` must tolerate ``demand=None`` and must tolerate paths whose
    interfaces are absent from the topology it was reset with.  Raising in
    either case is a recorded outcome, not a harness bug.
    """

    capabilities: Capabilities

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        """Start a fresh episode.  Must clear per-episode state."""
        ...

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        """Ingest measurements.  May be called with an empty sequence."""
        ...

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> Mapping[str, Prediction]:
        """Predict metrics for each path at ``t + horizon_s``.

        If ``demand`` is supplied the prediction should be conditioned on it.
        A model that ignores it should declare ``demand_conditioned=False``.
        """
        ...

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        """Return the distribution over paths that hosts should sample from."""
        ...


# --------------------------------------------------------------------------
# the agentic entry point
# --------------------------------------------------------------------------


@runtime_checkable
class SessionLike(Protocol):
    """The handle an agentic model is given. One episode, one model.

    Structural on purpose.  The real object lives in ``exposure.session`` and
    owns the substrate; this declares only what a model may touch, and declaring
    it *here* is what keeps this module free of any import that could reach the
    network state.  ``call`` returns a result object with ``ok``, ``data``,
    ``error`` and ``cost``; it is typed loosely because naming its class would
    reintroduce the import this protocol exists to avoid.

    Every call costs something and a refused call is a returned result, never an
    exception.  Rate limits, exhausted budgets, timed-out probes and paths that
    have stopped being offered all arrive this way, and handling them is part of
    the job rather than an error path.
    """

    @property
    def now(self) -> float:
        """Simulated seconds. It moves while you work."""
        ...

    def call(self, tool: str, **arguments: Any) -> Any:
        """Invoke a tool by name. See the registry for names and schemas."""
        ...

    def view(self) -> TopologySnapshot:
        """What previous calls have told you, timestamped when they told you."""
        ...

    def known_paths(self, src: str | None = None, dst: str | None = None) -> list[PathRef]: ...

    def drain(self, handle: str) -> Sequence[Any]:
        """Take what a subscription has delivered since the last drain."""
        ...

    def advance(self, dt_s: float) -> None:
        """Spend simulated time without doing anything."""
        ...


@runtime_checkable
class ToolUsingModel(PathModel, Protocol):
    """A model that drives itself.

    ``act`` is handed the session and a deadline in simulated seconds and may
    make any sequence of calls it can afford. Returning late is permitted and
    recorded -- the network does not wait, so a decision that took too long is
    applied to a world that moved on, which is the failure worth measuring.

    A ``ToolUsingModel`` still implements the four ``PathModel`` methods, so the
    same object can be run through the fixed cycle for comparison.
    """

    def act(self, session: SessionLike, deadline_s: float) -> None: ...
