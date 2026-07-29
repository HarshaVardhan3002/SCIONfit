"""Report card rendering: terminal, JSON, markdown."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..exposure.contracts import Capabilities
from .probes.base import ProbeResult, Status

GLYPH = {
    Status.PASS: "PASS",
    Status.WEAK: "WEAK",
    Status.FAIL: "FAIL",
    Status.DECLARED_ABSENT: "ABSENT",
    Status.FALSE_CLAIM: "FALSE CLAIM",
    Status.NOT_APPLICABLE: "N/A",
    Status.ERROR: "ERROR",
}
ANSI = {
    Status.PASS: "\033[32m",
    Status.WEAK: "\033[33m",
    Status.FAIL: "\033[31m",
    Status.DECLARED_ABSENT: "\033[90m",
    Status.FALSE_CLAIM: "\033[35m",
    Status.NOT_APPLICABLE: "\033[90m",
    Status.ERROR: "\033[31m",
}
RESET = "\033[0m"


@dataclass
class ReportCard:
    capabilities: Capabilities
    results: list[ProbeResult] = field(default_factory=list)
    world_seed: int = 0
    scionfit_version: str = "0.1.0"
    created: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    # ---------------- verdicts ----------------

    @property
    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self.results:
            c[r.status.value] = c.get(r.status.value, 0) + 1
        return c

    @property
    def blocking(self) -> list[ProbeResult]:
        return [r for r in self.results if r.status.is_blocking]

    @property
    def false_claims(self) -> list[ProbeResult]:
        return [r for r in self.results if r.status is Status.FALSE_CLAIM]

    @property
    def closed_loop_ready(self) -> bool:
        """Whether this model can be put in the multi-agent tier without the
        result being a foregone conclusion."""
        need = {"R6", "R8", "R9"}
        got = {
            r.probe_id
            for r in self.results
            if r.probe_id in need and r.status in (Status.PASS, Status.WEAK)
        }
        return got == need

    @property
    def absent(self) -> list[ProbeResult]:
        return [r for r in self.results if r.status is Status.DECLARED_ABSENT]

    @property
    def verdict(self) -> str:
        """CONFORMANT means every requirement is *met*, not merely met-or-
        honestly-declined.  An honest DECLARED_ABSENT is not a defect, but it
        is also not conformance: a model that cannot condition on demand is
        still a model that cannot be deployed in a loop."""
        if self.false_claims:
            return "MISDECLARED"
        if any(r.status is Status.ERROR for r in self.results):
            return "ERRORED"
        if not self.closed_loop_ready:
            return "OPEN-LOOP ONLY"
        if self.blocking or self.absent:
            return "PARTIAL"
        return "CONFORMANT"

    @property
    def score(self) -> float:
        vals = [
            r.score
            for r in self.results
            if r.score is not None and r.status is not Status.NOT_APPLICABLE
        ]
        return sum(vals) / len(vals) if vals else 0.0

    # ---------------- renderers ----------------

    def to_terminal(self, colour: bool = True) -> str:
        w = 78
        L: list[str] = []
        c = self.capabilities
        L.append("=" * w)
        L.append(f" scionfit conformance report  ::  {c.name} v{c.version}")
        L.append("=" * w)
        L.append("")
        for r in self.results:
            g = GLYPH[r.status]
            tag = f"{ANSI[r.status]}{g:<12}{RESET}" if colour else f"{g:<12}"
            sc = f"{r.score:.2f}" if r.score is not None else "  - "
            L.append(f" {r.probe_id:<4} {tag} {sc}  {r.title}")
            for line in _wrap(r.finding, w - 22):
                L.append(f"                          {line}")
            if r.status.is_blocking and r.remedy:
                for line in _wrap("remedy: " + r.remedy, w - 22):
                    L.append(f"                          {line}")
            L.append("")
        L.append("-" * w)
        cts = "  ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
        L.append(f" {cts}")
        L.append(f" mean score        {self.score:.2f}")
        L.append(f" closed-loop ready {'yes' if self.closed_loop_ready else 'no'}")
        L.append(f" VERDICT           {self.verdict}")
        L.append("=" * w)
        if self.verdict == "OPEN-LOOP ONLY":
            L.append(" This model can be benchmarked on prediction accuracy but not")
            L.append(" meaningfully on stability: it cannot represent the effect of")
            L.append(" its own advice, so the closed-loop tier would only confirm that.")
        if self.false_claims:
            L.append(" Declared capabilities contradicted by behaviour:")
            for r in self.false_claims:
                L.append(f"   {r.probe_id}  {r.title}")
        return "\n".join(L)

    def to_dict(self) -> dict:
        return {
            "scionfit_version": self.scionfit_version,
            "created": self.created,
            "world_seed": self.world_seed,
            "model": {
                "name": self.capabilities.name,
                "version": self.capabilities.version,
                "authors": self.capabilities.authors,
                "notes": self.capabilities.notes,
                "declared": {
                    k: v for k, v in vars(self.capabilities).items() if isinstance(v, bool)
                },
            },
            "results": [r.as_dict() for r in self.results],
            "summary": {
                "counts": self.counts,
                "mean_score": round(self.score, 4),
                "closed_loop_ready": self.closed_loop_ready,
                "verdict": self.verdict,
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_markdown(self) -> str:
        c = self.capabilities
        L = [
            f"# scionfit report: {c.name} v{c.version}",
            "",
            f"**Verdict: {self.verdict}**  ·  mean score {self.score:.2f}  ·  "
            f"closed-loop ready: {'yes' if self.closed_loop_ready else 'no'}",
            "",
            f"_{self.created}, world seed {self.world_seed}, scionfit {self.scionfit_version}_",
            "",
            "| Probe | Requirement | Status | Score | Finding |",
            "|---|---|---|---|---|",
        ]
        for r in self.results:
            f = r.finding.replace("|", "\\|")
            sc = f"{r.score:.2f}" if r.score is not None else "–"
            L.append(f"| {r.probe_id} | {r.requirement} | **{GLYPH[r.status]}** | {sc} | {f} |")
        blocking = self.blocking
        if blocking:
            L += ["", "## What has to change", ""]
            for r in blocking:
                L.append(f"- **{r.probe_id} {r.title}** — {r.remedy or r.finding}")
        return "\n".join(L)


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out
