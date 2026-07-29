"""scionarena -- a SCION network exposed to an AI, and everything that happens.

One substrate, four front-ends on it: ``conformance`` (M5), ``bench`` (M6),
``gym`` (M7), ``deploy`` (M9).  The conformance front-end is what v0.1 shipped
as ``scionfit`` and it is re-exported here unchanged::

    from scionarena import check
    from scionarena.reference import ReferenceStochastic
    print(check(ReferenceStochastic()).to_terminal())
"""
from .conformance.probes.base import Probe, ProbeResult, Status
from .conformance.report import ReportCard
from .conformance.runner import check, check_many, comparison_table
from .exposure.contracts import (
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

__version__ = "0.1.0"
__all__ = [
    "check", "check_many", "comparison_table", "ReportCard",
    "Probe", "ProbeResult", "Status",
    "PathModel", "Capabilities", "TopologySnapshot", "PathRef",
    "InterfaceAttrs", "Observation", "Demand", "Dist", "Prediction",
    "Advisory", "SLA", "__version__",
]
