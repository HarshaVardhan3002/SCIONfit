"""Conformance front-end: *can this model represent what deployment requires?*

Probes R1-R10, the runner, and the report card. A probe changes one thing and
compares a model against itself; it never compares one model against another.

Rewritten and extended in M5. ``scionfit`` remains the public name of this
front-end.
"""

from .probes.base import Probe, ProbeResult, Status
from .report import ReportCard
from .runner import check, check_many, comparison_table

__all__ = [
    "Probe",
    "ProbeResult",
    "Status",
    "ReportCard",
    "check",
    "check_many",
    "comparison_table",
]
