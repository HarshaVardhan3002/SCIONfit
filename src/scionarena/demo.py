"""``scionarena demo`` -- the M3 result, in one command.

Runs the same scenario, the same seed and the same scopes against two
reference models, and writes a page showing what each did to the network. The
whole point is that the two runs differ in nothing except the model, so
anything visible in the figure is the model's doing.

    scionarena demo                       # smoke tier, seconds
    scionarena demo --tier dev --scopes 40
    scionarena demo --tier realistic --scopes 100 --cycles 120

Add ``--slow`` for a third run: the greedy model again, with extra seconds of
decision latency injected, which is the controlled version of "the model was
too slow" rather than the anecdotal one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scionarena.core.scenario import Scenario
from scionarena.exposure.loop import LoopConfig, LoopResult, busiest_scopes, run_loop
from scionarena.instrument.report import render_html, report_card
from scionarena.reference.models import REFERENCE_MODELS

__all__ = ["main", "run_demo", "sections", "verdicts"]

#: The ratios M3 is scored against, per ADR 0010. Ratios between two models on
#: one scenario, not absolute thresholds: fast-band amplitude is in the units of
#: the series it was measured on, so a threshold in those units would be a
#: number fitted to whichever tier it was first run at.
MIN_SWING_RATIO = 4.0
MIN_COST_RATIO = 2.0
#: Rounds allowed to finish late before the sample grid stops being a grid. Not
#: zero: the cadence is calibrated from a few rounds and a probe that waits on a
#: rate limit can still push one round past it. One in a hundred is jitter; a
#: tenth of them is a series the detectors should not be run over at all.
MAX_OVERRUN_FRACTION = 0.01

CARD_COLUMNS = (
    "model",
    "decision_s",
    "swing",
    "share_swing",
    "oscillation_index",
    "dominant_period",
    "flap_rate",
    "mean_cost_ms",
    "mean_deviation",
    "mean_latency_s",
    "overruns",
    "calls",
    "wall_clock_s",
)


def run_demo(
    *,
    tier: str = "smoke",
    scopes: int = 8,
    cycles: int = 240,
    hosts: int = 200,
    seed: int = 7,
    slow_s: float = 0.0,
    models: Sequence[str] = ("minrtt", "reference"),
    progress: Callable[[str, int, int], None] | None = None,
) -> list[LoopResult]:
    """Every run shares one scenario, one seed and one set of scopes.

    ``progress`` is called with ``(what_is_running, rounds_done, rounds_total)``
    and anything it raises propagates out of here unchanged; ``ui`` cancels a
    run that way.
    """
    scenario = Scenario.for_tier(tier, seed=seed)
    picked = busiest_scopes(scenario.build(), scopes)
    config = LoopConfig(cycles=cycles, n_hosts=hosts, seed=seed)

    def watcher(label: str) -> Callable[[int, int], None] | None:
        if progress is None:
            return None
        return lambda done, total: progress(label, done, total)

    results = []
    for name in models:
        results.append(
            run_loop(
                REFERENCE_MODELS[name](),
                scenario,
                picked,
                config=config,
                on_cycle=watcher(name),
            )
        )
    if slow_s > 0.0:
        slow = LoopConfig(cycles=cycles, n_hosts=hosts, seed=seed, extra_latency_s=slow_s)
        result = run_loop(
            REFERENCE_MODELS[models[0]](),
            scenario,
            picked,
            config=slow,
            on_cycle=watcher(f"{models[0]} +{slow_s:g}s"),
        )
        result.model = f"{result.model} +{slow_s:g}s latency"
        results.append(result)
    return results


def verdicts(results: Sequence[LoopResult]) -> list[dict[str, Any]]:
    """Score the pair. Reported even when it fails, especially when it fails."""
    if len(results) < 2:
        return []
    herding, calm = results[0], results[1]

    def ratio(a: float, b: float) -> float:
        return a / b if b > 0 else float("inf")

    swing = ratio(herding.swing(), calm.swing())
    share = ratio(herding.share_swing(), calm.share_swing())
    cost = ratio(herding.cost(), calm.cost())
    old = herding.oscillation()
    out = [
        {
            "criterion": f"greedy link amplitude >= {MIN_SWING_RATIO:g}x stochastic",
            "measured": f"{swing:.2f}x  ({herding.swing():.3f} vs {calm.swing():.3f})",
            "ok": swing >= MIN_SWING_RATIO,
        },
        {
            "criterion": f"greedy split amplitude >= {MIN_SWING_RATIO:g}x stochastic",
            "measured": f"{share:.2f}x  ({herding.share_swing():.3f} vs {calm.share_swing():.3f})",
            "ok": share >= MIN_SWING_RATIO,
        },
        {
            "criterion": f"greedy mean path cost >= {MIN_COST_RATIO:g}x stochastic",
            "measured": f"{cost:.2f}x  ({herding.cost():.0f} ms vs {calm.cost():.0f} ms)",
            "ok": cost >= MIN_COST_RATIO,
        },
        {
            "criterion": "M3 as originally written: peak dominance > 0.5 (see ADR 0010)",
            "measured": f"{old:.3f}",
            "ok": old > 0.5,
        },
        {
            "criterion": f"samples sit on the decision grid (< {MAX_OVERRUN_FRACTION:.0%} late)",
            "measured": ", ".join(
                f"{r.overruns}/{r.config.cycles} at {r.cadence_s:g}s" for r in results
            ),
            "ok": all(r.overruns <= MAX_OVERRUN_FRACTION * r.config.cycles for r in results),
        },
    ]
    if len(results) > 2:
        slow = results[2]
        out.append(
            {
                "criterion": "a slower model is measurably worse, same model and seed",
                "measured": f"{slow.cost():.0f} ms vs {herding.cost():.0f} ms "
                f"at {slow.latency_s and sum(slow.latency_s) / len(slow.latency_s):.2f} s "
                f"decision latency",
                "ok": slow.cost() > herding.cost(),
            }
        )
    return out


def sections(results: Sequence[LoopResult]) -> list[dict[str, Any]]:
    """The figure: the same link, and the same scope, under each model."""
    link = _shared_link(results)
    scope = _shared_scope(results)
    sections: list[dict[str, Any]] = []
    if link is not None:
        sections.append(
            {
                "title": f"Offered load on interface {link}, from the advised populations",
                "note": "One line per model on <em>the same interface of the same "
                "topology</em>, sampled once per decision round. Above 1.0 the "
                "populations are asking for more than the link has. Background "
                "traffic is excluded here so that what is left is what the model "
                "caused.",
                "series": {r.model: r.advised_load.get(link, []) for r in results},
                "y_label": "offered / capacity",
            }
        )
        sections.append(
            {
                "title": f"Total utilisation of interface {link}, background included",
                "note": "What an operator's own graph would show.",
                "series": {r.model: r.utilisation.get(link, []) for r in results},
                "y_label": "utilisation",
                "y_max": 1.0,
            }
        )
    if scope is not None:
        sections.append(
            {
                "title": f"Realised share of one path, {scope[0]} to {scope[1]}",
                "note": "Where the hosts actually went, not where they were told to "
                "go. A flat line is a population that stayed put; a square wave "
                "is a population being thrown back and forth.",
                "series": {r.model: r.path_share.get(scope, []) for r in results},
                "y_label": "share of scope",
                "y_max": 1.0,
            }
        )
    sections.append(
        {
            "title": "Mean path cost, load-weighted over every scope",
            "note": "What the models are nominally optimising. Oscillation is not "
            "an aesthetic complaint: it shows up here.",
            "series": {r.model: r.mean_cost_ms for r in results},
            "y_label": "ms",
        }
    )
    return sections


def _shared_link(results: Sequence[LoopResult]) -> int | None:
    """The interface the first model shook hardest, if every run tracked it."""
    common = set(results[0].advised_load)
    for other in results[1:]:
        common &= set(other.advised_load)
    if not common:
        return None
    by_link = results[0].swing_by_link()
    return max(common, key=lambda i: by_link.get(i, 0.0))


def _shared_scope(results: Sequence[LoopResult]) -> tuple[str, str] | None:
    common = set(results[0].path_share)
    for other in results[1:]:
        common &= set(other.path_share)
    if not common:
        return None
    from scionarena.instrument.detectors import fast_swing

    return max(common, key=lambda s: fast_swing(results[0].path_share[s]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scionarena demo",
        description="Run two reference models over one scenario and render what they did.",
    )
    parser.add_argument("--tier", default="smoke", choices=["smoke", "dev", "realistic", "stress"])
    parser.add_argument("--scopes", type=int, default=8)
    parser.add_argument("--cycles", type=int, default=240)
    parser.add_argument("--hosts", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--slow", type=float, default=0.0, metavar="SECONDS")
    parser.add_argument("--models", default="minrtt,reference")
    parser.add_argument("--out", default="demo", metavar="DIR")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    results = run_demo(
        tier=args.tier,
        scopes=args.scopes,
        cycles=args.cycles,
        hosts=args.hosts,
        seed=args.seed,
        slow_s=args.slow,
        models=tuple(m.strip() for m in args.models.split(",") if m.strip()),
    )
    rows = [r.report() for r in results]
    checks = verdicts(results)
    meta = {
        "tier": args.tier,
        "scopes": args.scopes,
        "cycles": args.cycles,
        "hosts_per_scope": args.hosts,
        "seed": args.seed,
        "wall_clock_s": round(time.perf_counter() - started, 2),
        "digests": {r.model: r.session_summary.get("digest", "") for r in results},
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    page = render_html(
        "scionarena M3 — the closed loop",
        f"{args.tier} tier, {args.scopes} concurrent scopes, {args.cycles} decision rounds, "
        f"seed {args.seed}. Same world, same scopes, same seed; only the model differs.",
        sections(results),
        rows,
        verdicts=checks,
        meta=meta,
    )
    (out / "report.html").write_text(page, encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps({"meta": meta, "runs": rows, "verdicts": checks}, indent=2),
        encoding="utf-8",
    )

    print(report_card(rows, columns=CARD_COLUMNS))
    print()
    for check in checks:
        print(f"[{'PASS' if check['ok'] else 'FAIL'}] {check['criterion']}: {check['measured']}")
    print(f"\nwrote {out / 'report.html'} in {meta['wall_clock_s']}s")
    return 0 if all(c["ok"] for c in checks[:3]) else 1


if __name__ == "__main__":
    sys.exit(main())
