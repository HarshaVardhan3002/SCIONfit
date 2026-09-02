"""What a sweep writes down, and what a reader gets back.

One JSON file per cell, named by a digest of what produced it. Three properties
follow and they are the whole reason for the shape (ADR 0016): a sweep is
**resumable**, because a cell whose file exists is skipped; results are
**comparable**, because the suite digest is recorded and a suite that has been
edited does not silently reuse the results of the suite before the edit; and two
directories are **mergeable** by copying, because a cell's identity is a function
of the cell rather than of the run that produced it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.trace import TraceHash

__all__ = ["CellResult", "cell_id", "cell_seed", "load_results", "write_result"]

#: Bumped when the *shape* of a result file changes in a way a reader cannot
#: absorb. Adding a metric is not that; renaming ``axes`` would be.
SCHEMA = 1


def cell_id(suite: str, model: str, axes: Mapping[str, str], repeat: int) -> str:
    """A stable name for one cell, independent of when or where it ran."""
    return (
        TraceHash(label="cell")
        .update(
            {
                "suite": suite,
                "model": model,
                "axes": dict(axes),
                "repeat": int(repeat),
            }
        )
        .short(16)
    )


def cell_seed(suite: str, model: str, axes: Mapping[str, str], repeat: int) -> int:
    """The seed this cell's world is built from.

    Derived from the cell rather than from a counter. A counter makes a seed a
    function of execution order, and execution order under a process pool is not
    deterministic -- so a cell run on its own and the same cell run in the middle
    of a grid would draw different worlds and be reported as the same thing.
    """
    return int(cell_id(suite, model, axes, repeat), 16) % (2**31 - 1)


@dataclass
class CellResult:
    """One model, one cell, one repeat.

    ``error`` set means the cell ran and failed, which is a result: a report can
    say which cells are missing and why, and that is a finding about the model
    rather than a hole in the data. A cell that was never attempted has no file
    at all, and the two are deliberately different states.
    """

    cell_id: str
    suite: str
    suite_digest: str
    model: str
    label: str
    mandatory: bool
    axes: dict[str, str]
    repeat: int
    seed: int
    scenario: str
    #: The tier this cell ran at. Recorded rather than read off the suite when
    #: the report is built, because the suite object available then is the
    #: *current* one and printing its tier against an older cell produces a
    #: report that is internally consistent and false (ADR 0018).
    tier: str = ""
    #: What the model declared about itself, as booleans. Empty for a cell
    #: recorded before this existed, and a report prints that as "unrecorded"
    #: rather than as "declares nothing".
    capabilities: dict[str, Any] = field(default_factory=dict)
    #: What the substrate was when this ran. Recorded, not enforced: forcing a
    #: re-run on a mismatch would invalidate a week of stress-tier results that
    #: may be exactly what somebody wants to compare against.
    substrate_digest: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    wall_clock_s: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "cell_id": self.cell_id,
            "suite": self.suite,
            "suite_digest": self.suite_digest,
            "model": self.model,
            "label": self.label,
            "mandatory": self.mandatory,
            "axes": dict(self.axes),
            "repeat": self.repeat,
            "seed": self.seed,
            "scenario": self.scenario,
            "tier": self.tier,
            "capabilities": dict(self.capabilities),
            "substrate_digest": self.substrate_digest,
            "metrics": dict(self.metrics),
            "wall_clock_s": round(self.wall_clock_s, 4),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CellResult:
        known = {
            "cell_id",
            "suite",
            "suite_digest",
            "model",
            "label",
            "mandatory",
            "axes",
            "repeat",
            "seed",
            "scenario",
            "tier",
            "capabilities",
            "substrate_digest",
            "metrics",
            "wall_clock_s",
            "error",
        }
        return cls(**{k: v for k, v in data.items() if k in known})


def write_result(directory: Path, result: CellResult) -> Path:
    """Write one cell, atomically.

    Through a temporary file and a rename, because a sweep is interrupted by
    people rather than by the scheduler: a half-written file that parses is
    indistinguishable from a finished cell and would be resumed over.
    """
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{result.cell_id}.json"
    staging = directory / f".{result.cell_id}.partial"
    staging.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    staging.replace(final)
    return final


def load_results(directory: Path) -> Iterator[CellResult]:
    """Every cell in a directory, oldest name first. Unreadable files are skipped.

    A file that will not parse is skipped rather than raised on: the common
    cause is a sweep killed mid-write on a filesystem without atomic rename, and
    losing one cell is better than refusing to read the other four hundred.
    """
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("cell_id"):
            yield CellResult.from_dict(data)
