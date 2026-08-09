"""One model, one episode. The object every front-end drives.

A ``Session`` owns the substrate, the budget, the rate limiters, the streams and
the log, and hands the model none of them. The model gets tool results and
whatever it chose to remember.

Three properties are load-bearing, and each is enforced here rather than
documented:

**The view is an accumulation, not a window.** :meth:`Session.view` returns a
snapshot assembled from what previous tool calls returned. Nothing else writes
to it. A model that makes no calls sees its initial snapshot forever, including
the parts that have since stopped being true.

**Charging a call advances the world.** Wall clock is charged and simulated time
moves by the same amount in the same code path, so invariant 3 cannot be
forgotten. A two-second bandwidth test is answered by a network two seconds
older, and a model that spends its budget looking finds the thing it was looking
at has changed.

**A refused call is a result, not an exception.** Rate limits, budget exhaustion,
timeouts and vanished paths all come back as ``ToolResult(ok=False)`` with a code.
The episode continues, and what the model does about it is the measurement.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.clock import Stopwatch
from ..core.scenario import Scenario, Substrate
from ..core.segments import Path
from ..core.trace import TraceHash
from .budget import Budget, Cost, RateLimiter
from .contracts import (
    SLA,
    Advisory,
    Demand,
    InterfaceAttrs,
    Observation,
    PathModel,
    PathRef,
    TopologySnapshot,
)
from .streams import STREAMS, EventLog, RawEvent, Subscription
from .tools import (
    BWTEST_LOAD_MBPS,
    BWTEST_S,
    TOOLS,
    PathInfo,
    ToolError,
    ToolSpec,
    probe_cost,
    validate_arguments,
)

__all__ = [
    "Session",
    "ToolResult",
    "LogRecord",
    "drive_episode",
    "run_episode",
]

#: How long an unanswered echo waits before the caller gives up.
PROBE_TIMEOUT_S = 1.0
#: Jitter on a single echo, as a fraction of the path's latency. One sample is
#: one sample; a model that treats it as the truth should be able to be wrong.
ECHO_JITTER = 0.05


@dataclass(frozen=True)
class ToolResult:
    """What a tool call returned, successful or not."""

    tool: str
    ok: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    message: str = ""
    cost: Cost = Cost()
    t_s: float = 0.0
    retry_after_s: float | None = None

    def __bool__(self) -> bool:
        return self.ok

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "t": round(self.t_s, 6),
            "cost": self.cost.to_dict(),
        }
        if self.ok:
            out["data"] = dict(self.data)
        else:
            out["error"] = self.error
            out["message"] = self.message
            if self.retry_after_s is not None:
                out["retry_after_s"] = round(self.retry_after_s, 6)
        return out


@dataclass(frozen=True)
class LogRecord:
    """One line of the session's own history.

    Complete enough to replay: name, arguments, outcome, cost and both
    timestamps. M4 reads this for operational metrics and M8 reads it for memory
    tests, so it records refusals as carefully as successes.
    """

    seq: int
    kind: str  # "call" | "advance" | "tokens"
    t_start_s: float
    t_end_s: float
    name: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str = ""
    cost: Cost = Cost()
    return_bytes: int = 0
    #: True if the call arrived after the deadline the model was given. Not
    #: refused -- a late decision is applied late, which is the interesting case.
    late: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "t_start_s": round(self.t_start_s, 9),
            "t_end_s": round(self.t_end_s, 9),
            "name": self.name,
            "args": dict(sorted(self.args.items())),
            "ok": self.ok,
            "error": self.error,
            "cost": self.cost.to_dict(),
            "return_bytes": self.return_bytes,
            "late": self.late,
        }


def _hex(value: int) -> str:
    return f"{value & 0xFFFFFFFFFFFFFFFF:016x}"


class Session:
    """The exposure boundary, instantiated.

    Satisfies :class:`~scionarena.exposure.tools.ToolContext` structurally, and
    :class:`~scionarena.exposure.contracts.SessionLike` -- which is what a model
    is written against, and which imports nothing from ``core``.
    """

    def __init__(
        self,
        world: Substrate,
        *,
        budget: Budget | None = None,
        seed: int = 0,
        registry: Mapping[str, ToolSpec] | None = None,
        charge_real_time: bool = False,
        stopwatch: Stopwatch | None = None,
        label: str = "session",
    ) -> None:
        self._world = world
        self.label = label
        self.seed = seed
        self.budget = budget if budget is not None else Budget.unlimited()
        self.tools: Mapping[str, ToolSpec] = TOOLS if registry is None else registry
        self.limiter = RateLimiter()
        self.events = EventLog()
        self.log: list[LogRecord] = []
        self.subscriptions: dict[str, Subscription] = {}
        self.advisories: list[dict[str, Any]] = []
        #: Set by a driver before handing control to a tool-using model.
        self.deadline_s: float | None = None
        self.charge_real_time = charge_real_time
        self.stopwatch = stopwatch if stopwatch is not None else Stopwatch()

        self._rng = random.Random(seed)
        self._seq = 0
        self._prepaid_s = 0.0
        # --- the model's view, which only tool results write to ---------------
        self._known_ifaces: dict[str, InterfaceAttrs] = {}
        self._known_paths: dict[str, PathRef] = {}
        self._view_t = world.now
        # --- what the substrate is asked about on each step -------------------
        self._infos: dict[str, PathInfo] = {}
        self._scopes: dict[tuple[int, int], frozenset[str]] = {}
        self._traffic: dict[str, float] = {}  # path_id -> advisory weight
        self._probed: set[str] = set()
        self._epoch = world.segments.epoch

    # ------------------------------------------------------------------ basics

    @property
    def now(self) -> float:
        return self._world.now

    @property
    def n_calls(self) -> int:
        return sum(1 for r in self.log if r.kind == "call")

    def __repr__(self) -> str:
        return (
            f"Session({self.label!r}, t={self.now:.2f}s, calls={self.n_calls}, "
            f"paths_known={len(self._known_paths)}, {self.budget!r})"
        )

    # ------------------------------------------------------------------- view

    def view(self) -> TopologySnapshot:
        """Everything the model has been told, and nothing else.

        Timestamped with when the *last* tool result arrived, not with now. The
        difference between the two is exactly the staleness the model is
        supposed to be reasoning about, and hiding it by stamping ``now`` would
        make R10 unmeasurable.
        """
        return TopologySnapshot(
            t=self._view_t,
            interfaces=dict(self._known_ifaces),
            paths=tuple(self._known_paths.values()),
        )

    def known_paths(self, src: str | None = None, dst: str | None = None) -> list[PathRef]:
        return [
            p
            for p in self._known_paths.values()
            if (src is None or p.src == src) and (dst is None or p.dst == dst)
        ]

    def forget(self, path_ids: Iterable[str]) -> None:
        """Drop paths from the view. Offered so a model can manage its own
        memory; the harness never calls it."""
        for pid in path_ids:
            self._known_paths.pop(pid, None)

    # ------------------------------------------------------------------ calls

    def call(self, tool: str, **arguments: Any) -> ToolResult:
        """Run one tool. Charges it, advances the world, logs it, returns it."""
        t0 = self.now
        spec = self.tools.get(tool)
        if spec is None:
            return self._refuse(
                tool, arguments, t0, "unknown_tool", f"no tool named {tool!r}", Cost()
            )

        try:
            args = validate_arguments(spec, arguments)
        except ToolError as err:
            return self._refuse(tool, arguments, t0, err.code, err.message, spec.refusal_cost)

        # Rate limits are keyed on the thing that is limited -- the answering
        # border router, the destination path server -- so the key needs the
        # substrate and can itself fail on an argument that names nothing.
        limit = spec.limit(args)
        if limit is not None:
            try:
                key = spec.key(self, args)
            except ToolError as err:
                return self._refuse(
                    tool, args, t0, err.code, err.message, err.cost or spec.refusal_cost
                )
            if not self.limiter.allow(key, self.now, limit):
                wait = self.limiter.retry_after_s(key, self.now, limit)
                return self._refuse(
                    tool,
                    args,
                    t0,
                    "rate_limited",
                    f"{key} permits {limit.calls:g} per {limit.per_s:g}s",
                    spec.refusal_cost,
                    retry_after_s=wait,
                )

        shortfall = self.budget.shortfall(spec.reserve)
        if shortfall is not None:
            self.budget.refuse(shortfall)
            return self._refuse(
                tool,
                args,
                t0,
                "budget_exhausted",
                f"no {shortfall} left for {tool}",
                spec.refusal_cost,
                count_refusal=False,
            )

        self._prepaid_s = 0.0
        try:
            payload = spec.handler(self, args)
        except ToolError as err:
            return self._refuse(
                tool, args, t0, err.code, err.message, err.cost or spec.refusal_cost
            )

        cost = spec.cost(args, payload)
        shortfall = self.budget.shortfall(cost)
        if shortfall is not None:
            # Affordable to start, not affordable to receive. The call happened,
            # so it is charged and the result is withheld: this is the shape of
            # running out mid-flight and the model has to cope with it.
            self.budget.refuse(shortfall)
            return self._refuse(tool, args, t0, "budget_exhausted", f"ran out of {shortfall}", cost)

        self.budget.charge(cost)
        self._advance(max(0.0, cost.wall_clock_s - self._prepaid_s))
        self._prepaid_s = 0.0
        self._record("call", t0, tool, args, True, "", cost, cost.nbytes)
        self._view_t = self.now
        return ToolResult(tool=tool, ok=True, data=payload, cost=cost, t_s=self.now)

    def _refuse(
        self,
        tool: str,
        args: Mapping[str, Any],
        t0: float,
        code: str,
        message: str,
        cost: Cost,
        *,
        retry_after_s: float | None = None,
        count_refusal: bool = True,
    ) -> ToolResult:
        self.budget.charge(cost)
        self._advance(cost.wall_clock_s)
        self._record("call", t0, tool, args, False, code, cost, 0)
        return ToolResult(
            tool=tool,
            ok=False,
            error=code,
            message=message,
            cost=cost,
            t_s=self.now,
            retry_after_s=retry_after_s,
        )

    def _record(
        self,
        kind: str,
        t0: float,
        name: str,
        args: Mapping[str, Any],
        ok: bool,
        error: str,
        cost: Cost,
        return_bytes: int,
    ) -> None:
        late = self.deadline_s is not None and t0 > self.deadline_s
        self.log.append(
            LogRecord(
                seq=self._seq,
                kind=kind,
                t_start_s=t0,
                t_end_s=self.now,
                name=name,
                args=dict(args),
                ok=ok,
                error=error,
                cost=cost,
                return_bytes=return_bytes,
                late=late,
            )
        )
        self._seq += 1

    # --------------------------------------------------------------- the clock

    def advance(self, dt_s: float) -> None:
        """Let the world run for ``dt_s`` seconds of simulated time.

        A driver calls this between cycles; a model calls it to think. Either
        way the network moves and the streams fill, which is the point.
        """
        t0 = self.now
        self._advance(dt_s)
        self._record("advance", t0, "advance", {"dt_s": dt_s}, True, "", Cost(dt_s), 0)

    def _advance(self, dt_s: float) -> None:
        if dt_s <= 0.0:
            return
        self._world.step(dt_s)
        self._harvest()

    def think(self, tokens: int = 0) -> bool:
        """Charge the optional compute budget. False means it has run out."""
        return self.budget.spend_tokens(tokens)

    # ------------------------------------------------- ToolContext: the world

    def _as_index(self, name: str) -> int:
        try:
            return self._world.topology.as_index(name)
        except (KeyError, ValueError) as err:
            raise ToolError("unknown_as", f"no AS named {name!r}") from err

    def _info(self, path: Path, src: str, dst: str) -> PathInfo:
        topo = self._world.topology
        policy = self._world.identity_policy
        ifaces = tuple(topo.iface_name(int(i)) for i in path.ifaces)
        attrs = tuple(self._iface_attrs(int(i)) for i in path.ifaces)
        remaining = max(0.0, path.expiry_s - self.now)
        ref = PathRef(
            path_id=_hex(path.path_id(policy)),
            src=src,
            dst=dst,
            interfaces=ifaces,
            expiry_s=remaining,
            mtu=min((int(topo.link_mtu[link]) for link in path.links), default=None),
        )
        # Age of the freshest thing this path is made of, read back out of the
        # expiry rather than by looking segments up: a path's expiry is the
        # minimum over its components, and the components' ids here are
        # structural, not store indices.
        lifetime = self._world.scenario.beaconing.lifetime_s
        return PathInfo(
            ref=ref,
            structural_id=_hex(path.structural_id),
            segment_id=_hex(path.segment_id),
            expiry_s=remaining,
            beacon_age_s=max(0.0, lifetime - remaining),
            interfaces=attrs,
            handle=path,
        )

    def _iface_attrs(self, iface: int) -> InterfaceAttrs:
        topo = self._world.topology
        link = topo.link_of_iface(iface)
        rel = int(topo.link_rel[link])
        return InterfaceAttrs(
            iface_id=topo.iface_name(iface),
            as_id=topo.as_name(topo.as_of_iface(iface)),
            isd=int(topo.as_isd[topo.as_of_iface(iface)]),
            link_type=("core", "parent_child", "peering")[rel],
            declared_bw_mbps=float(topo.link_capacity_mbps[link]),
            declared_latency_ms=float(topo.link_latency_ms[link]),
            mtu=int(topo.link_mtu[link]),
        )

    def lookup_paths(self, src: str, dst: str, limit: int) -> list[PathInfo]:
        a, b = self._as_index(src), self._as_index(dst)
        paths = self._world.paths_for(a, b, limit=limit)
        infos = [self._info(p, src, dst) for p in paths]
        # The view is written here and nowhere else.
        for info in infos:
            self._infos[info.ref.path_id] = info
            self._known_paths[info.ref.path_id] = info.ref
            for attrs in info.interfaces:
                self._known_ifaces[attrs.iface_id] = attrs
        self._scopes[(a, b)] = frozenset(i.ref.path_id for i in infos)
        return infos

    def path_info(self, path_id: str) -> PathInfo:
        info = self._infos.get(path_id)
        if info is None:
            raise ToolError("unknown_path", f"{path_id} was never returned by query_paths")
        fresh = self._refresh(info)
        if fresh is None:
            raise ToolError(
                "path_unavailable",
                f"{path_id} is no longer offered for {info.ref.src}->{info.ref.dst}",
            )
        return fresh

    def _refresh(self, info: PathInfo) -> PathInfo | None:
        """Is this path still on offer, and what is it now?

        A path can disappear through AS policy filtering or expiry without any
        link going anywhere, and under ``crypto_bound`` identity it can also
        disappear by being re-signed. Looking it up again is how the model finds
        out -- there is no notification, because there is no notification in
        SCION either.
        """
        a = self._as_index(info.ref.src)
        b = self._as_index(info.ref.dst)
        for path in self._world.paths_for(a, b):
            if _hex(path.path_id(self._world.identity_policy)) == info.ref.path_id:
                current = self._info(path, info.ref.src, info.ref.dst)
                self._infos[current.ref.path_id] = current
                return current
        return None

    def border_router(self, path_id: str) -> str:
        """Which border router answers a probe on this path.

        The router owning the first egress interface. Two probes leaving through
        the same interface contend for the same SCMP allowance; two leaving the
        same AS through different interfaces do not, and spreading them is the
        one mitigation a model actually has.
        """
        info = self.path_info(path_id)
        path: Path = info.handle
        if not path.ifaces:
            raise ToolError("path_unavailable", f"{path_id} has no interfaces")
        return f"br:{self._world.topology.iface_name(int(path.ifaces[0]))}"

    def measure(self, info: PathInfo, kind: str) -> dict[str, Any]:
        """Take one measurement. Every kind of probe here can fail."""
        path: Path = info.handle
        metrics = self._world.links.path_metrics(path.ifaces)
        rtt_s = 2.0 * metrics.latency_ms / 1000.0
        # Probing a path is traffic on it, so telemetry sees it next step even
        # if no advisory ever sent anything that way.
        self._probed.add(info.ref.path_id)

        if kind == "latency":
            if self._rng.random() < metrics.loss:
                raise ToolError(
                    "probe_timeout",
                    f"no reply within {PROBE_TIMEOUT_S}s",
                    cost=probe_cost("latency", PROBE_TIMEOUT_S),
                )
            jitter = 1.0 + self._rng.uniform(-ECHO_JITTER, ECHO_JITTER)
            return {
                "latency_ms": metrics.latency_ms * jitter,
                "throughput_mbps": None,  # not measured, which is not zero
                "loss": None,
                "rtt_s": rtt_s,
                "source": "scmp",
            }

        if kind == "loss":
            # Twenty echoes, reported one by one. The ratio is not computed
            # here: a harness that hands over "loss = 0.15" has done the
            # model's work, and invariant 1 says it does not.
            samples: list[float | None] = [
                None
                if self._rng.random() < metrics.loss
                else metrics.latency_ms * (1.0 + self._rng.uniform(-ECHO_JITTER, ECHO_JITTER))
                for _ in range(20)
            ]
            if all(s is None for s in samples):
                raise ToolError(
                    "probe_timeout",
                    "every echo was lost",
                    cost=probe_cost("loss", rtt_s),
                )
            return {"samples_ms": samples, "n_sent": 20, "rtt_s": rtt_s, "source": "scmp"}

        # A bandwidth test is traffic. It loads the path it measures, for as
        # long as it runs, and then the load goes away. See ADR 0008.
        egress = [int(i) for i in path.ifaces[::2]]
        for iface in egress:
            self._world.links.add_demand(iface, BWTEST_LOAD_MBPS)
        self._advance(BWTEST_S)
        self._prepaid_s += BWTEST_S
        loaded = self._world.links.path_metrics(path.ifaces)
        for iface in egress:
            self._world.links.add_demand(iface, -BWTEST_LOAD_MBPS)
        achieved = min(BWTEST_LOAD_MBPS, loaded.bandwidth_mbps + BWTEST_LOAD_MBPS)
        return {
            "throughput_mbps": achieved,
            "latency_ms": loaded.latency_ms,
            "loss": loaded.loss,
            "rtt_s": 2.0 * loaded.latency_ms / 1000.0,
            "offered_mbps": BWTEST_LOAD_MBPS,
            "source": "bwtest",
        }

    def history(
        self,
        *,
        filt: Mapping[str, Any] | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = None,
    ) -> list[RawEvent]:
        return self.events.select(filt=filt, since=since, until=until, limit=limit)

    def open_subscription(self, stream: str, filt: Mapping[str, Any] | None) -> Subscription:
        if stream not in STREAMS:
            raise ToolError("unknown_stream", f"{stream!r} is not one of {list(STREAMS)}")
        handle = f"{stream}-{len(self.subscriptions)}"
        sub = Subscription(
            handle=handle,
            stream=stream,
            filt=dict(filt) if filt else None,
            created_s=self.now,
            cursor=len(self.events),
        )
        self.subscriptions[handle] = sub
        return sub

    def record_advisory(
        self,
        src: str,
        dst: str,
        weights: Mapping[str, float],
        meta: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        unknown = sorted(set(weights) - set(self._infos))
        if unknown:
            raise ToolError(
                "unknown_path",
                f"advisory names {len(unknown)} path(s) never returned by query_paths",
                unknown=unknown[:5],
            )
        total = sum(max(0.0, v) for v in weights.values())
        normalised = (
            {k: max(0.0, v) / total for k, v in weights.items()}
            if total > 0
            else dict.fromkeys(weights, 1.0 / max(1, len(weights)))
        )
        record = {
            "src": src,
            "dst": dst,
            "t": round(self.now, 6),
            "weights": normalised,
            "meta": dict(meta) if meta else {},
        }
        self.advisories.append(record)
        # Traffic follows the advisory, which is what makes telemetry appear on
        # the paths the model recommended and nowhere else.
        for pid, w in normalised.items():
            self._traffic[pid] = w
        self._emit(RawEvent(self.now, "tools", "advisory", dict(record)))
        return {"accepted": True, "t": round(self.now, 6), "n_paths": len(normalised)}

    # ---------------------------------------------------------------- streams

    def _emit(self, event: RawEvent) -> None:
        """Append to the log and deliver to whoever subscribed.

        Bytes are charged on delivery. A subscription whose bytes cannot be paid
        for starves and says so; it does not thin out quietly.
        """
        self.events.append(event)
        for sub in self.subscriptions.values():
            if not sub.accept(event):
                continue
            cost = Cost(nbytes=event.nbytes)
            if self.budget.shortfall(cost) is not None:
                self.budget.refuse("nbytes")
                sub.dropped += 1
                continue
            self.budget.charge(cost, count_call=False)
            sub.delivered += 1
            sub.nbytes += event.nbytes
            sub.queue.append(event)

    def drain(self, handle: str) -> list[RawEvent]:
        """Take everything a subscription has received since the last drain.

        Free: the bandwidth was charged when the records arrived. A model that
        never drains still paid for them, which is the correct incentive.
        """
        sub = self.subscriptions.get(handle)
        if sub is None:
            return []
        out = list(sub.queue)
        sub.queue.clear()
        return out

    def pending(self, handle: str) -> int:
        sub = self.subscriptions.get(handle)
        return len(sub.queue) if sub else 0

    def _harvest(self) -> None:
        """Turn what the substrate just did into raw records."""
        self._harvest_beacons()
        self._harvest_telemetry()
        self._harvest_path_server()

    def _subscribed(self, stream: str) -> bool:
        return any(s.stream == stream for s in self.subscriptions.values())

    def _harvest_beacons(self) -> None:
        resigned = self._world.segments.last_resigned
        if not resigned:
            return
        self._world.segments.last_resigned = []
        if not self._subscribed("beacons"):
            return
        topo = self._world.topology
        for seg_id in resigned:
            seg = self._world.segments.segment(seg_id)
            self._emit(
                RawEvent(
                    self.now,
                    "beacons",
                    "resigned",
                    {
                        "structural_id": _hex(seg.structural_id),
                        "segment_id": _hex(seg.segment_id),
                        "generation": seg.generation,
                        "origin_as": topo.as_name(seg.origin),
                        "hop_count": seg.hop_count,
                        "expires_in_s": round(seg.expiry_s - self.now, 6),
                    },
                )
            )

    def _harvest_telemetry(self) -> None:
        """One record per path that carried traffic.

        Which paths those are is decided by the advisories the model published,
        so the sample is biased towards what it recommended. That bias is in the
        problem -- you cannot measure a path nobody is using -- and correcting it
        here would be inventing data.
        """
        watched = {pid for pid, w in self._traffic.items() if w > 0.0} | self._probed
        self._probed.clear()
        if not watched or not self._subscribed("telemetry"):
            return
        for pid in sorted(watched):
            info = self._infos.get(pid)
            if info is None:
                continue
            path: Path = info.handle
            metrics = self._world.links.path_metrics(path.ifaces)
            self._emit(
                RawEvent(
                    self.now,
                    "telemetry",
                    "sample",
                    {
                        "path_id": pid,
                        "src": info.ref.src,
                        "dst": info.ref.dst,
                        "latency_ms": metrics.latency_ms,
                        "loss": metrics.loss,
                        "available_mbps": metrics.bandwidth_mbps,
                        "share": self._traffic.get(pid, 0.0),
                        "source": "idint",
                    },
                )
            )

    def _harvest_path_server(self) -> None:
        epoch = self._world.segments.epoch
        if epoch == self._epoch:
            return
        self._epoch = epoch
        if not self._subscribed("path_server"):
            return
        for (a, b), before in list(self._scopes.items()):
            src, dst = self._world.topology.as_name(a), self._world.topology.as_name(b)
            now_ids = frozenset(
                _hex(p.path_id(self._world.identity_policy)) for p in self._world.paths_for(a, b)
            )
            if now_ids == before:
                continue
            self._scopes[(a, b)] = now_ids
            self._emit(
                RawEvent(
                    self.now,
                    "path_server",
                    "changed",
                    {
                        "src": src,
                        "dst": dst,
                        "appeared": sorted(now_ids - before),
                        "disappeared": sorted(before - now_ids),
                        "n_paths": len(now_ids),
                    },
                )
            )

    # ------------------------------------------------------------ determinism

    def state_digest(self) -> str:
        """One hash over the session and the world under it.

        Two sessions that were given the same scenario, the same seed and the
        same calls in the same order agree on this string. That is the M2 replay
        criterion, and it covers the substrate too, so a divergence in the world
        shows up here rather than in the next milestone.
        """
        return (
            TraceHash(label="session")
            .update(
                {
                    "world": self._world.digest(),
                    "budget": self.budget.to_dict(),
                    "log": [r.to_dict() for r in self.log],
                    "view_t": round(self._view_t, 9),
                    "paths": sorted(self._known_paths),
                    "ifaces": sorted(self._known_ifaces),
                    "events": self.events.counts(),
                    "advisories": self.advisories,
                    "subscriptions": [s.to_dict() for s in self.subscriptions.values()],
                }
            )
            .short(32)
        )

    @classmethod
    def replay(
        cls,
        scenario: Scenario,
        log: Sequence[LogRecord],
        *,
        budget: Budget | None = None,
        **kw: Any,
    ) -> Session:
        """Rebuild a session by re-issuing its log against a fresh world.

        Nothing is restored from a snapshot: the scenario is rebuilt from its
        seed and every recorded call is made again in order. If the result does
        not hash the same, something in the session is not a function of the
        scenario and the calls, and that is the bug this exists to find.
        """
        session = cls(scenario.build(), budget=budget, **kw)
        for record in log:
            if record.kind == "call":
                session.call(record.name, **record.args)
            elif record.kind == "advance":
                session.advance(float(record.args.get("dt_s", 0.0)))
            elif record.kind == "tokens":
                session.think(int(record.args.get("tokens", 0)))
        return session

    # ---------------------------------------------------------------- reports

    def summary(self) -> dict[str, Any]:
        """For the front-ends' report cards. Not for the model."""
        by_tool: dict[str, dict[str, int]] = {}
        for record in self.log:
            if record.kind != "call":
                continue
            row = by_tool.setdefault(record.name, {"ok": 0, "failed": 0, "late": 0})
            row["ok" if record.ok else "failed"] += 1
            if record.late:
                row["late"] += 1
        errors: dict[str, int] = {}
        for record in self.log:
            if record.kind == "call" and not record.ok:
                errors[record.error] = errors.get(record.error, 0) + 1
        return {
            "t_s": round(self.now, 6),
            "calls": self.n_calls,
            "by_tool": by_tool,
            "errors": dict(sorted(errors.items())),
            "budget": self.budget.to_dict(),
            "events": self.events.counts(),
            "advisories": len(self.advisories),
            "paths_known": len(self._known_paths),
            "digest": self.state_digest(),
        }


# --------------------------------------------------------------------------
# drivers
# --------------------------------------------------------------------------


def _observations(result: ToolResult, path_id: str, t: float) -> list[Observation]:
    """Turn a probe result into contract observations. Mechanical, not clever.

    A timed-out echo produces no observation at all rather than a zero, and a
    latency probe produces ``throughput_mbps=None`` rather than a guess, because
    R3 checks that a model can tell not-measured from measured-and-zero.
    """
    if not result.ok:
        return []
    data = result.data
    if "samples_ms" in data:
        return [
            Observation(t=t, path_id=path_id, latency_ms=sample, source="scmp")
            for sample in data["samples_ms"]
            if sample is not None
        ]
    return [
        Observation(
            t=t,
            path_id=path_id,
            latency_ms=data.get("latency_ms"),
            throughput_mbps=data.get("throughput_mbps"),
            loss=data.get("loss"),
            source=str(data.get("source", "unknown")),
        )
    ]


def drive_episode(
    model: PathModel,
    session: Session,
    *,
    scopes: Sequence[tuple[str, str]],
    cycles: int = 10,
    sla: SLA | None = None,
    step_s: float = 1.0,
    probes_per_cycle: int = 2,
    limit: int = 20,
    n_hosts: int = 100,
) -> Session:
    """Drive a plain ``PathModel`` through the fixed observe/predict/advise cycle.

    The model never sees a tool. The driver makes the calls on its behalf and the
    costs are charged all the same, so a non-agentic model is comparable with an
    agentic one on the same budget rather than being quietly exempt from it.
    """
    sla = sla or SLA()
    model.reset(session.view(), seed=session.seed)
    for cycle in range(cycles):
        for src, dst in scopes:
            found = session.call("query_paths", src=src, dst=dst, limit=limit)
            paths = session.known_paths(src, dst)
            if not paths:
                continue

            observations: list[Observation] = []
            if found.ok and probes_per_cycle > 0:
                for offset in range(probes_per_cycle):
                    target = paths[(cycle * probes_per_cycle + offset) % len(paths)]
                    kind = "bandwidth" if (cycle and cycle % 5 == 0 and offset == 0) else "latency"
                    result = session.call("probe_path", path_id=target.path_id, kind=kind)
                    observations.extend(_observations(result, target.path_id, session.now))

            model.observe(observations, session.view())
            advisory: Advisory = model.advise(session.view(), paths, sla, n_hosts)
            weights = {k: float(v) for k, v in advisory.normalised().items()}
            if weights:
                session.call(
                    "publish_advisory",
                    src=src,
                    dst=dst,
                    weights=weights,
                    meta={"reason": advisory.reason[:200], "cycle": cycle},
                )
        session.advance(step_s)
    return session


def run_episode(
    model: PathModel,
    session: Session,
    *,
    scopes: Sequence[tuple[str, str]],
    cycles: int = 10,
    sla: SLA | None = None,
    step_s: float = 1.0,
    deadline_s: float = 0.5,
    **kwargs: Any,
) -> Session:
    """Run whichever kind of model this is.

    A model declaring ``uses_tools`` is handed the session and a deadline and
    left alone. Everything else is driven. Both end up charged against the same
    budget and logged in the same log, which is what makes them comparable.
    """
    if getattr(model.capabilities, "uses_tools", False) and hasattr(model, "act"):
        model.reset(session.view(), seed=session.seed)
        for _ in range(cycles):
            session.deadline_s = session.now + deadline_s
            model.act(session, deadline_s)
            session.deadline_s = None
            session.advance(step_s)
        return session
    return drive_episode(
        model, session, scopes=scopes, cycles=cycles, sla=sla, step_s=step_s, **kwargs
    )


def demand_from_advisory(advisory: Advisory, n_hosts: int = 1) -> Demand:
    """Contract-shaped demand from a published advisory. Used by M3."""
    return Demand(per_path=dict(advisory.normalised()), n_hosts=n_hosts)
