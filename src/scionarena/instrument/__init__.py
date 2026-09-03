"""Instrumentation. Taps every layer; written to, never read by, the model.

Event trace, metric registry, pathology detectors (oscillation, herding,
context rot, identity amnesia, staleness misuse), report rendering.

M3 landed the first detector. M4 adds the sampler that decides *when* a series
is read: off the world's own clock rather than once per model turn, so that a
model whose turns get slower cannot deform the grid its own spectra are measured
on. See ADR 0011.

Everything here is a plain function or a small dataclass over a series, with no
dependency beyond numpy, which is what lets the layer sit directly above
``core`` and be read by ``exposure`` and every front-end alike.
"""

from scionarena.instrument.detectors import (
    FAST_BAND_MAX_ROUNDS,
    FAST_BAND_MIN,
    WARMUP,
    band_min,
    dominant_period,
    fast_swing,
    flap_rate,
    oscillation_index,
    spectrum,
    warmup_samples,
)
from scionarena.instrument.metrics import (
    FAMILIES,
    REGISTRY,
    Forecast,
    Metric,
    MetricInput,
    compute,
    metric,
)
from scionarena.instrument.sampler import Sampler, Series, path_name

__all__ = [
    "DEFAULT_DEPTH",
    "Frame",
    "FrameLog",
    "LiveChannel",
    "FAMILIES",
    "FAST_BAND_MAX_ROUNDS",
    "FAST_BAND_MIN",
    "REGISTRY",
    "WARMUP",
    "Forecast",
    "Metric",
    "MetricInput",
    "Sampler",
    "Series",
    "band_min",
    "compute",
    "dominant_period",
    "fast_swing",
    "flap_rate",
    "metric",
    "path_name",
    "oscillation_index",
    "spectrum",
    "warmup_samples",
]
