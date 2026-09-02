"""Charts for the PDF. Plain data in, PNG bytes out.

matplotlib lives behind the ``[report]`` extra and is imported **inside** each
function rather than at module scope. ``scionarena bench axes`` and ``bench
show`` have to keep working on an install that has no plotting stack, and a
top-level import would take the whole CLI down with them (ADR 0018).

Nothing here reads a result object, a session or a model -- the same rule the
rest of ``instrument`` follows, and the reason a saved directory renders as well
as a live run. Every function takes sequences and mappings and returns bytes.

The palette is ``instrument.report``'s, deliberately: the interactive HTML view
and the PDF are different artefacts, but a reader who has both should not have
to learn two colour schemes to know which line is which model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .report import PALETTE

__all__ = [
    "EXTRA_HINT",
    "MissingReportDeps",
    "available",
    "axis_response",
    "horizon_bars",
    "model_ranking",
    "require",
]

#: Said once, here, so every entry point says the same thing.
EXTRA_HINT = (
    "the report needs matplotlib and reportlab, which are not part of the "
    "substrate. Install them with:  pip install 'scionarena[report]'"
)

#: Wide enough to fill a portrait A4 text column at 150 dpi without upscaling.
DPI = 150
FIGSIZE = (7.2, 3.4)


class MissingReportDeps(RuntimeError):
    """Raised, with the install line, when the reporting extra is absent."""


def available() -> bool:
    """Whether a PDF can be built at all. Cheap enough to call in a CLI."""
    try:
        import matplotlib  # noqa: F401
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def require() -> None:
    if not available():
        raise MissingReportDeps(EXTRA_HINT)


def _pyplot() -> Any:
    """matplotlib with a backend that needs no display, or one clear error."""
    try:
        import matplotlib
    except ImportError as exc:  # pragma: no cover -- exercised by test_report
        raise MissingReportDeps(EXTRA_HINT) from exc
    # Before pyplot, or pyplot picks an interactive backend and a headless CI
    # run dies inside a chart rather than at the import that caused it.
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _render(fig: Any) -> bytes:
    import io

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=DPI, bbox_inches="tight")
    _pyplot().close(fig)
    return buffer.getvalue()


def _colour(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def _legend(ax: Any, n: int) -> None:
    """Below the axes, always.

    ``loc="best"`` put the key over the nominal-coverage line in the first
    figure this drew, which is the one place on that chart a reader has to be
    able to see.
    """
    ax.legend(
        fontsize=7,
        frameon=False,
        ncols=min(4, max(1, n)),
        loc="upper center",
        bbox_to_anchor=(0.5, -0.22),
    )


def _style(ax: Any, *, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(labelsize=8)
    ax.grid(axis="y", linewidth=0.4, alpha=0.4)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def model_ranking(
    rows: Sequence[Mapping[str, Any]],
    *,
    metric: str,
    title: str = "",
    reference: float | None = None,
) -> bytes:
    """Models ranked on one metric, with the observed range as an error bar.

    ``rows`` are ``{"model", "value", "lo", "hi", "mandatory"}``. The mandatory
    baselines are drawn in a flat grey and every other model in the palette,
    because the point of the figure is the gap between a submitted model and the
    floor beneath it, not the identity of the five baselines.

    A model with no value is drawn as a labelled gap rather than dropped: an
    absent bar reads as an absent problem.
    """
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], max(1.6, 0.34 * len(rows) + 1.0)))

    labels = [str(r.get("model", "?")) for r in rows]
    y = list(range(len(rows)))
    index = 0
    for position, row in zip(y, rows, strict=True):
        value = row.get("value")
        if value is None:
            ax.text(0.0, position, "  not measured", fontsize=8, va="center", color="#8795a4")
            continue
        if row.get("mandatory"):
            colour = "#9aa7b4"
        else:
            colour = _colour(index)
            index += 1
        lo = float(row.get("lo", value))
        hi = float(row.get("hi", value))
        ax.barh(position, float(value), color=colour, height=0.6)
        if hi > lo:
            ax.plot([lo, hi], [position, position], color="#33414f", linewidth=1.2)
            ax.plot(
                [lo, lo, hi, hi],
                [position, position, position, position],
                "|",
                color="#33414f",
                markersize=5,
            )

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    if reference is not None:
        ax.axvline(reference, color="#d1495b", linewidth=1.0, linestyle="--")
        ax.text(reference, -0.8, f" {reference:g}", fontsize=8, color="#d1495b")
    _style(ax, title=title or metric, xlabel=metric, ylabel="")
    ax.grid(axis="x", linewidth=0.4, alpha=0.4)
    ax.grid(axis="y", visible=False)
    return _render(fig)


def axis_response(
    labels: Sequence[str],
    series: Mapping[str, Sequence[float | None]],
    *,
    axis: str,
    metric: str,
    mandatory: Sequence[str] = (),
) -> bytes:
    """One metric against one axis, one line per model.

    The x axis is categorical and stays in the order given, which is the order
    the axis declares its values in -- ordinal, not numeric, because "10pct" and
    "30pct" are labels and spacing them by their numbers would imply an
    interpolation the sweep never measured.

    A model missing a point leaves a gap in its line instead of joining across
    it, so a cell that failed cannot be read as a smooth response.
    """
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=FIGSIZE)
    x = list(range(len(labels)))
    baselines = set(mandatory)
    # Weight, not hue, separates a baseline from a submitted model: five
    # baselines drawn in the same grey are five lines nobody can tell apart,
    # and the legend then names colours that are all the same colour.
    for index, (name, values) in enumerate(series.items()):
        colour = _colour(index)
        width, style = (1.0, "--") if name in baselines else (2.2, "-")
        ys = [None if v is None else float(v) for v in values]
        ax.plot(
            x,
            ys,
            marker="o",
            markersize=3.5,
            color=colour,
            linewidth=width,
            linestyle=style,
            label=name,
        )
    ax.set_xticks(x)
    ax.set_xticklabels(list(labels), fontsize=8)
    _style(ax, title=f"{metric} against {axis}", xlabel=axis, ylabel=metric)
    _legend(ax, len(series))
    return _render(fig)


def horizon_bars(
    horizons: Sequence[str],
    series: Mapping[str, Sequence[float | None]],
    *,
    metric: str,
    nominal: float | None = None,
) -> bytes:
    """One accuracy metric, grouped by horizon, one bar per model.

    Grouped rather than stacked or averaged: an accuracy metric is stratified by
    horizon by construction (ADR 0017) and a figure that pooled the horizons
    would undo in the rendering what the registry enforces in the computation.
    """
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=FIGSIZE)
    names = list(series)
    n = max(1, len(names))
    width = 0.8 / n
    for index, name in enumerate(names):
        values = [0.0 if v is None else float(v) for v in series[name]]
        offsets = [i - 0.4 + width * (index + 0.5) for i in range(len(horizons))]
        ax.bar(offsets, values, width=width, color=_colour(index), label=name)
    ax.set_xticks(list(range(len(horizons))))
    ax.set_xticklabels(list(horizons), fontsize=8)
    if nominal is not None:
        ax.axhline(nominal, color="#d1495b", linewidth=1.1, linestyle="--")
        ax.text(
            len(horizons) - 0.5,
            nominal,
            f" nominal {nominal:g}",
            fontsize=8,
            color="#d1495b",
            va="bottom",
            ha="right",
        )
    _style(ax, title=f"{metric} by horizon", xlabel="forecast horizon", ylabel=metric)
    _legend(ax, len(names))
    return _render(fig)
