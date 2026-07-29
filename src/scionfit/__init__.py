"""scionfit -- conformance and fit checking for SCION path-selection models.

    from scionfit import check
    from scionfit.reference import ReferenceStochastic
    print(check(ReferenceStochastic()).to_terminal())
"""
from .interface import (
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
from .probes.base import Probe, ProbeResult, Status
from .report import ReportCard
from .runner import check, check_many, comparison_table

__version__ = "0.1.0"
__all__ = [
    "check", "check_many", "comparison_table", "ReportCard",
    "Probe", "ProbeResult", "Status",
    "PathModel", "Capabilities", "TopologySnapshot", "PathRef",
    "InterfaceAttrs", "Observation", "Demand", "Dist", "Prediction",
    "Advisory", "SLA", "__version__",
]
