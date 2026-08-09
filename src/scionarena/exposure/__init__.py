"""The only thing a model ever touches.

Contracts (``PathModel`` and the data types), and from M2 the tool registry,
cost model, rate limits, observation streams, budgets and the per-episode
session object.

Any information a model receives passes through this layer. There is no back
door into :mod:`scionarena.core`.
"""

from .budget import Budget, Cost, RateLimit, RateLimiter
from .contracts import (
    SLA,
    Advisory,
    Capabilities,
    Demand,
    Dist,
    InterfaceAttrs,
    Observation,
    PathModel,
    PathRef,
    Prediction,
    SessionLike,
    ToolUsingModel,
    TopologySnapshot,
)
from .session import LogRecord, Session, ToolResult, drive_episode, run_episode
from .streams import STREAMS, EventLog, RawEvent, Subscription
from .tools import TOOLS, PathInfo, ToolError, ToolSpec, tool_definitions

__all__ = [
    "SLA",
    "STREAMS",
    "TOOLS",
    "Advisory",
    "Budget",
    "Capabilities",
    "Cost",
    "Demand",
    "Dist",
    "EventLog",
    "InterfaceAttrs",
    "LogRecord",
    "Observation",
    "PathInfo",
    "PathModel",
    "PathRef",
    "Prediction",
    "RateLimit",
    "RateLimiter",
    "RawEvent",
    "Session",
    "SessionLike",
    "Subscription",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "ToolUsingModel",
    "TopologySnapshot",
    "drive_episode",
    "run_episode",
    "tool_definitions",
]
