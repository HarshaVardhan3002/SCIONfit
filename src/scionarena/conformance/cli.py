"""Command line interface: ``scionarena conformance check ...``"""

from __future__ import annotations

import argparse
import sys

from ..exposure.loading import BUILTIN_MODELS, ModelLoadError, capability_report, load_model
from .runner import check, comparison_table


def _load(spec: str):
    """One door for every model, ours included. See ADR 0013."""
    try:
        return load_model(spec)
    except ModelLoadError as exc:
        raise SystemExit(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scionarena conformance",
        description="Conformance and fit checking for SCION path-selection models.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run the conformance suite on one model")
    c.add_argument(
        "model",
        help=f"{' | '.join(sorted(BUILTIN_MODELS))} | 'my.module:MyModel' | './my_model.py:MyModel'",
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
    c.add_argument(
        "--no-capabilities",
        action="store_true",
        help="skip the declaration report printed before the run",
    )

    m = sub.add_parser("compare", help="run every reference model and print a matrix")
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--repeats", type=int, default=3)

    sub.add_parser("list", help="list built-in reference models")

    a = ap.parse_args(argv)

    if a.cmd == "list":
        for name, target in BUILTIN_MODELS.items():
            caps = _load(name).capabilities
            print(f"  {name:<14} {caps.name:<22} {target}")
            if caps.notes:
                print(f"  {'':<14} {caps.notes[:76]}")
        return 0

    if a.cmd == "compare":
        cards = {
            name: check(_load(name), seed=a.seed, repeats=a.repeats) for name in BUILTIN_MODELS
        }
        print(comparison_table(cards))
        return 0

    model = _load(a.model)
    if not a.no_capabilities and a.format == "terminal" and not a.out:
        # Before the run, not after it: the declaration is the part the author
        # controls, and DECLARED_ABSENT on six probes is a surprise worth having
        # while there is still time to fix the declaration.
        print(capability_report(model, a.model).to_terminal())
        print()
    card = check(model, seed=a.seed, repeats=a.repeats, n_paths=a.paths, drift=a.drift)
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
