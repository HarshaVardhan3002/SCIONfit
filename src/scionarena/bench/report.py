"""The PDF. What was tested, what the model declared, how it did, and how thin.

This is the artefact the product statement is about, and the first output read by
somebody who was not there when it was produced. It is a **pure function of a
results directory** (ADR 0018): nothing here builds a substrate, loads a model or
recomputes a metric, so a report can be regenerated months later from a directory
copied off a cluster, on a machine where the model's own dependencies are absent.

The price of that is that anything the report says had to be recorded when the
cell ran. Where it was not, the report prints ``unrecorded`` -- never a guess and
never a default that looks like a measurement.

Conformance verdicts arrive as a JSON file rather than as an import, because
``bench`` and ``conformance`` are siblings and neither may import the other::

    scionfit check --model pkg:Model --format json --out card.json
    scionarena bench report results/ --conformance card.json --out report.pdf
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from ..instrument.figures import MissingReportDeps, axis_response, horizon_bars, model_ranking
from ..instrument.figures import require as require_deps
from ..instrument.metrics import FAMILIES, NOMINAL, REGISTRY
from .axes import AXES
from .results import CellResult, load_results

__all__ = [
    "HEADLINE",
    "MissingReportDeps",
    "Reading",
    "ReportData",
    "build_pdf",
    "gather",
    "summary_lines",
]

#: Repeats below which a spread is not estimable. Not a round number chosen for
#: the look of it: it is ``SweepSpec.repeats``, which is three because the
#: headline metric was measured *bimodal* across seeds, so two points cannot
#: distinguish a spread from a mode.
THIN_REPEATS = 3

#: Observations below which a value inside one run is a sample rather than a
#: statistic. Twenty, because the strongest claim in the registry is a
#: percentile and the 95th percentile of nineteen observations is the largest
#: of them -- printed to four decimals it would say otherwise.
THIN_SUPPORT = 20

#: The support metric each family's numbers are divided by. One per family
#: rather than one overall: a coverage figure from three repeats of a run that
#: scored four forecasts each is not a figure from twelve observations.
SUPPORT_OF: Mapping[str, str] = {
    "accuracy": "n_scored",
    "decision": "n_cost_samples",
    "stability": "n_samples",
    "operational": "n_decisions",
}

#: What a family is read on at a glance. Accuracy has none because it is
#: stratified by horizon by construction and a single headline would be the
#: pooling the registry exists to prevent.
HEADLINE: Mapping[str, str] = {
    "decision": "regret_ratio",
    "stability": "swing",
    "operational": "decision_p95_s",
}

#: Which metrics get a figure per axis. Two rather than all four: the sweep
#: engine measured these as the pair that discriminates, and a response figure
#: for every metric on every axis is a hundred pages nobody reads. ``--axis-metric``
#: adds more.
AXIS_METRICS: tuple[str, ...] = ("swing", "regret_ratio")

#: Variants an architecture needs before its row stops being a claim about the
#: best variant anybody happened to submit. Three, for the same reason repeats
#: are three: below it the spread is not estimable, and an architecture is a
#: population of models exactly as a cell is a population of seeds.
THIN_VARIANTS = 3

THIN_MARK = "†"


def _sig(value: float, digits: int) -> str:
    """``value`` to ``digits`` significant figures, without exponent noise."""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        # Kept as a word rather than folded into a large number: "settled at
        # sample 9,999" and "never settled" are different findings.
        return "-inf" if value < 0 else "inf"
    if value == 0.0:
        return "0"
    return f"{value:.{digits}g}"


@dataclass(frozen=True, slots=True)
class Reading:
    """One metric, for one model, at one point of the matrix.

    Carries what it is *and* how much is behind it, because the two are printed
    together or the digits lie. ``value`` is the median over repeats; ``lo`` and
    ``hi`` are the observed range, not a confidence interval -- with three
    repeats of a bimodal quantity an interval would be a fiction and a range is
    a fact.
    """

    metric: str
    value: float | None = None
    #: Repeats that produced a value. Zero means the metric was not measured,
    #: which is a different state from measuring zero.
    n: int = 0
    lo: float | None = None
    hi: float | None = None
    #: Median observations *within* a run, from the family's support metric.
    #: ``None`` when the run predates the support metrics, and that renders as
    #: thin rather than as fine, because unknown support is not good support.
    support: float | None = None

    @property
    def measured(self) -> bool:
        """Whether there is anything here at all.

        Known-zero support counts as not measured. A stability number computed
        over zero samples is a number the detector returned, not a measurement,
        and printing ``0`` for it is how a run whose warmup ate the whole
        episode reads as a perfectly stable model.
        """
        if self.value is None or self.n <= 0:
            return False
        return not (self.support is not None and self.support <= 0)

    @property
    def thin(self) -> bool:
        if not self.measured:
            return False
        if self.n < THIN_REPEATS:
            return True
        return self.support is None or self.support < THIN_SUPPORT

    def render(self) -> str:
        if not self.measured:
            if self.value is not None and self.support is not None and self.support <= 0:
                return "no observations"
            return "not measured"
        assert self.value is not None
        if self.thin:
            where = f"n={self.n}"
            if self.support is not None:
                where += f", {int(self.support)} obs"
            return f"{_sig(self.value, 2)} ({where}){THIN_MARK}"
        span = ""
        if self.lo is not None and self.hi is not None and self.hi > self.lo:
            span = f"  [{_sig(self.lo, 3)}..{_sig(self.hi, 3)}]"
        return f"{_sig(self.value, 4)}{span}"


@dataclass
class ReportData:
    """Everything the PDF prints, read off the cells and nothing else."""

    suite: str = ""
    suite_digest: str = ""
    tier: str = ""
    created: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    directory: str = ""
    n_cells: int = 0
    #: ``(label, mandatory)``, user models first.
    models: list[tuple[str, bool]] = field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: display name -> architecture tag, empty where the model did not declare
    #: one. Printed as "unrecorded"; never guessed from the class name.
    architecture: dict[str, str] = field(default_factory=dict)
    #: display name -> the drive that produced it (ADR 0019).
    drive_of: dict[str, str] = field(default_factory=dict)
    #: Compute-charging policies seen across the cells (ADR 0021). More than one
    #: means the directory holds two different experiments.
    think_seen: list[str] = field(default_factory=list)
    #: display name -> the model's own label, which the two halves of a parity
    #: pair share. One model run twice is one variant, and counting the rows
    #: instead would let a tag reach the variant threshold on a duplicate.
    bare_of: dict[str, str] = field(default_factory=dict)
    #: Models present under more than one drive, by their bare label. These are
    #: the parity pairs, and the only rows whose difference isolates what
    #: choosing your own probes is worth.
    paired: list[str] = field(default_factory=list)
    #: axis name -> labels that actually appear in the results.
    axes_seen: dict[str, list[str]] = field(default_factory=dict)
    baseline: dict[str, str] = field(default_factory=dict)
    #: family -> flattened metric names present, in registry order.
    metrics_by_family: dict[str, list[str]] = field(default_factory=dict)
    #: (model, metric) -> Reading, at the baseline cell.
    at_baseline: dict[tuple[str, str], Reading] = field(default_factory=dict)
    #: axis -> metric -> model -> readings, one per axis label.
    by_axis: dict[str, dict[str, dict[str, list[Reading]]]] = field(default_factory=dict)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    #: Model labels whose cells were *not applicable* rather than broken -- a
    #: forecaster asked for its agentic half. Held apart from ``failures``
    #: because a page that renders the two alike tells a reader that five
    #: mandatory baselines are faulty, which would discredit the floor every
    #: other number is measured against.
    refused: dict[str, int] = field(default_factory=dict)
    substrate_digests: list[str] = field(default_factory=list)
    seeds: dict[str, int] = field(default_factory=dict)
    #: True when no cell moved more than one axis off the baseline, so no
    #: interaction between axes was measured. Derived, not recorded: the mode
    #: is a property of the suite and this is a property of the results.
    one_at_a_time: bool = True
    stale: list[str] = field(default_factory=list)

    @property
    def horizons(self) -> list[str]:
        """Horizon suffixes present, shortest first."""
        found: set[str] = set()
        for _, metric in self.at_baseline:
            base, _, suffix = metric.partition(".")
            if suffix and REGISTRY.get(base) is not None and REGISTRY[base].family == "accuracy":
                found.add(suffix)
        return sorted(found, key=lambda h: int(h.lstrip("h") or 0))


def _where(axes: Mapping[str, str]) -> str:
    moved = {k: v for k, v in axes.items() if k in AXES and v != AXES[k].values[0].label}
    return ", ".join(f"{k}={v}" for k, v in sorted(moved.items())) or "baseline"


def _support_key(metric: str) -> str | None:
    """The support metric that qualifies ``metric``, stratum for stratum."""
    base, _, suffix = metric.partition(".")
    entry = REGISTRY.get(base)
    if entry is None or entry.support:
        return None
    support = SUPPORT_OF.get(entry.family)
    if support is None:
        return None
    return f"{support}.{suffix}" if suffix else support


def _reading(metric: str, cells: Sequence[CellResult]) -> Reading:
    values: list[float] = []
    supports: list[float] = []
    support_key = _support_key(metric)
    for cell in cells:
        if not cell.ok:
            continue
        raw = cell.metrics.get(metric)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        values.append(float(raw))
        if support_key is not None:
            got = cell.metrics.get(support_key)
            if isinstance(got, (int, float)) and not isinstance(got, bool):
                supports.append(float(got))
    if not values:
        return Reading(metric=metric)
    return Reading(
        metric=metric,
        value=median(values),
        n=len(values),
        lo=min(values),
        hi=max(values),
        support=median(supports) if supports else None,
    )


def gather(results: Sequence[CellResult]) -> ReportData:
    """Fold a results directory into what the PDF prints.

    Cells recorded under a different suite digest are kept and *listed* rather
    than dropped. Dropping them would silently report a subset; listing them
    lets a reader see that the directory holds two suites, which is the actual
    problem and is usually a merge that should not have happened.
    """
    data = ReportData(n_cells=len(results))
    if not results:
        return data

    digests = [r.suite_digest for r in results]
    data.suite_digest = max(set(digests), key=digests.count)
    data.stale = sorted({d for d in digests if d != data.suite_digest})
    cells = [r for r in results if r.suite_digest == data.suite_digest]
    data.suite = cells[0].suite
    tiers = {r.tier for r in cells if r.tier}
    data.tier = "/".join(sorted(tiers)) if tiers else "unrecorded"
    data.substrate_digests = sorted({r.substrate_digest for r in cells if r.substrate_digest})

    seen: dict[str, list[str]] = {}
    for cell in cells:
        for axis, label in cell.axes.items():
            if axis in AXES and label not in seen.setdefault(axis, []):
                seen[axis].append(label)
        if sum(1 for a, v in cell.axes.items() if a in AXES and v != AXES[a].values[0].label) > 1:
            data.one_at_a_time = False
    data.axes_seen = {a: [v for v in AXES[a].labels if v in labels] for a, labels in seen.items()}
    data.baseline = {a: AXES[a].values[0].label for a in data.axes_seen}

    # One model run two ways is two things to compare, not one thing measured
    # twice, so the drive joins the name -- but only where both halves are
    # present, or every ordinary suite would grow a suffix that says nothing.
    # Refused cells carry no drive and must not count as a second one.
    drives: dict[str, set[str]] = {}
    for cell in cells:
        if cell.drive:
            drives.setdefault(cell.label, set()).add(cell.drive)
    data.paired = sorted(label for label, seen in drives.items() if len(seen) > 1)
    paired = set(data.paired)

    def name_of(cell: CellResult) -> str:
        if cell.label in paired and cell.drive:
            return f"{cell.label} [{cell.drive}]"
        return cell.label

    order: dict[str, bool] = {}
    for cell in cells:
        name = name_of(cell)
        order.setdefault(name, cell.mandatory)
        if cell.capabilities:
            data.capabilities.setdefault(name, dict(cell.capabilities))
            data.architecture.setdefault(name, str(cell.capabilities.get("architecture", "")))
        if cell.drive:
            data.drive_of.setdefault(name, cell.drive)
        if cell.think and cell.think not in data.think_seen:
            data.think_seen.append(cell.think)
        data.bare_of.setdefault(name, cell.label)
        data.seeds.setdefault(f"{name} @ {_where(cell.axes)} #{cell.repeat}", cell.seed)
        if cell.refused:
            data.refused[name] = data.refused.get(name, 0) + 1
        elif not cell.ok:
            data.failures.append((name, _where(cell.axes), cell.error or "?"))
    data.models = sorted(order.items(), key=lambda kv: (kv[1], kv[0]))

    present: set[str] = set()
    for cell in cells:
        if cell.ok:
            present.update(k for k in cell.metrics if k.partition(".")[0] in REGISTRY)
    for family in FAMILIES:
        names = sorted(
            n
            for n in present
            if REGISTRY[n.partition(".")[0]].family == family
            and not REGISTRY[n.partition(".")[0]].support
        )
        if names:
            data.metrics_by_family[family] = names

    base_cells: dict[str, list[CellResult]] = {}
    for cell in cells:
        if _where(cell.axes) == "baseline":
            base_cells.setdefault(name_of(cell), []).append(cell)
    for label, _ in data.models:
        for metric in present:
            data.at_baseline[(label, metric)] = _reading(metric, base_cells.get(label, []))

    for axis, labels in data.axes_seen.items():
        per_metric: dict[str, dict[str, list[Reading]]] = {}
        for metric in sorted(present):
            per_model: dict[str, list[Reading]] = {}
            for label, _ in data.models:
                row: list[Reading] = []
                for value in labels:
                    subset = [
                        c
                        for c in cells
                        if name_of(c) == label
                        and c.axes.get(axis) == value
                        and all(
                            c.axes.get(a) == AXES[a].values[0].label
                            for a in data.axes_seen
                            if a != axis
                        )
                    ]
                    row.append(_reading(metric, subset))
                per_model[label] = row
            per_metric[metric] = per_model
        data.by_axis[axis] = per_metric
    return data


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _styles() -> Any:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    sheet = getSampleStyleSheet()
    sheet.add(
        ParagraphStyle(
            "Body",
            parent=sheet["BodyText"],
            fontSize=8.6,
            leading=11.6,
            alignment=TA_LEFT,
            spaceAfter=6,
        )
    )
    sheet.add(ParagraphStyle("Small", parent=sheet["BodyText"], fontSize=7.4, leading=9.6))
    sheet.add(
        ParagraphStyle("H1", parent=sheet["Heading1"], fontSize=16, leading=19, spaceBefore=2)
    )
    sheet.add(
        ParagraphStyle("H2", parent=sheet["Heading2"], fontSize=11.5, leading=14, spaceBefore=14)
    )
    sheet.add(
        ParagraphStyle("H3", parent=sheet["Heading3"], fontSize=9.5, leading=12, spaceBefore=10)
    )
    return sheet


def _table(rows: Sequence[Sequence[str]], widths: Sequence[float] | None = None) -> Any:
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(list(rows), colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.2),
                ("LEADING", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#5b6b7c")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#cfd8e3")),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e3e8ee")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _image(png: bytes, width: float) -> Any:
    """A PNG scaled to the text column, keeping its aspect ratio.

    Measured from the image rather than assumed from ``figures.FIGSIZE``: the
    charts are saved with ``bbox_inches="tight"``, so the height depends on how
    long the legend turned out to be and a fixed ratio would squash it.
    """
    import io

    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image

    native_w, native_h = ImageReader(io.BytesIO(png)).getSize()
    scale = width / float(native_w)
    return Image(io.BytesIO(png), width=width, height=native_h * scale)


def _paragraph(text: str, style: Any) -> Any:
    from reportlab.platypus import Paragraph

    return Paragraph(text, style)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _series(
    data: ReportData, axis: str, metric: str
) -> tuple[list[str], dict[str, list[float | None]]]:
    labels = data.axes_seen.get(axis, [])
    per_model = data.by_axis.get(axis, {}).get(metric, {})
    return labels, {m: [r.value for r in readings] for m, readings in per_model.items()}


def _ranking_rows(data: ReportData, metric: str) -> list[dict[str, Any]]:
    entry = REGISTRY.get(metric.partition(".")[0])
    higher = bool(entry.higher_is_better) if entry is not None else False
    rows = []
    for label, mandatory in data.models:
        reading = data.at_baseline.get((label, metric), Reading(metric=metric))
        rows.append(
            {
                "model": label,
                "value": reading.value,
                "lo": reading.lo,
                "hi": reading.hi,
                "mandatory": mandatory,
            }
        )

    def rank(row: Mapping[str, Any]) -> tuple[bool, float]:
        value = row["value"]
        if not isinstance(value, (int, float)):
            # Unmeasured sorts last whichever way the metric runs, so an absent
            # bar never lands at the top of a ranking.
            return True, 0.0
        return False, -float(value) if higher else float(value)

    rows.sort(key=rank)
    return rows


def _cover(story: list[Any], sheet: Any, data: ReportData) -> None:
    story.append(_paragraph("scionarena benchmark report", sheet["H1"]))
    story.append(
        _paragraph(
            f"suite <b>{_escape(data.suite or 'unnamed')}</b> "
            f"({_escape(data.suite_digest or 'no digest')}) &middot; "
            f"tier <b>{_escape(data.tier)}</b> &middot; {data.n_cells} cells &middot; "
            f"rendered {_escape(data.created)}",
            sheet["Body"],
        )
    )
    story.append(
        _paragraph(
            "This report is a rendering of recorded cells. Nothing in it was recomputed: "
            "every figure below is a function of the result files and of nothing else. "
            "A value the harness could not measure is printed as <i>not measured</i> and never "
            f"as zero; a value with little behind it is marked {THIN_MARK} and printed to two "
            "significant figures rather than four.",
            sheet["Body"],
        )
    )
    story.append(
        _paragraph(
            f"read from <font face='Courier'>{_escape(data.directory or '?')}</font>",
            sheet["Small"],
        )
    )
    if data.stale:
        story.append(
            _paragraph(
                f"<b>{len(data.stale)} other suite digest(s) are present in this directory</b> "
                f"({_escape(', '.join(data.stale))}) and their cells are excluded. A directory "
                "holding two suites is usually a merge that should not have happened.",
                sheet["Body"],
            )
        )


def _what_was_tested(story: list[Any], sheet: Any, data: ReportData) -> None:
    story.append(_paragraph("What was tested", sheet["H2"]))
    rows = [["axis", "what it varies", "values run", "held at"]]
    for axis, labels in data.axes_seen.items():
        swept = len(labels) > 1
        rows.append(
            [
                axis,
                _shorten(AXES[axis].doc, 58),
                ", ".join(labels) if swept else "not swept",
                data.baseline.get(axis, "?"),
            ]
        )
    story.append(_table(rows, widths=[62, 210, 130, 60]))
    story.append(
        _paragraph(
            "An axis marked <i>not swept</i> was held at its baseline for every cell. "
            "This report says nothing about what happens when it moves.",
            sheet["Small"],
        )
    )
    story.append(
        _paragraph(
            "The <b>probes</b> axis is the probe-limit regime: how much probing the deployment "
            "permits. There is no protocol answer to that (open question Q6), so it is a "
            "scenario setting rather than a constant, and two runs made under different values "
            "of it are not the same experiment. This report's baseline regime is "
            f"<b>{_escape(data.baseline.get('probes', 'unrecorded'))}</b>.",
            sheet["Body"],
        )
    )
    if data.one_at_a_time:
        story.append(
            _paragraph(
                "No cell moved more than one axis off the baseline, so <b>no interaction between "
                "axes was measured</b>. Each reading is that axis alone, against the baseline.",
                sheet["Body"],
            )
        )
    story.append(
        _paragraph(
            f"{len(data.substrate_digests)} distinct substrate digest(s) across the cells; "
            "each cell's seed is derived from the suite name, the model, the axis values and the "
            "repeat, so a reader with this report and the suite can rebuild any cell's world "
            "exactly. First few seeds: "
            + _escape(
                ", ".join(f"{k} = {v}" for k, v in list(data.seeds.items())[:3]) or "none recorded"
            ),
            sheet["Small"],
        )
    )


def _strata(names: Sequence[str]) -> list[str]:
    """The columns one metric gets, in the order a reader expects.

    A metric that returns a mapping on one cell and ``None`` on another lands in
    the results under *both* its bare name and its stratified ones -- a point
    estimator scores no coverage, so ``coverage`` is ``None`` for it while
    ``coverage.h60`` exists for the distributional models. The bare name is
    dropped when any stratum is present: a column of "not measured" under the
    heading ``value`` says nothing and reads as a sixth horizon.

    Horizons sort numerically. Lexicographically, h300 comes before h60, and a
    report whose columns run 0, 300, 60 invites exactly the misreading the
    stratification exists to prevent.
    """
    suffixes = [n.partition(".")[2] for n in names]
    if any(suffixes):
        names = [n for n, suffix in zip(names, suffixes, strict=True) if suffix]

    def key(name: str) -> tuple[int, float, str]:
        suffix = name.partition(".")[2]
        if suffix.startswith("h") and suffix[1:].isdigit():
            return (0, float(suffix[1:]), suffix)
        return (1, 0.0, suffix)

    return sorted(names, key=key)


def _plain(doc: str) -> str:
    """A registry docstring as prose.

    Strips the RST double backticks and the Markdown asterisks: the docstrings
    are written for a code reader and printing ``**An upper bound.**`` on a page
    is the markup leaking, not emphasis.
    """
    return " ".join(doc.replace("``", "").replace("**", "").split())


def _shorten(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _declarations(story: list[Any], sheet: Any, data: ReportData) -> None:
    story.append(_paragraph("What each model declared about itself", sheet["H2"]))
    if not data.capabilities:
        story.append(
            _paragraph(
                "Not recorded. These cells predate the harness recording a model's declaration, "
                "so this report cannot say what any of them claimed to do.",
                sheet["Body"],
            )
        )
        return
    flags = sorted(
        {k for caps in data.capabilities.values() for k, v in caps.items() if isinstance(v, bool)}
    )
    # Capability down, model across. The other way round runs off the page as
    # soon as there are a dozen flags, and the flag list grows with the spec
    # while the model list is whatever the user submitted plus five.
    names = [label for label, _ in data.models]
    rows = [["declares"] + names]
    for flag in flags:
        row = [flag.replace("_", " ")]
        for label in names:
            caps = data.capabilities.get(label)
            row.append("?" if caps is None else ("yes" if caps.get(flag) else "–"))
        rows.append(row)
    story.append(_table(rows))
    story.append(
        _paragraph(
            "A declaration is a claim, not a result. Whether behaviour matched it is the "
            "conformance section; a model that declares nothing is not thereby failing anything.",
            sheet["Small"],
        )
    )


def _architectures(story: list[Any], sheet: Any, data: ReportData) -> None:
    """Architecture before variant, which is the order the question is asked in.

    Deliberately above the model tables and deliberately hedged: with a handful
    of variants per tag this compares the best variants anybody submitted, not
    the architectures. The variant count prints beside every row so a reader can
    see which rows are a comparison and which are one model with a label.
    """
    if not data.architecture:
        return
    story.append(_paragraph("Architectures", sheet["H2"]))

    groups: dict[str, list[str]] = {}
    for name, _ in data.models:
        groups.setdefault(data.architecture.get(name) or "unrecorded", []).append(name)

    metrics = [m for m in HEADLINE.values() if any(k[1] == m for k in data.at_baseline)]
    header = ["architecture", "variants"] + [_shorten(m, 16) for m in metrics] + ["best variant"]
    rows = [header]
    for tag in sorted(groups, key=lambda t: (t == "unrecorded", t)):
        names = groups[tag]
        # Distinct models, not distinct rows: a parity pair is one variant run
        # two ways, and letting it count twice would carry a tag over the
        # threshold on the strength of a duplicate.
        variants = len({data.bare_of.get(n, n) for n in names})
        cells = [f"{tag}{THIN_MARK if variants < THIN_VARIANTS else ''}", str(variants)]
        best_names = []
        for metric in metrics:
            entry = REGISTRY.get(metric.partition(".")[0])
            higher = bool(entry.higher_is_better) if entry is not None else False
            scored = [
                (data.at_baseline[(n, metric)], n)
                for n in names
                if (n, metric) in data.at_baseline and data.at_baseline[(n, metric)].measured
            ]
            if not scored:
                cells.append("not measured")
                continue
            pick = (
                max(scored, key=lambda p: p[0].value or 0.0)
                if higher
                else min(scored, key=lambda p: p[0].value or 0.0)
            )
            cells.append(pick[0].render())
            best_names.append(pick[1])
        # Which variant won. Collapsed to bare labels first: one model winning
        # under both drives is one winner, and printing the pair here would say
        # the architecture has two champions when it has one run two ways.
        # Several bare labels means the tag's row is several models, and naming
        # only the first would read as the architecture's champion.
        winners = sorted({data.bare_of.get(n, n) for n in best_names})
        cells.append(_shorten(" / ".join(winners), 40) if winners else "-")
        rows.append(cells)

    story.append(_table(rows))
    story.append(
        _paragraph(
            "Each cell is the <b>best variant</b> under that tag, not the tag's average -- an "
            "architecture is judged by what it can do, and averaging a good variant with a bad "
            "one measures the submission, not the architecture. A tag marked "
            f"{THIN_MARK} has fewer than {THIN_VARIANTS} variants behind it, which makes its "
            "row a statement about one or two models that happen to share a label.",
            sheet["Body"],
        )
    )
    if data.paired:
        story.append(
            _paragraph(
                "<b>"
                + _escape(", ".join(data.paired))
                + "</b> appears twice, once per drive. Those two rows share a world and differ "
                "only in who chose the probes, so their difference is what choosing your own "
                "probes was worth here -- and nothing else in this report measures that.",
                sheet["Body"],
            )
        )


def _family_section(
    story: list[Any], sheet: Any, data: ReportData, family: str, width: float
) -> None:
    metrics = data.metrics_by_family.get(family, [])
    if not metrics:
        return
    from reportlab.platypus import PageBreak

    story.append(PageBreak())
    story.append(_paragraph(f"{family.capitalize()}", sheet["H2"]))

    bases: dict[str, list[str]] = {}
    for name in metrics:
        bases.setdefault(name.partition(".")[0], []).append(name)

    if family == "accuracy":
        _accuracy_figures(story, sheet, data, width)

    headline = HEADLINE.get(family)
    if headline in metrics:
        story.append(
            _image(
                model_ranking(
                    _ranking_rows(data, headline),
                    metric=headline,
                    title=f"{headline} at the baseline cell (grey = mandatory baseline)",
                ),
                width,
            )
        )

    for base in sorted(bases):
        entry = REGISTRY[base]
        strata = _strata(bases[base])
        suffixes = [s.partition(".")[2] for s in strata]
        header = ["model"] + [s or "value" for s in suffixes]
        rows = [header]
        for label, mandatory in data.models:
            cells = [f"{label}{'  *' if mandatory else ''}"]
            for name in strata:
                cells.append(data.at_baseline.get((label, name), Reading(metric=name)).render())
            rows.append(cells)
        story.append(_paragraph(f"{base} — {_escape(_plain(entry.doc))}", sheet["H3"]))
        story.append(_table(rows))
        direction = (
            "closer to zero is better"
            if entry.higher_is_better is None
            else ("higher is better" if entry.higher_is_better else "lower is better")
        )
        story.append(
            _paragraph(f"{direction}; * mandatory baseline (Master Spec §28)", sheet["Small"])
        )


def _accuracy_figures(story: list[Any], sheet: Any, data: ReportData, width: float) -> None:
    horizons = data.horizons
    if not horizons:
        story.append(
            _paragraph(
                "No forecast was scored. Either the run did not record forecasts, or every "
                "horizon fell past the end of it. Nothing below is an accuracy result.",
                sheet["Body"],
            )
        )
        return
    for metric, nominal in (("coverage", NOMINAL), ("crps", None)):
        full = {
            label: [
                data.at_baseline.get((label, f"{metric}.{h}"), Reading(metric="")).value
                for h in horizons
            ]
            for label, _ in data.models
        }
        # A model that scored nothing here is left out of the figure rather than
        # drawn as a row of empty slots between the bars that do exist. The
        # table below carries it as "not measured", which is where an absence
        # belongs; in a bar chart it is just a gap the eye reads as zero.
        series = {k: v for k, v in full.items() if any(x is not None for x in v)}
        if not series:
            continue
        story.append(_image(horizon_bars(horizons, series, metric=metric, nominal=nominal), width))
        missing = [k for k in full if k not in series]
        if missing:
            story.append(
                _paragraph(
                    f"Not in this figure, having produced no {metric}: "
                    f"{_escape(', '.join(missing))}. A point estimator has no interval to score.",
                    sheet["Small"],
                )
            )


def _axis_response(
    story: list[Any], sheet: Any, data: ReportData, width: float, metrics: Sequence[str]
) -> None:
    from reportlab.platypus import PageBreak

    story.append(PageBreak())
    story.append(_paragraph("Response to stress", sheet["H2"]))
    story.append(
        _paragraph(
            "One axis at a time, every other axis held at its baseline. A gap in a line is a "
            "cell that produced no value; the line is not joined across it, because a failed "
            "cell must not read as a smooth response. Dashed thin lines are the mandatory "
            "baselines; solid heavy lines are the models this suite was run for.",
            sheet["Body"],
        )
    )
    mandatory = [label for label, is_base in data.models if is_base]
    drew = False
    for axis in data.axes_seen:
        if len(data.axes_seen[axis]) < 2:
            continue
        for metric in metrics:
            labels, series = _series(data, axis, metric)
            if not any(v is not None for row in series.values() for v in row):
                continue
            story.append(
                _image(
                    axis_response(labels, series, axis=axis, metric=metric, mandatory=mandatory),
                    width,
                )
            )
            drew = True
    if not drew:
        story.append(
            _paragraph(
                "No axis was swept at more than one value, so there is no response to show.",
                sheet["Body"],
            )
        )


def _conformance(story: list[Any], sheet: Any, card: Mapping[str, Any] | None) -> None:
    from reportlab.platypus import PageBreak

    story.append(PageBreak())
    story.append(_paragraph("Conformance", sheet["H2"]))
    if card is None:
        story.append(
            _paragraph(
                "<b>No conformance card was supplied.</b> This report therefore says nothing "
                "about whether the model's declarations above were checked against its "
                "behaviour, and in particular it does not distinguish a capability the model "
                "honestly declined from one it claimed and does not have. Produce a card with "
                "<font face='Courier'>scionfit check --format json --out card.json</font> and "
                "re-render.",
                sheet["Body"],
            )
        )
        return
    summary = card.get("summary", {}) if isinstance(card.get("summary"), Mapping) else {}
    model = card.get("model", {}) if isinstance(card.get("model"), Mapping) else {}
    story.append(
        _paragraph(
            f"<b>{_escape(str(model.get('name', '?')))}</b> "
            f"v{_escape(str(model.get('version', '?')))} &middot; verdict "
            f"<b>{_escape(str(summary.get('verdict', '?')))}</b> &middot; mean score "
            f"{summary.get('mean_score', '?')} &middot; closed-loop ready: "
            f"{'yes' if summary.get('closed_loop_ready') else 'no'}",
            sheet["Body"],
        )
    )
    rows = [["probe", "requirement", "status", "score", "finding"]]
    for result in card.get("results", []) or []:
        if not isinstance(result, Mapping):
            continue
        score = result.get("score")
        rows.append(
            [
                str(result.get("probe_id", "?")),
                str(result.get("requirement", "")),
                str(result.get("status", "?")),
                f"{float(score):.2f}" if isinstance(score, (int, float)) else "–",
                _shorten(str(result.get("finding", "")), 110),
            ]
        )
    story.append(_table(rows, widths=[34, 90, 66, 30, 242]))
    story.append(
        _paragraph(
            "<b>DECLARED_ABSENT is not FALSE_CLAIM.</b> The first is a model saying it does not "
            "do something and being right; the second is a model claiming a capability its "
            "behaviour contradicts. Only the second is a defect, and the two are carried "
            "separately all the way to this page rather than collapsed into a tick.",
            sheet["Small"],
        )
    )


def _failures(story: list[Any], sheet: Any, data: ReportData) -> None:
    if data.refused:
        story.append(_paragraph("Cells that did not apply", sheet["H2"]))
        listed = ", ".join(f"{label} ({n})" for label, n in sorted(data.refused.items()))
        story.append(
            _paragraph(
                f"{_escape(listed)} &mdash; asked for a drive the model has no code path for, "
                "which for a forecaster asked to be agentic is a description rather than a "
                "fault. These cells cost nothing: the refusal is resolved before a world is "
                "built. They are listed apart from failures because rendering them alike "
                "would say the floor is broken.",
                sheet["Body"],
            )
        )
    if not data.failures:
        return
    story.append(_paragraph("Cells that failed", sheet["H2"]))
    story.append(
        _paragraph(
            "A cell that ran and failed is a result, not a hole: it is a finding about the "
            "model at that point of the matrix. A cell that was never attempted has no row "
            "here at all, and the two are different states.",
            sheet["Body"],
        )
    )
    rows = [["model", "where", "what happened"]]
    for label, where, error in data.failures[:60]:
        rows.append([label, where, _shorten(error, 120)])
    story.append(_table(rows, widths=[110, 130, 222]))
    if len(data.failures) > 60:
        story.append(
            _paragraph(f"{len(data.failures) - 60} more, in the result files.", sheet["Small"])
        )


def _limits(story: list[Any], sheet: Any, data: ReportData) -> None:
    story.append(_paragraph("What this report does not claim", sheet["H2"]))
    points = [
        "<b>Regret is an upper bound, not regret.</b> It is measured against the cheapest path "
        "as costs actually were, which is a lower bound on achievable cost because moving the "
        "whole scope onto that path would have raised it. The bound is identical for every "
        "model on the same world, so the comparison holds; the level does not.",
        f"<b>The numbers are the {_escape(data.tier)} tier.</b> The tier that matters is "
        "<i>realistic</i> (2,000 ASes, 10,000 inter-AS links). A number from a smaller tier "
        "describes a smaller network and nothing else."
        if data.tier != "realistic"
        else "<b>These are realistic-tier numbers</b> (2,000 ASes, 10,000 inter-AS links).",
        f"<b>A value marked {THIN_MARK} is thin.</b> Fewer than {THIN_REPEATS} repeats, or fewer "
        f"than {THIN_SUPPORT} observations inside the run. The headline stability metric was "
        "measured bimodal across seeds, so a median of two runs of it describes neither.",
        "<b>The operational numbers are machine-dependent.</b> No cell records the hardware "
        "it ran on, so <i>wall_clock_s</i> and the decision-latency percentiles compare "
        "models only within this run, on whatever took it. A result file reproduces the "
        "world exactly and the measurement only on the machine that made it.",
        "<b>The harness does not summarise for the model</b> and there is no loss curve here. "
        "This evaluates trained models; it does not train them, and a model's own learning "
        "dynamics are outside what any cell recorded.",
    ]
    if "measured" in data.think_seen:
        points.append(
            "<b>The decision times are this machine's.</b> This run charged the model's own "
            "compute to the simulated clock, so a slower or faster machine produces a "
            "different run &mdash; not merely a different measurement of the same run. That is "
            "the price of measuring what a model actually costs, and it is why the suite does "
            "not do it by default."
        )
    elif data.think_seen and "free" in data.think_seen:
        points.append(
            "<b>Thinking was free here.</b> Only time spent inside tools moved the clock, so a "
            "model that deliberates for seconds and probes nothing was applied as though it "
            "had answered instantly. Fine for models that decide in microseconds; misleading "
            "for one that does not."
        )
    if len(data.think_seen) > 1:
        points.append(
            "<b>These cells were not all charged the same way</b> ("
            + _escape(", ".join(sorted(data.think_seen)))
            + "). A cell that paid for its thinking and one that did not are two different "
            "experiments, and any column holding both compares nothing."
        )
    drives = {d for d in data.drive_of.values() if d}
    if "fixed" in drives:
        points.append(
            "<b>A model shown under <i>fixed</i> is not being shown at its best.</b> The "
            "probing policy in that mode is the harness's -- query the scope, probe two paths, "
            "round robin -- and it is dumb on purpose so that every model faces the same one. "
            "That makes it fair without making it good, and a tool-using model measured under "
            "it is being told what to look at rather than deciding."
        )
    if data.paired:
        points.append(
            "<b>The parity pair holds the world constant, not the effort.</b> Both halves run "
            "the same seed and the same scenario, so their difference is the probing policy "
            "and the time it costs -- but the agentic half also spends its slot differently, "
            "and an operational number is not comparable across the pair the way an accuracy "
            "number is."
        )
    if data.one_at_a_time:
        points.append(
            "<b>No interaction between axes was measured.</b> Every reading holds the other "
            "five axes at their baseline, so a model that survives each stress alone may still "
            "fail two together and nothing here would show it."
        )
    for point in points:
        story.append(_paragraph("&bull; " + point, sheet["Body"]))


def build_pdf(
    directory: Path | str,
    out: Path | str,
    *,
    conformance: Path | str | None = None,
    axis_metrics: Sequence[str] = AXIS_METRICS,
) -> Path:
    """Render a results directory to a PDF. Returns the path written."""
    require_deps()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    directory = Path(directory)
    results = list(load_results(directory))
    if not results:
        raise ValueError(f"no result files in {directory}; run a sweep into it first")

    data = gather(results)
    data.directory = str(directory)

    card: Mapping[str, Any] | None = None
    if conformance is not None:
        loaded = json.loads(Path(conformance).read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{conformance} is not a conformance card")
        card = loaded

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet = _styles()
    margin = 16 * mm
    width = A4[0] - 2 * margin
    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"scionarena benchmark: {data.suite}",
        author="scionarena",
    )

    story: list[Any] = []
    _cover(story, sheet, data)
    _what_was_tested(story, sheet, data)
    _architectures(story, sheet, data)
    _declarations(story, sheet, data)
    for family in FAMILIES:
        _family_section(story, sheet, data, family, width)
    _axis_response(story, sheet, data, width, axis_metrics)
    _conformance(story, sheet, card)
    _failures(story, sheet, data)
    _limits(story, sheet, data)
    doc.build(story)
    return out


def summary_lines(data: ReportData) -> list[str]:
    """A terminal echo of what the PDF will say, for the CLI to print."""
    lines = [
        f"suite {data.suite} ({data.suite_digest}), tier {data.tier}, {data.n_cells} cells",
        f"{len(data.models)} models, {len(data.failures)} failed cells, "
        f"{sum(1 for v in data.axes_seen.values() if len(v) > 1)} axes swept "
        f"and {sum(1 for v in data.axes_seen.values() if len(v) == 1)} held",
    ]
    tags = {t or "unrecorded" for t in data.architecture.values()}
    if tags:
        lines.append(f"{len(tags)} architectures: " + ", ".join(sorted(tags)))
    if data.paired:
        lines.append("parity pairs (one world, both drives): " + ", ".join(data.paired))
    if data.refused:
        lines.append(
            f"{sum(data.refused.values())} cells did not apply (no such drive): "
            + ", ".join(sorted(data.refused))
        )
    thin = sum(1 for r in data.at_baseline.values() if r.thin)
    absent = sum(1 for r in data.at_baseline.values() if not r.measured)
    lines.append(f"{thin} baseline readings are thin, {absent} were not measured at all")
    if data.stale:
        lines.append(
            "excluded cells from another suite in this directory: " + ", ".join(data.stale)
        )
    return lines
