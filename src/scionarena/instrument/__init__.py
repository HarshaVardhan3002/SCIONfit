"""Instrumentation. Taps every layer; written to, never read by, the model.

Event trace, metric registry, pathology detectors (oscillation, herding,
context rot, identity amnesia, staleness misuse), report rendering.

M3 lands the first detector. Everything here is a plain function over a series
with no dependency beyond numpy, which is what lets the layer sit directly
above ``core`` and be read by ``exposure`` and every front-end alike.
"""

from scionarena.instrument.detectors import (
    FAST_BAND_MIN,
    WARMUP,
    dominant_period,
    fast_swing,
    flap_rate,
    oscillation_index,
    spectrum,
)

__all__ = [
    "FAST_BAND_MIN",
    "WARMUP",
    "dominant_period",
    "fast_swing",
    "flap_rate",
    "oscillation_index",
    "spectrum",
]
