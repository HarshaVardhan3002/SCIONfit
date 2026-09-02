"""The tool registry: everything a model can do, what it costs, what limits it.

The registry is **data**. :class:`~scionarena.exposure.session.Session` executes
whatever is in ``TOOLS`` and never names a tool, so a sixth one is an entry here
and a test, and nothing else in the codebase moves. That property is an M2
acceptance criterion and there is a test that adds a tool to prove it.

Each schema is literal JSON Schema, which means :func:`tool_definitions` emits
Anthropic ``input_schema`` blocks or OpenAI ``function`` blocks with no
translation step -- an LLM agent can be handed the output directly.

Costs are in ADR 0008 and are differentiated across two orders of magnitude on
purpose. A flat cost per call would make budget reasoning trivial and would
teach a model that an echo and a bandwidth test are the same kind of decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from scionarena.core.scenario import ProbeLimits

from .budget import Cost, RateLimit
from .contracts import InterfaceAttrs, PathRef
from .streams import STREAMS, RawEvent, Subscription

__all__ = [
    "ToolError",
    "ToolContext",
    "PathInfo",
    "ToolSpec",
    "TOOLS",
    "tool_definitions",
    "validate_arguments",
    "PROBE_KINDS",
    "DEFAULT_PROBE_LIMITS",
]

#: Cheap to expensive. The whole point of the parameter.
PROBE_KINDS = ("latency", "loss", "bandwidth")

# --- the cost table, in one place so it can be argued with ------------------
QUERY_FIXED_S = 0.002
QUERY_PER_PATH_S = 0.0001
QUERY_BYTES_PER_PATH = 200

ECHO_PROBE_UNITS = 1.0
ECHO_BYTES = 128
LOSS_ECHOES = 20
LOSS_FLOOR_S = 0.5
BWTEST_S = 2.0
BWTEST_PROBE_UNITS = 100.0
BWTEST_BYTES = 5_000_000
#: What a bandwidth test puts on the path while it runs. It measures a link it
#: is itself loading; see ADR 0008.
BWTEST_LOAD_MBPS = 50.0

HISTORY_PER_EVENT_S = 0.0002

#: Listing the scopes is configuration, not measurement: a deployed
#: recommendation node is told which source-destination pairs it serves rather
#: than discovering them. Priced as one query anyway, because a call that costs
#: nothing is a call a model is free to make every round.
SCOPES_FIXED_S = QUERY_FIXED_S
SCOPES_BYTES_PER_SCOPE = 40
SUBSCRIBE_S = 0.005
SUBSCRIBE_BYTES = 256
PUBLISH_S = 0.001
PUBLISH_BYTES = 128

#: Charged for a call that was refused. Asking still costs something, or a
#: rate-limited model discovers it can poll the limiter for free information.
REFUSAL_COST = Cost(wall_clock_s=0.001, nbytes=64)

#: The limits a run assumed, when a caller has no scenario to hand. Every real
#: call resolves them from ``Scenario.probe_limits`` instead: there is no rate
#: limiter in the SCION router and the specification only says there *may* be
#: one, so what a deployment permits is a scenario parameter and Q6 is closed
#: that way rather than by picking a number and calling it a protocol fact.
DEFAULT_PROBE_LIMITS = ProbeLimits()


def scmp_limit(limits: ProbeLimits) -> RateLimit:
    """SCMP is answered by the border router, so that is what runs out."""
    return RateLimit(calls=limits.scmp_calls, per_s=limits.scmp_per_s)


def bwtest_limit(limits: ProbeLimits) -> RateLimit:
    return RateLimit(calls=limits.bwtest_calls, per_s=limits.bwtest_per_s)


def path_server_limit(limits: ProbeLimits) -> RateLimit:
    return RateLimit(calls=limits.path_server_calls, per_s=limits.path_server_per_s)


class ToolError(Exception):
    """A call that could not be answered. Carries a machine-readable code.

    Optionally carries the cost of having failed: a probe that times out
    consumed a probe unit and the timeout's worth of wall clock, and pretending
    otherwise would make failure the cheapest way to look at the network.
    """

    def __init__(self, code: str, message: str = "", *, cost: Cost | None = None, **extra: Any):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.cost = cost
        self.extra = extra


# --------------------------------------------------------------------------
# what a tool is given
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PathInfo:
    """One path as the exposure layer knows it: the contract's ``PathRef`` plus
    the beacon metadata a model needs in order to reason about staleness.

    Both identifiers are present regardless of the scenario's identity policy.
    ``ref.path_id`` is whichever one the policy says ``path_id`` means, and a
    model that keys memory on it under ``crypto_bound`` will lose that memory
    every refresh cycle. That is the failure probe R4 exists to catch, so the
    harness makes it possible rather than preventing it.
    """

    ref: PathRef
    structural_id: str
    segment_id: str
    expiry_s: float
    beacon_age_s: float
    interfaces: tuple[InterfaceAttrs, ...] = ()
    #: Opaque handle back to the substrate. Never handed to a model.
    handle: Any = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "path_id": self.ref.path_id,
            "src": self.ref.src,
            "dst": self.ref.dst,
            "interfaces": list(self.ref.interfaces),
            "hop_count": self.ref.hop_count,
            "mtu": self.ref.mtu,
            "structural_id": self.structural_id,
            "segment_id": self.segment_id,
            "expires_in_s": round(self.expiry_s, 6),
            "beacon_age_s": round(self.beacon_age_s, 6),
        }


class ToolContext(Protocol):
    """What a handler may touch.

    Small on purpose. A handler that needs something not here is asking for a
    new substrate primitive, and that is a decision to make deliberately rather
    than by adding an attribute. The context is satisfied by ``Session``; no
    handler ever sees the substrate.
    """

    @property
    def now(self) -> float: ...

    def lookup_paths(self, src: str, dst: str, limit: int) -> list[PathInfo]: ...

    def path_info(self, path_id: str) -> PathInfo: ...

    def border_router(self, path_id: str) -> str: ...

    def measure(self, info: PathInfo, kind: str) -> dict[str, Any]: ...

    def history(
        self,
        *,
        filt: Mapping[str, Any] | None,
        since: float | None,
        until: float | None,
        limit: int | None,
    ) -> list[RawEvent]: ...

    def open_subscription(self, stream: str, filt: Mapping[str, Any] | None) -> Subscription: ...

    def record_advisory(
        self, src: str, dst: str, weights: Mapping[str, float], meta: Mapping[str, Any] | None
    ) -> dict[str, Any]: ...

    def served_scopes(self) -> list[tuple[str, str]]: ...


Handler = Callable[[ToolContext, Mapping[str, Any]], dict[str, Any]]
CostFn = Callable[[Mapping[str, Any], Mapping[str, Any]], Cost]
LimitFn = Callable[[Mapping[str, Any], ProbeLimits], RateLimit | None]
KeyFn = Callable[[ToolContext, Mapping[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """A tool, its schema, its price and its limit."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Handler
    cost: CostFn
    limit: LimitFn = lambda args, limits: None
    key: KeyFn = lambda ctx, args: ""
    refusal_cost: Cost = REFUSAL_COST
    #: Charged before the handler runs, so a call cannot be afforded only
    #: because it failed. ``None`` means the cost is not knowable up front.
    reserve: Cost = field(default=Cost())

    def definition(self, dialect: str = "anthropic") -> dict[str, Any]:
        if dialect == "anthropic":
            return {
                "name": self.name,
                "description": self.description,
                "input_schema": dict(self.parameters),
            }
        if dialect == "openai":
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": dict(self.parameters),
                },
            }
        raise ValueError(f"unknown dialect {dialect!r}; expected 'anthropic' or 'openai'")


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": (list, tuple),
}


def validate_arguments(spec: ToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
    """Check arguments against the tool's schema and fill in defaults.

    Enough JSON Schema to hold the registry honest -- required, type, enum,
    bounds, no extra keys -- and no more. Pulling in a validator library for
    seven keywords would be a dependency the exposure layer does not need, and
    the schemas are ours: if one grows a keyword this does not know, the test
    that walks every schema fails.
    """
    props: Mapping[str, Any] = spec.parameters.get("properties", {})
    required: Sequence[str] = spec.parameters.get("required", ())

    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(
            "invalid_arguments", f"{spec.name} has no parameter(s) {unknown}", unknown=unknown
        )
    missing = [k for k in required if k not in args]
    if missing:
        raise ToolError(
            "invalid_arguments", f"{spec.name} requires {missing}", missing=list(missing)
        )

    out: dict[str, Any] = {}
    for name, schema in props.items():
        if name not in args:
            if "default" in schema:
                out[name] = schema["default"]
            continue
        value = args[name]
        expected = schema.get("type")
        if expected and expected in _JSON_TYPES:
            want = _JSON_TYPES[expected]
            ok = isinstance(value, want) and not (expected != "boolean" and isinstance(value, bool))
            if not ok:
                raise ToolError(
                    "invalid_arguments",
                    f"{spec.name}.{name} should be {expected}, got {type(value).__name__}",
                )
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError(
                "invalid_arguments",
                f"{spec.name}.{name} should be one of {schema['enum']}, got {value!r}",
            )
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError("invalid_arguments", f"{spec.name}.{name} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError("invalid_arguments", f"{spec.name}.{name} > {schema['maximum']}")
        out[name] = value
    return out


# --------------------------------------------------------------------------
# the five tools
# --------------------------------------------------------------------------


def _query_paths(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    infos = ctx.lookup_paths(str(args["src"]), str(args["dst"]), int(args["limit"]))
    return {
        "src": args["src"],
        "dst": args["dst"],
        "t": round(ctx.now, 6),
        "paths": [i.to_payload() for i in infos],
        "n_paths": len(infos),
    }


def _query_cost(args: Mapping[str, Any], payload: Mapping[str, Any]) -> Cost:
    n = int(payload.get("n_paths", 0))
    return Cost(
        wall_clock_s=QUERY_FIXED_S + QUERY_PER_PATH_S * n,
        nbytes=QUERY_BYTES_PER_PATH * n,
    )


def _probe_path(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    info = ctx.path_info(str(args["path_id"]))
    reading = ctx.measure(info, str(args["kind"]))
    return {"path_id": args["path_id"], "kind": args["kind"], "t": round(ctx.now, 6), **reading}


def _probe_cost(args: Mapping[str, Any], payload: Mapping[str, Any]) -> Cost:
    return probe_cost(str(args["kind"]), float(payload.get("rtt_s", 0.0)))


def probe_cost(kind: str, rtt_s: float) -> Cost:
    """What a probe costs, given how long it took to come back.

    Exposed because the timeout path needs it too: an echo that never answers
    waited a timeout and consumed its probe unit, and charging it less than a
    successful one would make failure the cheap way to look.
    """
    if kind == "bandwidth":
        return Cost(BWTEST_S, BWTEST_PROBE_UNITS, BWTEST_BYTES)
    if kind == "loss":
        return Cost(max(LOSS_FLOOR_S, LOSS_ECHOES * rtt_s), LOSS_ECHOES, ECHO_BYTES * LOSS_ECHOES)
    return Cost(rtt_s, ECHO_PROBE_UNITS, ECHO_BYTES)


def _probe_limit(args: Mapping[str, Any], limits: ProbeLimits) -> RateLimit | None:
    if args.get("kind") == "bandwidth":
        return bwtest_limit(limits)
    return scmp_limit(limits)


def _get_history(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    events = ctx.history(
        filt=args.get("filter"),
        since=args.get("since"),
        until=args.get("until"),
        limit=args.get("limit"),
    )
    return {
        "t": round(ctx.now, 6),
        "events": [e.to_dict() for e in events],
        "n_events": len(events),
        "nbytes": sum(e.nbytes for e in events),
    }


def _history_cost(args: Mapping[str, Any], payload: Mapping[str, Any]) -> Cost:
    n = int(payload.get("n_events", 0))
    return Cost(wall_clock_s=HISTORY_PER_EVENT_S * n, nbytes=int(payload.get("nbytes", 0)))


def _subscribe(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    sub = ctx.open_subscription(str(args["stream"]), args.get("filter"))
    return {"handle": sub.handle, "stream": sub.stream, "since_s": round(sub.created_s, 6)}


def _list_scopes(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    scopes = ctx.served_scopes()
    return {
        "t": round(ctx.now, 6),
        "scopes": [{"src": a, "dst": b} for a, b in scopes],
        "n_scopes": len(scopes),
    }


def _scopes_cost(args: Mapping[str, Any], payload: Mapping[str, Any]) -> Cost:
    n = int(payload.get("n_scopes", 0))
    return Cost(wall_clock_s=SCOPES_FIXED_S, nbytes=SCOPES_BYTES_PER_SCOPE * n)


def _publish_advisory(ctx: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    weights = args["weights"]
    if not isinstance(weights, dict) or not weights:
        raise ToolError("invalid_arguments", "weights must be a non-empty object")
    bad = [k for k, v in weights.items() if not isinstance(v, (int, float)) or v < 0]
    if bad:
        raise ToolError("invalid_arguments", f"weights must be non-negative numbers: {bad}")
    return ctx.record_advisory(str(args["src"]), str(args["dst"]), weights, args.get("meta"))


TOOLS: dict[str, ToolSpec] = {
    "list_scopes": ToolSpec(
        name="list_scopes",
        description=(
            "The source-destination pairs this node is responsible for advising. "
            "Configuration rather than measurement -- a deployed node is told what "
            "it serves -- but it costs a query, and the answer can change during a "
            "run as scopes are added or drained."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_scopes,
        cost=_scopes_cost,
    ),
    "query_paths": ToolSpec(
        name="query_paths",
        description=(
            "Ask the path server for the paths currently available from one AS to "
            "another. Returns beacon metadata including both identifiers and the "
            "remaining lifetime of each path. Costs query latency and is rate "
            "limited per destination AS."
        ),
        parameters={
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source ISD-AS identifier."},
                "dst": {"type": "string", "description": "Destination ISD-AS identifier."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of paths to return.",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
            "required": ["src", "dst"],
            "additionalProperties": False,
        },
        handler=_query_paths,
        cost=_query_cost,
        limit=lambda args, limits: path_server_limit(limits),
        key=lambda ctx, args: f"ps:{args['dst']}",
    ),
    "probe_path": ToolSpec(
        name="probe_path",
        description=(
            "Measure one path. 'latency' is a single SCMP echo; 'loss' is twenty "
            "echoes; 'bandwidth' is a two-second bandwidth test which itself puts "
            "traffic on the path. Costs rise by roughly two orders of magnitude "
            "across the three. Rate limited per border router, and probes can time "
            "out."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path_id": {"type": "string", "description": "A path id from query_paths."},
                "kind": {
                    "type": "string",
                    "description": "Which measurement to make.",
                    "enum": list(PROBE_KINDS),
                    "default": "latency",
                },
            },
            "required": ["path_id"],
            "additionalProperties": False,
        },
        handler=_probe_path,
        cost=_probe_cost,
        limit=_probe_limit,
        key=lambda ctx, args: ctx.border_router(str(args["path_id"])),
        reserve=Cost(probe_units=ECHO_PROBE_UNITS),
    ),
    "get_history": ToolSpec(
        name="get_history",
        description=(
            "Read raw past events from this session's log: everything delivered on "
            "any stream, every measurement taken, every advisory published. No "
            "aggregation is performed. Costs wall clock and bandwidth in proportion "
            "to the volume returned."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "description": (
                        "Field/value pairs a record must match. A list of values "
                        "matches any of them."
                    ),
                },
                "since": {"type": "number", "description": "Earliest timestamp, seconds."},
                "until": {"type": "number", "description": "Latest timestamp, seconds."},
                "limit": {
                    "type": "integer",
                    "description": "Most recent N records of the range.",
                    "minimum": 1,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_get_history,
        cost=_history_cost,
    ),
    "subscribe": ToolSpec(
        name="subscribe",
        description=(
            "Open a push feed. 'beacons' reports every re-signed segment, "
            "'telemetry' reports measurements of paths that carried traffic, "
            "'path_server' reports changes to path sets. Records arrive raw and "
            "bandwidth is charged as they arrive."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Which feed to open.",
                    "enum": list(STREAMS),
                },
                "filter": {
                    "type": "object",
                    "description": "Field/value pairs a record must match to be delivered.",
                },
            },
            "required": ["stream"],
            "additionalProperties": False,
        },
        handler=_subscribe,
        cost=lambda args, payload: Cost(SUBSCRIBE_S, nbytes=SUBSCRIBE_BYTES),
    ),
    "publish_advisory": ToolSpec(
        name="publish_advisory",
        description=(
            "Publish a distribution over paths for one source/destination scope. "
            "Hosts sample from it. Weights need not sum to one; they are "
            "normalised. This call is nearly free and it changes the network."
        ),
        parameters={
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source ISD-AS identifier."},
                "dst": {"type": "string", "description": "Destination ISD-AS identifier."},
                "weights": {
                    "type": "object",
                    "description": "Path id to non-negative weight.",
                },
                "meta": {
                    "type": "object",
                    "description": "Free-form annotation recorded with the advisory.",
                },
            },
            "required": ["src", "dst", "weights"],
            "additionalProperties": False,
        },
        handler=_publish_advisory,
        cost=lambda args, payload: Cost(PUBLISH_S, nbytes=PUBLISH_BYTES),
    ),
}


def tool_definitions(
    dialect: str = "anthropic", registry: Mapping[str, ToolSpec] | None = None
) -> list[dict[str, Any]]:
    """The registry as LLM function definitions, ready to send unmodified."""
    tools = TOOLS if registry is None else registry
    return [spec.definition(dialect) for spec in tools.values()]
