"""What the interface knows, read out of the registries rather than listed here.

The hard requirement of Phase 7 and the one to design against rather than
retrofit: **add a metric in code and the interface shows it with no interface
change; delete it and it disappears.** Four registries already existed and this
module is what makes them load-bearing:

===============  ==========================================  ==========
what             where                                       added by
===============  ==========================================  ==========
metrics          ``REGISTRY`` and the ``@metric`` decorator   ADR 0017
axes             ``AXES``                                     ADR 0016
probes           ``ALL_PROBES``                               M5
baselines        ``MANDATORY_BASELINES``                      Phase 2
===============  ==========================================  ==========

Three consequences, all accepted rather than worked around. A metric must arrive
fully described -- name, family, direction, shape, a one-line doc -- because
there is nothing else to render it from. There is no per-metric layout code
anywhere: a metric declares its *shape* and there is one renderer per shape.
And selecting a subset is a filter over the registry, never a hardcoded list,
which is why :func:`selection` exists and why what it *left out* travels with it.

Every gloss comes from the registry entry itself. The interface does not keep its
own copy, because a copy drifts and a drifted gloss is worse than none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..bench.axes import AXES
from ..conformance.probes import ALL_PROBES
from ..instrument.metrics import FAMILIES, REGISTRY
from ..reference.baselines import MANDATORY_BASELINES

__all__ = ["catalogue", "selection", "estimate_s", "TIER_COST_S"]

#: Measured seconds per cell per tier **per decision round**, on one core of the
#: machine this was recorded on. A constant rather than a model: the point is to
#: tell somebody clicking "realistic" what they are about to start *before* they
#: start it, and a wrong order of magnitude is the only error that matters here.
#: It is labelled as this machine's wherever it is shown.
#:
#: Per round rather than per cell, which the first version got wrong. A cell is
#: not a fixed amount of work -- it is ``cycles`` decision rounds -- so a flat
#: per-cell figure predicted 32 s for a 108-cell sweep at 90 rounds that took
#: about 250 s, and would have been wrong in the *reassuring* direction for
#: anyone raising the round count.
TIER_COST_S: Mapping[str, float] = {
    "smoke": 0.10,
    "dev": 0.29,
    "realistic": 4.7,
    "stress": 60.0,
}

#: What ``cycles`` the figures above are per-round *of*. A round is not free of
#: the tier: at the realistic tier a round touches far more of the network.
NOMINAL_CYCLES = 1


def catalogue() -> dict[str, Any]:
    """Everything the interface can render, straight from the registries.

    Nothing here is a literal list of names. If this function ever grows one,
    the property the phase exists for has been lost.
    """
    metrics = [
        {
            "name": name,
            "family": entry.family,
            "doc": entry.doc,
            "shape": entry.shape,
            "support": entry.support,
            # ``None`` is a third direction, not a missing one: a coverage gap of
            # +0.4 is as wrong as one of -0.4, and a column sorted "lower is
            # better" would rank the most over-confident model top.
            "direction": (
                "closer to zero"
                if entry.higher_is_better is None
                else ("higher" if entry.higher_is_better else "lower")
            ),
        }
        for name, entry in sorted(REGISTRY.items())
    ]
    axes = [
        {
            "name": axis.name,
            "doc": axis.doc,
            "baseline": axis.values[0].label,
            "values": [
                {"label": v.label, "note": v.note, "stages": len(v.disturbances)}
                for v in axis.values
            ],
        }
        for axis in AXES.values()
    ]
    probes = [
        {
            "id": p.probe_id,
            "requirement": p.requirement,
            "title": p.title,
            "capability": p.capability or "",
        }
        for p in ALL_PROBES
    ]
    return {
        "families": list(FAMILIES),
        "metrics": metrics,
        "axes": axes,
        "probes": probes,
        "baselines": list(MANDATORY_BASELINES),
        "tier_cost_s": dict(TIER_COST_S),
    }


def selection(
    *,
    families: Sequence[str] = (),
    metrics: Sequence[str] = (),
    axes: Sequence[str] = (),
) -> dict[str, Any]:
    """What a narrowed run will cover, **and what it will not**.

    The second half is the load-bearing one. A researcher improving calibration
    should be able to run the accuracy family alone and skip an hour of stability
    sweeps -- and a narrowed suite that renders like a full one is exactly the
    dishonesty Phase 4 exists to prevent, moved into the interface where it is
    easier to commit and harder to see. So the omissions are computed here, in
    the same call, and travel with the result.
    """
    chosen_families = list(families) or list(FAMILIES)
    unknown = [f for f in chosen_families if f not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown famil(ies) {unknown}; the families are {list(FAMILIES)}")
    unknown_axes = [a for a in axes if a not in AXES]
    if unknown_axes:
        raise ValueError(f"unknown ax(es) {unknown_axes}; the axes are {sorted(AXES)}")

    included = {
        name
        for name, entry in REGISTRY.items()
        if entry.family in chosen_families and (not metrics or name in metrics)
    }
    chosen_axes = list(axes) or list(AXES)
    return {
        "families": chosen_families,
        "metrics": sorted(included),
        "axes": chosen_axes,
        "omitted": {
            "families": [f for f in FAMILIES if f not in chosen_families],
            "metrics": sorted(set(REGISTRY) - included),
            "axes": [a for a in AXES if a not in chosen_axes],
        },
    }


def estimate_s(cells: int, tier: str, workers: int = 1, cycles: int = 60) -> float:
    """Roughly how long ``cells`` will take. This machine's numbers, and said so.

    Deliberately crude and deliberately *before* the run. Somebody who clicks
    "realistic" without being told what that costs deserves to know beforehand,
    and an order of magnitude is enough to make that decision -- which is why
    ``cycles`` is an argument rather than an assumption. A cell is not a fixed
    amount of work and the first version of this treated it as one.
    """
    per_round = TIER_COST_S.get(tier, TIER_COST_S["dev"])
    return max(0.0, cells) * max(1, cycles) * per_round / max(1, workers)
