"""Command line interface: ``scionarena conformance check ...``"""

from __future__ import annotations

import argparse
import importlib
import sys

from ..reference import REFERENCE_MODELS
from .runner import check, comparison_table


def _load(spec: str):
    """Load a model from ``module:Attr`` or from the reference set by name."""
    if spec in REFERENCE_MODELS:
        return REFERENCE_MODELS[spec]()
    if ":" not in spec:
        raise SystemExit(
            f"unknown model {spec!r}. Use one of {sorted(REFERENCE_MODELS)} "
            f"or 'package.module:ClassName'."
        )
    mod_name, attr = spec.split(":", 1)
    obj = getattr(importlib.import_module(mod_name), attr)
    return obj() if isinstance(obj, type) else obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scionarena conformance",
        description="Conformance and fit checking for SCION path-selection models.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run the conformance suite on one model")
    c.add_argument(
        "model", help="'ema' | 'minrtt' | 'proportional' | 'reference' | 'my.module:MyModel'"
    )
    c.add_argument("--seed", type=int, default=0)
    c.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="run each probe against N seeds and keep the worst outcome",
    )
    c.add_argument("--paths", type=int, default=6)
    c.add_argument(
        "--drift", type=float, default=0.0, help="exogenous condition drift, 0 = static world"
    )
    c.add_argument("--format", choices=["terminal", "json", "markdown"], default="terminal")
    c.add_argument("--out", default=None, help="write to a file instead of stdout")
    c.add_argument("--no-colour", action="store_true")
    c.add_argument(
        "--strict", action="store_true", help="exit non-zero unless the verdict is CONFORMANT"
    )

    m = sub.add_parser("compare", help="run every reference model and print a matrix")
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--repeats", type=int, default=3)

    sub.add_parser("list", help="list built-in reference models")

    a = ap.parse_args(argv)

    if a.cmd == "list":
        for k, v in REFERENCE_MODELS.items():
            caps = v().capabilities
            print(f"  {k:<14} {caps.name:<22} {caps.notes[:60]}")
        return 0

    if a.cmd == "compare":
        cards = {k: check(v(), seed=a.seed, repeats=a.repeats) for k, v in REFERENCE_MODELS.items()}
        print(comparison_table(cards))
        return 0

    card = check(_load(a.model), seed=a.seed, repeats=a.repeats, n_paths=a.paths, drift=a.drift)
    text = {
        "terminal": lambda: card.to_terminal(colour=not a.no_colour),
        "json": card.to_json,
        "markdown": card.to_markdown,
    }[a.format]()
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {a.out}")
    else:
        print(text)

    if a.strict and card.verdict != "CONFORMANT":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
