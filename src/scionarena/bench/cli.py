"""``scionarena bench ...`` -- run a suite, list the axes, read a directory.

    scionarena bench axes                            # what gets varied, and how
    scionarena bench plan --models mypkg.mine:Model  # what would run, for free
    scionarena bench run   --models mypkg.mine:Model --tier dev --out results/
    scionarena bench show  results/                  # read what is there
    scionarena bench report results/ --out report.pdf  # the artefact

``plan`` before ``run`` is the cheap check that a suite is the size you think it
is: the grid is 1,458 points and one-at-a-time is sixteen, and the difference at
the realistic tier is a coffee against a weekend.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..exposure.loading import ModelLoadError
from ..instrument.figures import MissingReportDeps
from ..instrument.metrics import FAMILIES, REGISTRY
from .axes import AXES
from .report import AXIS_METRICS, build_pdf, gather, summary_lines
from .results import load_results
from .sweep import SweepSpec, plan, run_sweep, summarise


def _spec(args: argparse.Namespace) -> SweepSpec:
    models = tuple(m.strip() for m in (args.models or "").split(",") if m.strip())
    return SweepSpec(
        name=args.suite,
        models=models,
        tier=args.tier,
        mode=args.mode,
        repeats=args.repeats,
        cycles=args.cycles,
        decision_s=args.decision_s,
        scopes=args.scopes,
        only=tuple(a.strip() for a in (args.axes or "").split(",") if a.strip()),
        include_baselines=not args.no_baselines,
    )


def _add_suite_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--models", default="", help="comma-separated model specs")
    parser.add_argument("--suite", default="standard")
    parser.add_argument("--tier", default="dev", choices=["smoke", "dev", "realistic", "stress"])
    parser.add_argument(
        "--mode",
        default="oat",
        choices=["oat", "grid"],
        help="oat varies one axis at a time around the baseline (16 cells); "
        "grid is the cartesian product (1,458 cells)",
    )
    parser.add_argument("--axes", default="", help="comma-separated axes to move; default all")
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="worlds per cell. One is enough to discriminate and not enough to "
        "quote: the headline metric is bimodal across seeds.",
    )
    parser.add_argument("--cycles", type=int, default=120, help="under ~100 measures nothing")
    parser.add_argument("--decision-s", type=float, default=30.0, dest="decision_s")
    parser.add_argument("--scopes", type=int, default=8)
    parser.add_argument(
        "--no-baselines",
        action="store_true",
        help="skip the five mandatory baselines. A score with no floor under it "
        "means nothing; this is for tests.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scionarena bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_axes = sub.add_parser("axes", help="what a sweep varies")
    p_axes.add_argument("--json", action="store_true")

    p_metrics = sub.add_parser("metrics", help="what a sweep measures")
    p_metrics.add_argument("--json", action="store_true")

    p_plan = sub.add_parser("plan", help="what would run, without running it")
    _add_suite_args(p_plan)

    p_run = sub.add_parser("run", help="run a suite into a directory")
    _add_suite_args(p_run)
    p_run.add_argument("--out", default="results", help="one JSON file per cell lands here")
    p_run.add_argument("--workers", type=int, default=None)
    p_run.add_argument("--no-resume", action="store_true", help="re-run cells already on disk")

    p_show = sub.add_parser("show", help="read a results directory")
    p_show.add_argument("directory")
    p_show.add_argument("--metric", default="swing")
    p_show.add_argument("--json", action="store_true")

    p_report = sub.add_parser("report", help="render a results directory to a PDF")
    p_report.add_argument("directory")
    p_report.add_argument("--out", default="report.pdf")
    p_report.add_argument(
        "--conformance",
        default=None,
        help="a card from `scionfit check --format json`. Without one the report "
        "cannot say whether the model's declarations were checked, and says so.",
    )
    p_report.add_argument(
        "--axis-metric",
        action="append",
        default=None,
        dest="axis_metrics",
        help="metric to draw an axis-response figure for; repeatable. "
        f"Default: {', '.join(AXIS_METRICS)}",
    )

    args = parser.parse_args(argv)

    if args.command == "axes":
        return _axes(args)
    if args.command == "metrics":
        return _metrics(args)
    if args.command == "show":
        return _show(args)
    if args.command == "report":
        return _report(args)

    try:
        spec = _spec(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cells = plan(spec)
    if args.command == "plan":
        print(f"suite {spec.name} ({spec.digest()}), {spec.mode}, {len(cells)} cells")
        for cell in cells:
            print(f"  {cell.describe()}")
        return 0

    out = Path(args.out)
    print(f"suite {spec.name} ({spec.digest()}) -> {out}, {len(cells)} cells")

    def progress(done: int, total: int, ident: str, error: str | None) -> None:
        print(f"  {done}/{total}  {ident}  {error or 'ok'}")

    try:
        results = run_sweep(
            spec, out, workers=args.workers, resume=not args.no_resume, on_cell=progress
        )
    except ModelLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results)} cells in {out}, {len(failed)} failed")
    # Every cell failing is a broken suite; some cells failing is a finding
    # about a model, and the report says which ones and why.
    return 1 if failed and len(failed) == len(results) else 0


def _axes(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({n: a.to_dict() for n, a in AXES.items()}, indent=2))
        return 0
    for name, axis in AXES.items():
        print(f"{name}  -- {axis.doc}")
        for value in axis.values:
            mark = "  (baseline)" if value is axis.values[0] else ""
            note = f"  # {value.note}" if value.note else ""
            print(f"    {value.label:<14}{mark}{note}")
        print()
    return 0


def _metrics(args: argparse.Namespace) -> int:
    """The registry, by family. What `--metric` will take."""
    if args.json:
        print(
            json.dumps(
                {
                    name: {
                        "family": m.family,
                        "doc": m.doc,
                        "higher_is_better": m.higher_is_better,
                        "support": m.support,
                    }
                    for name, m in sorted(REGISTRY.items())
                },
                indent=2,
            )
        )
        return 0
    for family in FAMILIES:
        print(f"{family}")
        for name, entry in sorted(REGISTRY.items()):
            if entry.family == family and not entry.support:
                if entry.higher_is_better is None:
                    arrow = "closer to zero is better"
                else:
                    arrow = "higher is better" if entry.higher_is_better else "lower is better"
                print(f"    {name:<20} {entry.doc}")
                print(f"    {'':<20} ({arrow})")
        for name, entry in sorted(REGISTRY.items()):
            if entry.family == family and entry.support:
                print(f"    {name:<20} {entry.doc}")
                print(f"    {'':<20} (support: how much is behind the numbers above)")
        print()
    print("an accuracy metric reports one value per horizon, as name.h0 / name.h60 / name.h300")
    return 0


def _show(args: argparse.Namespace) -> int:
    results = list(load_results(Path(args.directory)))
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
        return 0
    if not results:
        print(f"nothing in {args.directory}", file=sys.stderr)
        return 1
    failed = [r for r in results if not r.ok]
    print(f"{len(results)} cells, {len(failed)} failed\n")
    print(f"{'where':<26}{'model':<22}{'n':>3}  {args.metric:>10}  range")
    for row in summarise(results, args.metric):
        mark = "*" if row["mandatory"] else " "
        if row[args.metric] is None:
            print(f"{row['where']:<26}{row['model']:<22}{row['n']:>3}  {row['error']}")
            continue
        span = f"{row['min']:.4g} .. {row['max']:.4g}"
        print(
            f"{row['where']:<26}{row['model']:<22}{row['n']:>3}  "
            f"{row[args.metric]:>10.4g}  {span} {mark}"
        )
    print("\nmedian over repeats; * mandatory baseline (Master Spec 28)")
    return 0


def _report(args: argparse.Namespace) -> int:
    """Render a directory to a PDF, and echo to the terminal what it says."""
    directory = Path(args.directory)
    try:
        out = build_pdf(
            directory,
            args.out,
            conformance=args.conformance,
            axis_metrics=tuple(args.axis_metrics or AXIS_METRICS),
        )
    except MissingReportDeps as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for line in summary_lines(gather(list(load_results(directory)))):
        print(line)
    print(f"\n{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
