"""Umbrella command line: ``scionarena <front-end> ...``

Each front-end owns its own parser and is dispatched to with the remaining
argv untouched, so ``scionarena conformance check reference`` and
``python -m scionarena.conformance.cli check reference`` are the same command.
Front-ends are registered here as they land; ``conformance`` and ``bench``
exist.
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
    "bench": "score a model across every axis, against the mandatory baselines",
    "demo": "run two models over one scenario and render what they did",
    "ui": "the same, from a browser, with the knobs exposed",
    "cockpit": "watch a sweep while it happens: the lens, the registries, the bad day",
    "models": "load a model and print what it declares, without running anything",
    "adapt": "call a model against synthetic input and say what would break, in seconds",
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

    if frontend == "adapt":
        return adapt_main(rest)

    if frontend == "bench":
        from .bench.cli import main as bench_main

        return bench_main(rest)

    if frontend == "demo":
        from .demo import main as demo_main

        return demo_main(rest)

    if frontend == "ui":
        from .ui import main as ui_main

        return ui_main(rest)

    if frontend == "cockpit":
        from .cockpit.app import main as cockpit_main

        return cockpit_main(rest)

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


def adapt_main(argv: list[str]) -> int:
    """``scionarena adapt <spec>`` -- the thirty-second check (ADR 0020).

    ``models`` says whether it loads and what it claims. This actually calls it:
    reset, observe, predict, advise, against a topology made of five dataclasses
    and no substrate at all. It finds a transposed array, a mapping returned as
    a list, or weights keyed on something that is not a path id, in the second
    after you wrote them rather than in cell one of ninety.

    Exit codes are the point of the command: 0 when nothing would break, 1 when
    something would. It is deliberately not a verdict on the model -- an
    unchecked declaration is neither a pass nor a failure, and the count of them
    prints beside the rest so the summary cannot be read as a score.
    """
    from .exposure.precheck import CONTRADICTED, precheck

    parser = argparse.ArgumentParser(
        prog="scionarena adapt",
        description=(
            "Put a model through the contract against synthetic input. Seconds, "
            "no substrate, no verdict -- run 'scionfit check' for the check that decides."
        ),
        epilog="a spec is a built-in name, 'my.module:MyModel', or './my_adaptor.py:MyAdaptor'",
    )
    parser.add_argument("spec")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="constructor argument, repeatable; values are parsed as JSON, then as text",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail on a declaration the behaviour contradicted, not only on "
        "something that would stop a cell running",
    )
    args = parser.parse_args(argv)

    kwargs: dict[str, object] = {}
    for item in args.arg:
        name, _, raw = item.partition("=")
        try:
            kwargs[name] = json.loads(raw)
        except json.JSONDecodeError:
            kwargs[name] = raw

    report = precheck(args.spec, kwargs)
    if args.json:
        print(
            json.dumps(
                {
                    "spec": report.spec,
                    "name": report.name,
                    "architecture": report.architecture,
                    "load_error": report.load_error,
                    "checks": [
                        {"name": c.name, "state": c.state, "detail": c.detail}
                        for c in report.checks
                    ],
                },
                indent=2,
            )
        )
    else:
        for line in report.lines():
            print(line)

    if report.load_error or report.blocking:
        return 1
    if args.strict and report.by_state(CONTRADICTED):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
