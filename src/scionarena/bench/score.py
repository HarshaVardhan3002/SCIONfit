"""What can only be said about a *sweep*, not about a run.

One thing, so far, and it is the number every stability claim is conditional on:
**the compliant-share threshold** -- the fraction of the population that has to
follow advice before a mechanism stops working.

It is deliberately not in the metric registry. A metric there is a function of
one run, and a threshold computed from one run is a threshold with one point in
it: it would render in a report as though it had been measured. This is a
function over results, and it needs the defector axis to have been swept.

It must be read **per regime**. A threshold measured with ten thousand mixing
hosts says nothing about a hundred single-path gateways, because the mechanism
those two populations are running is not the same mechanism -- which is what the
``paths`` and ``population`` axes exist to vary and why ``regimes`` groups by
them rather than pooling.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from .axes import AXES
from .results import CellResult

__all__ = ["Threshold", "compliant_share_threshold", "regimes"]

#: The axis a threshold is read along, and the axis label -> compliant share.
#: Compliant share is one minus the defector fraction, because the question §28
#: asks is how many have to *follow* rather than how many may defect.
_DEFECTOR_SHARE: Mapping[str, float] = {"none": 1.0, "10pct": 0.9, "30pct": 0.7}


@dataclass(frozen=True, slots=True)
class Threshold:
    """Where a model stopped working, along the defector axis."""

    model: str
    regime: str
    metric: str
    limit: float
    #: Highest compliant share at which the metric was still over the limit, or
    #: ``None`` if it never was. ``None`` is the good answer and it is not 1.0.
    share: float | None
    #: What was measured at each share, so a reader can see how thin it is.
    measured: Mapping[float, float]

    @property
    def held(self) -> bool:
        return self.share is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "regime": self.regime,
            "metric": self.metric,
            "limit": self.limit,
            "share": self.share,
            "held": self.held,
            "measured": {str(k): v for k, v in sorted(self.measured.items())},
        }


def regimes(results: Sequence[CellResult]) -> dict[str, list[CellResult]]:
    """Cells grouped by the regime they were run in.

    Population and paths-per-selector, because those two decide *what mechanism*
    the population is running. A threshold pooled across them is an average of
    two different experiments.
    """
    out: dict[str, list[CellResult]] = {}
    for r in results:
        key = f"population={r.axes.get('population', '?')}, paths={r.axes.get('paths', '?')}"
        out.setdefault(key, []).append(r)
    return out


def compliant_share_threshold(
    results: Sequence[CellResult], *, metric: str = "swing", limit: float = 1.0
) -> list[Threshold]:
    """The compliant share at which each model first breaches ``limit``.

    Reported per model per regime. A model whose metric never breaches gets
    ``share=None`` and ``held=True``; a model measured at only one share gets a
    row anyway, with ``measured`` showing that it is one point, because silently
    dropping it would make an unswept axis look like a passing result.
    """
    known = set(AXES["defectors"].labels)
    out: list[Threshold] = []
    for regime, cells in sorted(regimes(results).items()):
        by_model: dict[str, dict[float, list[float]]] = {}
        for cell in cells:
            label = cell.axes.get("defectors", "")
            value = cell.metrics.get(metric)
            if not cell.ok or label not in known or not isinstance(value, (int, float)):
                continue
            share = _DEFECTOR_SHARE.get(label)
            if share is None:
                continue
            by_model.setdefault(cell.label, {}).setdefault(share, []).append(float(value))

        for model, samples in sorted(by_model.items()):
            measured = {share: median(values) for share, values in samples.items()}
            # Walk from fully compliant downwards; the threshold is the first
            # share at which it breaks, so the answer is the *highest* share
            # that already fails rather than the lowest that still passes.
            breached = [
                share for share, value in sorted(measured.items(), reverse=True) if value > limit
            ]
            out.append(
                Threshold(
                    model=model,
                    regime=regime,
                    metric=metric,
                    limit=limit,
                    share=max(breached) if breached else None,
                    measured=measured,
                )
            )
    return out
