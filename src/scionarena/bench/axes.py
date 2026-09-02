"""The six axes a sweep varies, and what each value means to the substrate.

Master Spec §28 and its M4½ name five -- population size, defector fraction, the
mechanism ladder, paths per selector, staleness -- and Q6 made the probe-limit
regime a sixth by moving it out of ``exposure`` and into the scenario, where two
runs made under different assumptions about the price of information stop being
the same experiment.

An axis value is a *label* and a payload. The label is what appears in a result
file, a report and a filename; the payload is the overrides it applies. Nothing
downstream reads the payload, so a value's meaning can be corrected without
invalidating the identity of the cells that used it -- which is also the trap:
correcting one silently changes what a recorded score means. The suite digest is
over the labels *and* the payloads for that reason (ADR 0016).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Axis", "AXES", "AxisValue", "baseline_cell", "settings_for"]


@dataclass(frozen=True, slots=True)
class AxisValue:
    """One point on one axis."""

    label: str
    #: Fields of ``HostParams`` this value overrides.
    hosts: Mapping[str, Any] = field(default_factory=dict)
    #: Fields of ``LoopConfig`` this value overrides.
    loop: Mapping[str, Any] = field(default_factory=dict)
    #: Fields of ``ProbeLimits`` this value overrides.
    probes: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "hosts": dict(self.hosts),
            "loop": dict(self.loop),
            "probes": dict(self.probes),
        }


@dataclass(frozen=True, slots=True)
class Axis:
    """One question the sweep asks, and the values it asks it at.

    The **first** value is the baseline. Under the default one-at-a-time mode
    every other axis is held there while this one moves, so the first value is
    load-bearing rather than merely first: if it is unrepresentative, every
    reading in the suite is unrepresentative in the same direction.
    """

    name: str
    doc: str
    values: tuple[AxisValue, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(v.label for v in self.values)

    def value(self, label: str) -> AxisValue:
        for v in self.values:
            if v.label == label:
                return v
        raise KeyError(f"axis {self.name!r} has no value {label!r}; it has {list(self.labels)}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "values": [v.to_dict() for v in self.values]}


AXES: Mapping[str, Axis] = {
    # On ``loop`` rather than on ``hosts``: the driver sizes each scope's
    # population against the spare capacity of its best path and overwrites
    # ``HostParams.n_hosts`` while doing it, so an axis that set the scenario's
    # figure would be silently ignored -- which it was, for one afternoon.
    "population": Axis(
        name="population",
        doc="how many selectors are acting on the advice",
        values=(
            AxisValue("1k", loop={"n_hosts": 1_000}),
            AxisValue("100", loop={"n_hosts": 100}, note="1/sqrt(N) noise is visible here"),
            AxisValue("10k", loop={"n_hosts": 10_000}),
        ),
    ),
    "defectors": Axis(
        name="defectors",
        doc="fraction of the population that ignores the advisory and goes greedy",
        values=(
            AxisValue("none", hosts={"defector_fraction": 0.0}),
            AxisValue("10pct", hosts={"defector_fraction": 0.1}),
            AxisValue("30pct", hosts={"defector_fraction": 0.3}),
        ),
    ),
    # The mechanism ladder. Rungs 1 and 2 are model-side -- whether a model
    # publishes point rankings or intervals -- and live in Capabilities. These
    # are rungs 3 and 4, which are population-side (ADR 0015).
    "discipline": Axis(
        name="discipline",
        doc="rungs 3 and 4 of the mechanism ladder: what a selector does between "
        "being told and moving",
        values=(
            AxisValue("none", note="maximally responsive, which flatters an unstable model"),
            AxisValue("epsilon", hosts={"eps_set": 0.5}),
            AxisValue("hysteresis", hosts={"hysteresis": 0.1}),
            AxisValue("dwell", hosts={"dwell_s": 60.0}),
            AxisValue("synchronised", hosts={"timer_jitter": 0.0, "resample_s": 30.0}),
            AxisValue("mirror", hosts={"mirror_jitter_s": 30.0}, note="rung 4"),
        ),
    ),
    "paths": Axis(
        name="paths",
        doc="how many paths one selector will place traffic on",
        values=(
            AxisValue("all"),
            AxisValue("3", hosts={"k_paths": 3}),
            AxisValue("1", hosts={"k_paths": 1}, note="the discrete regime: no within-host mixing"),
        ),
    ),
    "staleness": Axis(
        name="staleness",
        doc="how old the telemetry is when the model reads it -- information "
        "delay, not decision delay",
        values=(
            AxisValue("fresh", loop={"telemetry_delay_s": 0.0}),
            AxisValue("30s", loop={"telemetry_delay_s": 30.0}),
            AxisValue("120s", loop={"telemetry_delay_s": 120.0}),
        ),
    ),
    # Q6: the SCMP specification says only that SCMP *may* be rate limited, and
    # the audited router implements no limiter. The regime is therefore a
    # deployment choice that may be zero, and every budget result is conditional
    # on which one was in force.
    "probes": Axis(
        name="probes",
        doc="how much probing the deployment permits (Q6: there is no protocol answer)",
        values=(
            AxisValue("audited", note="the defaults: what the audited router actually does"),
            AxisValue(
                "unlimited",
                probes={"scmp_calls": 1e9, "bwtest_calls": 1e9, "path_server_calls": 1e9},
                note="no limiter anywhere, which is what the audited router has",
            ),
            AxisValue(
                "strict",
                probes={"scmp_calls": 1.0, "bwtest_calls": 1.0, "bwtest_per_s": 120.0},
                note="an operator who rate-limits hard",
            ),
        ),
    ),
}


def baseline_cell() -> dict[str, str]:
    """Every axis at its first value. The point every reading is measured from."""
    return {name: axis.values[0].label for name, axis in AXES.items()}


def settings_for(cell: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(host overrides, loop overrides, probe overrides)`` for one cell.

    Axes are applied in ``AXES`` order and a later axis wins a collision. There
    is one deliberate collision -- ``discipline=synchronised`` sets
    ``resample_s`` -- and no axis after ``discipline`` touches it.
    """
    hosts: dict[str, Any] = {}
    loop: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    for name, axis in AXES.items():
        value = axis.value(cell[name]) if name in cell else axis.values[0]
        hosts.update(value.hosts)
        loop.update(value.loop)
        probes.update(value.probes)
    return hosts, loop, probes
