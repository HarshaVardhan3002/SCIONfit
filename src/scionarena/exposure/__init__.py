"""The only thing a model ever touches.

Contracts (``PathModel`` and the data types), and from M2 the tool registry,
cost model, rate limits, observation streams, budgets and the per-episode
session object.

Any information a model receives passes through this layer. There is no back
door into :mod:`scionarena.core`.
"""

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
    TopologySnapshot,
)

__all__ = [
    "SLA",
    "Advisory",
    "Capabilities",
    "Demand",
    "Dist",
    "InterfaceAttrs",
    "Observation",
    "PathModel",
    "PathRef",
    "Prediction",
    "TopologySnapshot",
]
