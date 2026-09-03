"""Loading a model the harness did not write.

A model is named by an import path -- ``mypackage.mymodule:MyModel`` -- and the
built-in names are aliases for import paths rather than a second lookup, so there
is one resolution rule for everybody. See ADR 0013.

This lives in ``exposure`` because it is the only layer every front-end may
import: ``conformance``, ``bench`` and the UI all need it and none of them may
import another. It reaches nothing in ``core``.

Two jobs, and they are separate on purpose:

``load_model``
    Turn a string into an instance, and fail with a sentence when it cannot.
    Every failure names the spec, what went wrong, and what to do about it.

``capability_report``
    Say what the model will be tested on, from its own declaration, *before*
    anything runs. The declaration is the part the author controls, so it is
    the part to show them while they can still change it.

Loading executes the named module in this process. That is what ``import`` means
and no error message changes it: a spec from an untrusted source is not safe to
load.
"""

from __future__ import annotations

import dataclasses
import difflib
import importlib
import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from .contracts import Capabilities, PathModel

__all__ = [
    "BUILTIN_MODELS",
    "CAPABILITY_NOTES",
    "REQUIRED_METHODS",
    "CapabilityLine",
    "CapabilityReport",
    "ModelLoadError",
    "capability_report",
    "load_model",
    "resolve",
]

#: The models we ship, as aliases for import paths. ``prober`` is the only
#: tool-using one, and it used to be absent from here because it needed the
#: scopes it would advise at construction time -- which a bench user cannot
#: know. It asks the session instead (ADR 0019), so it loads from a bare name
#: like everything else and the agentic path is reachable from a sweep.
BUILTIN_MODELS: Final[Mapping[str, str]] = {
    "ema": "scionarena.reference.models:EMAOracle",
    "minrtt": "scionarena.reference.models:MinRTTGreedy",
    "proportional": "scionarena.reference.models:CapacityProportional",
    "reference": "scionarena.reference.models:ReferenceStochastic",
    "prober": "scionarena.reference.agents:BudgetedProber",
    "gbdt": "scionarena.reference.trees:GradientBoosted",
    "layered": "scionarena.reference.layered:LayeredRanker",
}

#: What may follow the colon: a name, or a dotted path to a nested class.
_ATTRIBUTE: Final = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")

#: The four methods ``PathModel`` requires, in lifecycle order.
REQUIRED_METHODS: Final = ("reset", "observe", "predict", "advise")

#: Every boolean a model may declare about itself, the probe that verifies it,
#: and what it claims. An empty probe id means no probe gates it today -- the
#: flag is read by the closed loop or by the report instead.
#:
#: This table is static because ``exposure`` may not import ``conformance`` to
#: ask the probe registry directly. ``tests/test_loading.py`` fails if the two
#: drift apart.
CAPABILITY_NOTES: Final[Mapping[str, tuple[str, str]]] = {
    "composes_unseen_paths": (
        "R2",
        "scores a path built from interfaces it has seen, never seen combined",
    ),
    "handles_unseen_interfaces": (
        "R4",
        "keeps working when an interface appears that was absent at reset",
    ),
    "distributional": ("R5", "emits a distribution, not a point estimate"),
    "demand_conditioned": ("R6", "changes its prediction when offered demand changes"),
    "monotone_in_demand": ("R7", "predicted cost does not fall when demand on a path rises"),
    "emits_assignment": ("R8", "publishes a distribution over paths, not a ranking"),
    "self_consistent": ("R9", "the load its own advice implies matches what it predicted"),
    "staleness_aware": ("R10", "widens or backs off as its information ages"),
    "reports_confidence": ("", "fills in Prediction.confidence; reported, not gated"),
    "stateful": ("", "carries state between rounds, and reset() clears it"),
    "uses_tools": ("", "implements act() and drives itself through the tool registry"),
    "manages_own_memory": ("", "decides what to keep from the raw log; M8 tests this"),
    "respects_deadline": ("", "returns inside its decision slot; checked against the session log"),
}

#: Under ``from __future__ import annotations`` a field's type is the string.
_FLAGS: Final = tuple(
    field.name for field in dataclasses.fields(Capabilities) if field.type in ("bool", bool)
)


class ModelLoadError(Exception):
    """A model could not be loaded, and this says what to do next.

    Raised in place of whatever the interpreter happened to throw. A missing
    package, a mistyped class name, a constructor wanting arguments and a model
    missing ``advise`` are four different problems with four different fixes,
    and ``ModuleNotFoundError`` is the right message for none of them.
    """

    def __init__(self, spec: str, problem: str, hint: str = "") -> None:
        self.spec, self.problem, self.hint = spec, problem, hint
        message = f"cannot load model {spec!r}: {problem}"
        if hint:
            message += f"\n  {hint}"
        super().__init__(message)


# --------------------------------------------------------------------------
# loading


def resolve(spec: str, *, aliases: Mapping[str, str] = BUILTIN_MODELS) -> str:
    """Turn whatever the user typed into a ``module:attribute`` string."""
    name = spec.strip()
    if not name:
        raise ModelLoadError(spec, "empty model name", _alias_hint(aliases))
    if name in aliases:
        return aliases[name]
    if ":" not in name:
        close = difflib.get_close_matches(name, list(aliases), n=1)
        did_you_mean = f"did you mean {close[0]!r}? " if close else ""
        raise ModelLoadError(
            spec,
            "not a built-in name and not an import path",
            f"{did_you_mean}{_alias_hint(aliases)}",
        )
    return name


def load_model(
    spec: str,
    *,
    aliases: Mapping[str, str] = BUILTIN_MODELS,
    args: Mapping[str, Any] | None = None,
) -> PathModel:
    """Import ``spec``, instantiate it, and check it against ``PathModel``.

    ``args`` reaches the constructor, which is how a model needing configuration
    is loaded without writing a factory. The conformance check is structural
    rather than ``isinstance(obj, PathModel)``: the protocol answers with one
    bool, and a bool cannot say which method is missing.
    """
    target = resolve(spec, aliases=aliases)
    module_name, attribute = _split(spec, target)

    module = _import(spec, module_name)
    obj = _attribute(spec, module, module_name, attribute)
    model = _instantiate(spec, obj, dict(args or {}))
    _check_conformance(spec, target, model)
    return cast("PathModel", model)


def _split(spec: str, target: str) -> tuple[str, str]:
    """``module:attribute``, splitting at the *last* colon.

    Splitting at the first one is wrong on Windows and only on Windows, where an
    absolute path begins ``C:``: ``C:/models/mine.py:MyModel`` became module
    ``C``, and the error said the package ``C`` was not installed. An attribute
    cannot contain a colon, so the last one is always the separator.
    """
    module_name, _, attribute = target.rpartition(":")
    if not module_name or not _ATTRIBUTE.fullmatch(attribute):
        raise ModelLoadError(
            spec,
            f"{target!r} is not of the form module:attribute",
            "write it as 'mypackage.mymodule:MyModel' or './my_model.py:MyModel' -- "
            "the part after the last colon is the name of the class",
        )
    return module_name, attribute


def _import(spec: str, module_name: str) -> Any:
    if module_name.endswith(".py"):
        return _import_file(spec, module_name)
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or module_name
        hint = (
            f"install it into this environment first -- 'pip install -e .' from its "
            f"package root, or 'pip install {missing.split('.')[0]}'"
        )
        raise ModelLoadError(spec, f"no module named {missing!r}", hint) from exc
    except Exception as exc:  # importing it ran the module, and the module raised
        raise ModelLoadError(
            spec,
            f"importing {module_name!r} raised {type(exc).__name__}: {exc}",
            "the module fails on import, before the harness touches the model",
        ) from exc


def _import_file(spec: str, path_name: str) -> Any:
    """``./my_model.py:MyModel`` -- a single file, with nothing packaged.

    The lowest barrier there is: somebody with one script and no ``pyproject``
    can be measured without first learning how to publish a package. The file is
    executed exactly as an import executes it, with the same consequences.
    """
    path = Path(path_name).expanduser()
    if not path.is_file():
        raise ModelLoadError(
            spec,
            f"no file at {path}",
            "a spec ending in .py is read as a path, relative to where you ran this",
        )
    module_spec = importlib.util.spec_from_file_location(path.stem, path)
    if module_spec is None or module_spec.loader is None:
        raise ModelLoadError(spec, f"{path} cannot be imported as a module")
    module = importlib.util.module_from_spec(module_spec)
    # Registered before execution: a module that imports itself, or is pickled
    # from inside itself, needs to find itself under this name.
    sys.modules.setdefault(module_spec.name, module)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_spec.name, None)
        raise ModelLoadError(
            spec,
            f"running {path} raised {type(exc).__name__}: {exc}",
            "the file fails on execution, before the harness touches the model",
        ) from exc
    return module


def _attribute(spec: str, module: Any, module_name: str, attribute: str) -> Any:
    """``mod:Outer.Inner`` walks the dots, so a nested class is reachable."""
    obj: Any = module
    where = module_name
    for part in attribute.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            public = [name for name in dir(obj) if not name.startswith("_")]
            close = difflib.get_close_matches(part, public, n=3)
            hint = f"did you mean {', '.join(repr(c) for c in close)}?" if close else ""
            raise ModelLoadError(spec, f"{where} has no attribute {part!r}", hint) from exc
        where = f"{where}.{part}"
    return obj


def _instantiate(spec: str, obj: Any, args: dict[str, Any]) -> Any:
    """A class or factory is called; anything else is taken as already built."""
    if not callable(obj):
        if args:
            raise ModelLoadError(
                spec,
                f"{type(obj).__name__} is not callable, so {sorted(args)} cannot be passed",
                "point the spec at the class rather than at an instance of it",
            )
        return obj

    try:
        signature: inspect.Signature | None = inspect.signature(obj)
    except (TypeError, ValueError):  # builtins and C extensions have none
        signature = None

    if signature is not None:
        try:
            signature.bind(**args)
        except TypeError as exc:
            needed = [
                str(parameter)
                for parameter in signature.parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            ]
            hint = (
                f"it requires {', '.join(needed)} -- pass those, or point the spec at a "
                f"zero-argument factory that supplies them"
                if needed
                else ""
            )
            raise ModelLoadError(spec, f"cannot be constructed as given: {exc}", hint) from exc

    try:
        return obj(**args)
    except Exception as exc:
        raise ModelLoadError(
            spec,
            f"constructing it raised {type(exc).__name__}: {exc}",
            "the failure is inside the model's own __init__, not in the harness",
        ) from exc


def _check_conformance(spec: str, target: str, model: Any) -> None:
    problems = []

    missing = [name for name in REQUIRED_METHODS if not callable(getattr(model, name, None))]
    if missing:
        problems.append(
            f"missing or uncallable: {', '.join(missing)} "
            f"(PathModel needs {', '.join(REQUIRED_METHODS)})"
        )

    caps = getattr(model, "capabilities", None)
    if caps is None:
        problems.append(
            "no 'capabilities' attribute -- set one to a scionarena.exposure.contracts.Capabilities"
        )
    else:
        if not isinstance(getattr(caps, "name", None), str) or not caps.name:
            problems.append("capabilities.name is empty, and it is what the report calls the model")
        absent = [flag for flag in _FLAGS if not hasattr(caps, flag)]
        if absent:
            problems.append(
                f"capabilities is missing {', '.join(absent)} -- every flag must be present, "
                f"because an absent one reads as False and silently costs you probes"
            )

    if problems:
        raise ModelLoadError(spec, f"{target} does not implement PathModel", "; ".join(problems))


def _alias_hint(aliases: Mapping[str, str]) -> str:
    return (
        f"use one of {sorted(aliases)}, or an import path like 'mypackage.mymodule:MyModel' "
        f"(see docs/MODELS.md)"
    )


# --------------------------------------------------------------------------
# what the model says about itself


@dataclass(frozen=True)
class CapabilityLine:
    """One declaration, and what the harness will do about it."""

    flag: str
    declared: bool
    probe: str
    meaning: str

    @property
    def outcome(self) -> str:
        if not self.probe:
            return "reported" if self.declared else "not reported"
        return "tested" if self.declared else "DECLARED_ABSENT"


@dataclass(frozen=True)
class CapabilityReport:
    """What a loaded model will and will not be tested on, before it runs."""

    spec: str
    resolved: str
    name: str
    version: str
    authors: str
    notes: str
    lines: tuple[CapabilityLine, ...]

    @property
    def gated(self) -> tuple[CapabilityLine, ...]:
        return tuple(line for line in self.lines if line.probe)

    @property
    def tested(self) -> tuple[str, ...]:
        return tuple(line.probe for line in self.gated if line.declared)

    @property
    def absent(self) -> tuple[str, ...]:
        return tuple(line.probe for line in self.gated if not line.declared)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "resolved": self.resolved,
            "name": self.name,
            "version": self.version,
            "authors": self.authors,
            "notes": self.notes,
            "tested": list(self.tested),
            "declared_absent": list(self.absent),
            "capabilities": [
                {
                    "flag": line.flag,
                    "declared": line.declared,
                    "probe": line.probe,
                    "outcome": line.outcome,
                    "meaning": line.meaning,
                }
                for line in self.lines
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_terminal(self) -> str:
        head = [f"{self.spec}  ->  {self.resolved}", f"{self.name}  v{self.version}"]
        if self.authors:
            head.append(self.authors)
        rows = [f"  {'probe':<6} {'capability':<26} {'harness':<16} claim"]
        rows += [
            f"  {line.probe or '--':<6} {line.flag:<26} {line.outcome:<16} {line.meaning}"
            for line in self.lines
        ]
        tail = [
            f"{len(self.tested)} of {len(self.gated)} gated probes will run; "
            f"{len(self.absent)} will be recorded as DECLARED_ABSENT.",
            "An honest absence is not a defect. A declaration the behaviour contradicts is a",
            "FALSE_CLAIM, which scores worse -- so declare what the model does, not what it "
            "should do.",
        ]
        if self.notes:
            tail = [self.notes, ""] + tail
        return "\n".join(head + [""] + rows + [""] + tail)


def capability_report(
    model: PathModel, spec: str = "", *, aliases: Mapping[str, str] = BUILTIN_MODELS
) -> CapabilityReport:
    """What ``model`` says it does, and what the harness will do about it."""
    caps = model.capabilities
    lines = tuple(
        CapabilityLine(
            flag=flag,
            declared=bool(getattr(caps, flag, False)),
            probe=probe,
            meaning=meaning,
        )
        for flag, (probe, meaning) in CAPABILITY_NOTES.items()
    )
    resolved = ""
    if spec:
        try:
            resolved = resolve(spec, aliases=aliases)
        except ModelLoadError:  # a report on an instance nobody named by string
            resolved = ""
    return CapabilityReport(
        spec=spec or getattr(caps, "name", "model"),
        resolved=resolved or f"{type(model).__module__}:{type(model).__qualname__}",
        name=getattr(caps, "name", "") or type(model).__name__,
        version=getattr(caps, "version", "0.0.0"),
        authors=getattr(caps, "authors", ""),
        notes=getattr(caps, "notes", ""),
        lines=lines,
    )
