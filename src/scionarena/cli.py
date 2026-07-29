"""Umbrella command line: ``scionarena <front-end> ...``

Each front-end owns its own parser and is dispatched to with the remaining
argv untouched, so ``scionarena conformance check reference`` and
``python -m scionarena.conformance.cli check reference`` are the same command.
Front-ends are registered here as they land; only ``conformance`` exists.
"""

from __future__ import annotations

import sys

FRONTENDS = {
    "conformance": "can this model represent what deployment requires?",
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

    from .conformance.cli import main as conformance_main

    return conformance_main(rest)


if __name__ == "__main__":
    sys.exit(main())
