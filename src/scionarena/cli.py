"""Umbrella command line: ``scionarena <front-end> ...``

Each front-end owns its own parser and is dispatched to with the remaining
argv untouched, so ``scionarena conformance check reference`` and
``python -m scionarena.conformance.cli check reference`` are the same command.
Front-ends are registered here as they land; only ``conformance`` exists.
``demo``, ``ui`` and ``models`` are not front-ends -- the first two are the M3
result, in one command and in a browser respectively, and the third answers
"will my model load, and what will it be tested on?" without running anything.
They live here because that is where a reader looks for them.
"""

from __future__ import annotations

import argparse
import json
import sys

FRONTENDS = {
    "conformance": "can this model represent what deployment requires?",
    "demo": "run two models over one scenario and render what they did",
    "ui": "the same, from a browser, with the knobs exposed",
    "models": "load a model and print what it declares, without running anything",
}


def _usage() -> str:
    lines = ["usage: scionarena <front-end> [args...]", "", "front-ends:"]
    lines += [f"  {name:<14} {help_}" for name, help_ in FRONTENDS.items()]
    lines += ["", "run 'scionarena <front-end> --help' for that front-end's options"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in {"-h", "--help"}:
        print(_usage())
        return 0 if args else 2

    frontend, rest = args[0], args[1:]
    if frontend not in FRONTENDS:
        print(f"unknown front-end {frontend!r}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    if frontend == "models":
        return models_main(rest)

    if frontend == "demo":
        from .demo import main as demo_main

        return demo_main(rest)

    if frontend == "ui":
        from .ui import main as ui_main

        return ui_main(rest)

    from .conformance.cli import main as conformance_main

    return conformance_main(rest)


def models_main(argv: list[str]) -> int:
    """``scionarena models [spec]`` -- does it load, and what does it claim?

    The fastest check that an integration is right: no scenario is built and no
    round is run, so a wrong import path or a missing method costs a second
    rather than the length of a run. See ADR 0013.
    """
    from .exposure.loading import BUILTIN_MODELS, ModelLoadError, capability_report, load_model

    parser = argparse.ArgumentParser(
        prog="scionarena models",
        description="Load a model by spec and report what it declares about itself.",
        epilog="a spec is a built-in name, 'my.module:MyModel', or './my_model.py:MyModel'",
    )
    parser.add_argument("spec", nargs="?", help="omit to list the built-in models")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.spec is None:
        if args.json:
            print(json.dumps(dict(BUILTIN_MODELS), indent=2))
            return 0
        for name, target in BUILTIN_MODELS.items():
            print(f"  {name:<14} {target}")
        print()
        print("any import path works too. See docs/MODELS.md.")
        return 0

    try:
        model = load_model(args.spec)
    except ModelLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = capability_report(model, args.spec)
    print(report.to_json() if args.json else report.to_terminal())
    return 0


if __name__ == "__main__":
    sys.exit(main())
