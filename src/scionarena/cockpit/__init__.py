"""The cockpit: watch a model fly, and watch it fly through the bad day.

The one layer above the front-ends. It reads every registry -- metrics, axes,
probes, mandatory baselines -- runs the ``bench`` engine, and renders whatever it
finds. Nothing imports it back, which is what keeps "add a metric and the
interface shows it" a property of the code rather than a promise (ADR 0025).

The live channel itself lives in ``instrument`` because ``bench`` writes to it
and a front-end may not import the interface that watches it.
"""

from ..instrument.channel import Frame, FrameLog, LiveChannel
from .panels import TIER_COST_S, catalogue, estimate_s, selection

__all__ = [
    "Frame",
    "FrameLog",
    "LiveChannel",
    "TIER_COST_S",
    "catalogue",
    "estimate_s",
    "selection",
]
