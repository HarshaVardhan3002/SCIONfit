"""Rendering. Turns series into something a person can look at.

Self-contained SVG written by hand rather than by a plotting library, because
the alternative is a dependency that exists only to draw two line charts and a
table. The output is one HTML file with everything inlined, so it opens from a
file:// URL on a machine with nothing installed -- which is the machine a
demonstration is usually given on.

Nothing here reads a model or a session. It takes plain sequences and plain
dictionaries, which is what lets ``instrument`` sit under ``exposure`` in the
layering: a saved trace renders exactly as well as a live run.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["CSS", "svg_lines", "report_card", "render_body", "render_html"]

#: Enough to tell four or five series apart on a projector, in that order.
PALETTE = ("#d1495b", "#0f4c81", "#28a745", "#f2a541", "#7768ae", "#3f8f8b")

CSS = """
body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 0 auto; max-width: 1100px; padding: 32px 24px 64px; color: #17202a; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 36px 0 6px; }
p.sub { color: #5b6b7c; margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 4px; font-size: 14px; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid #e3e8ee; }
th:first-child, td:first-child { text-align: left; }
th { font-weight: 600; color: #5b6b7c; border-bottom: 2px solid #cfd8e3; }
tr.headline td { background: #fdf3f4; font-weight: 600; }
td.pass { color: #1a7f37; } td.fail { color: #b42318; }
.key { display: inline-block; margin: 4px 14px 4px 0; font-size: 13px; }
.key i { display: inline-block; width: 22px; height: 3px; vertical-align: middle;
         margin-right: 6px; }
.note { background: #f7f9fc; border-left: 3px solid #cfd8e3; padding: 10px 14px;
        margin: 14px 0; font-size: 14px; color: #35485c; }
code { background: #f2f5f9; padding: 1px 4px; border-radius: 3px; }
"""


def svg_lines(
    series: Mapping[str, Sequence[float]],
    *,
    width: int = 1040,
    height: int = 260,
    y_label: str = "",
    x_label: str = "decision round",
    y_max: float | None = None,
) -> str:
    """One chart, several series, no dependencies.

    Series are drawn in the order given and share one y axis, so only pass
    things measured in the same units. The y axis starts at zero: these are
    load fractions, and a truncated axis would make a calm model look dramatic.
    """
    left, right, top, bottom = 56, 16, 14, 34
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for s in series.values() for v in s]
    if not values:
        return f'<svg width="{width}" height="{height}"></svg>'
    hi = max(y_max if y_max is not None else max(values), 1e-9)
    n = max(max(len(s) for s in series.values()), 2)

    def x(i: int) -> float:
        return left + plot_w * i / (n - 1)

    def y(v: float) -> float:
        return top + plot_h * (1.0 - min(max(v, 0.0), hi) / hi)

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = top + plot_h * (1.0 - frac)
        parts.append(
            f'<line x1="{left}" y1="{gy:.1f}" x2="{left + plot_w}" y2="{gy:.1f}" '
            f'stroke="#e3e8ee" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="#7b8794">{hi * frac:.2f}</text>'
        )
    for index, (name, values_) in enumerate(series.items()):
        colour = PALETTE[index % len(PALETTE)]
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values_))
        parts.append(
            f'<polyline fill="none" stroke="{colour}" stroke-width="1.6" '
            f'stroke-linejoin="round" points="{points}"/>'
        )
        del name
    parts.append(
        f'<text x="{left + plot_w / 2:.0f}" y="{height - 6}" text-anchor="middle" '
        f'font-size="12" fill="#5b6b7c">{html.escape(x_label)}</text>'
    )
    if y_label:
        parts.append(
            f'<text x="14" y="{top + plot_h / 2:.0f}" font-size="12" fill="#5b6b7c" '
            f'transform="rotate(-90 14 {top + plot_h / 2:.0f})" '
            f'text-anchor="middle">{html.escape(y_label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _key(names: Sequence[str]) -> str:
    return "".join(
        f'<span class="key"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>'
        f"{html.escape(name)}</span>"
        for i, name in enumerate(names)
    )


def report_card(rows: Sequence[Mapping[str, Any]], *, columns: Sequence[str] = ()) -> str:
    """The same numbers as a fixed-width table, for a terminal or a log."""
    if not rows:
        return "(no runs)"
    keys = list(columns) if columns else list(rows[0].keys())
    widths = {k: max(len(k), *(len(_fmt(r.get(k))) for r in rows)) for k in keys}
    line = "  ".join(k.ljust(widths[k]) for k in keys)
    rule = "  ".join("-" * widths[k] for k in keys)
    body = ["  ".join(_fmt(r.get(k)).ljust(widths[k]) for k in keys) for r in rows]
    return "\n".join([line, rule, *body])


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def render_body(
    sections: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    verdicts: Sequence[Mapping[str, Any]] = (),
    meta: Mapping[str, Any] | None = None,
) -> str:
    """Everything below the title, as a fragment.

    ``sections`` are ``{"title", "note", "series", "y_label", "y_max"}``.
    ``verdicts`` are ``{"criterion", "measured", "ok"}`` and are rendered first,
    because a demonstration that makes the reader hunt for whether it passed is
    a demonstration that is hiding something.

    Split out from :func:`render_html` so that the saved file and the live UI
    are the same rendering rather than two that drift apart.
    """
    out: list[str] = []
    if verdicts:
        out.append("<h2>Acceptance criteria</h2><table><tr><th>criterion</th>")
        out.append("<th>measured</th><th>verdict</th></tr>")
        for v in verdicts:
            state = "pass" if v.get("ok") else "fail"
            out.append(
                f"<tr><td>{html.escape(str(v['criterion']))}</td>"
                f"<td>{html.escape(_fmt(v.get('measured')))}</td>"
                f"<td class='{state}'>{'met' if v.get('ok') else 'NOT met'}</td></tr>"
            )
        out.append("</table>")

    for section in sections:
        series: Mapping[str, Sequence[float]] = section.get("series", {})
        out.append(f"<h2>{html.escape(str(section.get('title', '')))}</h2>")
        if section.get("note"):
            out.append(f"<div class='note'>{section['note']}</div>")
        out.append(_key(list(series)))
        out.append(
            svg_lines(
                series,
                y_label=str(section.get("y_label", "")),
                y_max=section.get("y_max"),
            )
        )

    if rows:
        out.append("<h2>Report card</h2><table><tr>")
        keys = list(rows[0].keys())
        out += [f"<th>{html.escape(k)}</th>" for k in keys]
        out.append("</tr>")
        for row in rows:
            cells = "".join(f"<td>{html.escape(_fmt(row.get(k)))}</td>" for k in keys)
            out.append(f"<tr>{cells}</tr>")
        out.append("</table>")

    if meta:
        out.append("<h2>Run</h2><pre style='font-size:12px;color:#35485c'>")
        out.append(html.escape(json.dumps(dict(meta), indent=2, sort_keys=True)))
        out.append("</pre>")
    return "".join(out)


def render_html(
    title: str,
    subtitle: str,
    sections: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    verdicts: Sequence[Mapping[str, Any]] = (),
    meta: Mapping[str, Any] | None = None,
) -> str:
    """One standalone page, everything inlined, openable from a ``file://`` URL."""
    return "".join(
        [
            "<!doctype html><html><head><meta charset='utf-8'>",
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>",
            f"<h1>{html.escape(title)}</h1><p class='sub'>{html.escape(subtitle)}</p>",
            render_body(sections, rows, verdicts=verdicts, meta=meta),
            "</body></html>",
        ]
    )
