"""Run the probe suite against a model."""

from __future__ import annotations

import random
from collections.abc import Sequence

from ..backends.analytical import World
from ..exposure.contracts import PathModel
from .probes.base import Probe, ProbeResult, Status
from .probes.conformance import ALL_PROBES
from .report import ReportCard

__all__ = ["check", "check_many"]


def check(
    model: PathModel,
    *,
    seed: int = 0,
    probes: Sequence[Probe] | None = None,
    n_paths: int = 6,
    n_ases: int = 8,
    drift: float = 0.0,
    repeats: int = 1,
) -> ReportCard:
    """Run the conformance suite.

    Each probe gets a *fresh* world built from the same seed, so probes cannot
    contaminate one another and a report is reproducible from the seed alone.

    ``repeats`` > 1 runs every probe against several seeds and keeps the worst
    outcome per probe, which is the honest summary for a stochastic model.
    """
    probes = list(probes if probes is not None else ALL_PROBES)
    order = [Status.PASS, Status.NOT_APPLICABLE, Status.DECLARED_ABSENT,
             Status.WEAK, Status.FAIL, Status.FALSE_CLAIM, Status.ERROR]
    rank = {s: i for i, s in enumerate(order)}

    worst: dict[str, ProbeResult] = {}
    for rep in range(max(1, repeats)):
        s = seed + rep * 1009
        for p in probes:
            world = World(n_ases=n_ases, n_paths=n_paths, seed=s, drift=drift)
            rng = random.Random(s ^ (hash(p.probe_id) & 0xFFFF))
            res = p.execute(model, world, rng)
            prev = worst.get(p.probe_id)
            if prev is None or rank[res.status] > rank[prev.status]:
                worst[p.probe_id] = res

    results = [worst[p.probe_id] for p in probes if p.probe_id in worst]
    return ReportCard(capabilities=model.capabilities, results=results, world_seed=seed)


def check_many(models: dict[str, PathModel], **kw) -> dict[str, ReportCard]:
    return {name: check(m, **kw) for name, m in models.items()}


def comparison_table(cards: dict[str, ReportCard]) -> str:
    """A compact cross-model matrix. Used to show the probes discriminate."""
    from .probes.base import Status as S
    glyph = {S.PASS: "PASS", S.WEAK: "weak", S.FAIL: "FAIL",
             S.DECLARED_ABSENT: "  - ", S.FALSE_CLAIM: "LIED", S.NOT_APPLICABLE: " n/a",
             S.ERROR: " ERR"}
    ids = [r.probe_id for r in next(iter(cards.values())).results]
    w = max(len(n) for n in cards) + 2
    lines = [" " * w + "".join(f"{i:>6}" for i in ids) + "   verdict"]
    lines.append("-" * (w + 6 * len(ids) + 20))
    for name, card in cards.items():
        by = {r.probe_id: r.status for r in card.results}
        row = "".join(f"{glyph.get(by.get(i), '?'):>6}" for i in ids)
        lines.append(f"{name:<{w}}{row}   {card.verdict}")
    return "\n".join(lines)
