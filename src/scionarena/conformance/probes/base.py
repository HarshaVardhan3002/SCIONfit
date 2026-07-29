"""Probe results and the probe base class.

A probe is a controlled experiment on a model.  It changes exactly one thing,
observes what the model does, and reports.  Probes never compare two models
against each other; they only compare one model against itself under a
controlled change, which is what makes results portable across model families.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):  # noqa: UP042  StrEnum would change str(Status.PASS)
    PASS = "PASS"
    FAIL = "FAIL"
    WEAK = "WEAK"                      # satisfied, but marginally
    DECLARED_ABSENT = "DECLARED_ABSENT"  # model honestly says it lacks this
    FALSE_CLAIM = "FALSE_CLAIM"        # model claimed it, behaviour says no
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"                    # model raised

    @property
    def is_blocking(self) -> bool:
        """Blocking statuses stop a model being deployed as-is."""
        return self in (Status.FAIL, Status.FALSE_CLAIM, Status.ERROR)


@dataclass
class ProbeResult:
    probe_id: str
    requirement: str
    title: str
    status: Status
    finding: str
    evidence: dict[str, Any] = field(default_factory=dict)
    score: float | None = None          # [0,1] where meaningful
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "requirement": self.requirement,
            "title": self.title,
            "status": self.status.value,
            "finding": self.finding,
            "evidence": self.evidence,
            "score": self.score,
            "remedy": self.remedy,
        }


class Probe:
    """Base class.  Subclasses implement ``run``."""

    probe_id: str = "R00"
    requirement: str = ""
    title: str = ""
    #: capability flag this probe verifies; None means always applicable
    capability: str | None = None
    remedy: str = ""

    def run(self, model, world, rng) -> ProbeResult:  # pragma: no cover
        raise NotImplementedError

    # ---------------- helpers for subclasses ----------------

    def result(self, status: Status, finding: str, **evidence) -> ProbeResult:
        score = evidence.pop("score", None)
        return ProbeResult(
            probe_id=self.probe_id, requirement=self.requirement,
            title=self.title, status=status, finding=finding,
            evidence=evidence, score=score, remedy=self.remedy,
        )

    def declared_absent(self, flag: str) -> ProbeResult:
        return self.result(
            Status.DECLARED_ABSENT,
            f"Model declares {flag}=False. Not a defect; recorded so the "
            f"closed-loop tier knows what to expect.",
        )

    def errored(self, exc: BaseException) -> ProbeResult:
        return self.result(
            Status.ERROR,
            f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=4),
        )

    def execute(self, model, world, rng) -> ProbeResult:
        """Run with capability gating and exception capture.

        The gating rule matters and is worth stating plainly:

        * capability not claimed, probe fails  -> DECLARED_ABSENT.
          An honest limitation. Recorded, not punished.
        * capability not claimed, probe passes -> PASS, with a note suggesting
          the author update their declaration.
        * capability claimed, probe fails      -> FALSE_CLAIM.
          This is the only outcome treated as worse than a plain failure,
          because a wrong declaration silently corrupts every downstream
          comparison that trusts it.
        """
        claimed = (self.capability is not None
                   and getattr(model.capabilities, self.capability, False))

        try:
            res = self.run(model, world, rng)
        except Exception as exc:
            if self.capability is not None and not claimed:
                out = self.declared_absent(self.capability)
                out.evidence = {"raised": f"{type(exc).__name__}: {exc}"}
                return out
            return self.errored(exc)

        if self.capability is None:
            return res

        if claimed:
            if res.status is Status.FAIL:
                res.status = Status.FALSE_CLAIM
                res.finding = (f"Model declares {self.capability}=True but "
                               f"behaviour says otherwise. ") + res.finding
            return res

        if res.status in (Status.FAIL, Status.WEAK):
            out = self.declared_absent(self.capability)
            out.evidence = res.evidence
            out.score = 0.0
            return out
        if res.status is Status.PASS:
            res.finding = (f"Satisfied although {self.capability} was not "
                           f"declared. Consider updating the declaration. ") + res.finding
        return res
