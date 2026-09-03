"""A reference model that drives itself.

``BudgetedProber`` exists for the same reason the other four do: so the agentic
half of the exposure layer is demonstrated by something that runs, and so the
things that can go wrong there have something to go wrong to.

It is not clever. It subscribes to telemetry, spends probes in a round robin
across border routers -- because SCMP is rate limited per router and rotating is
the only mitigation available -- backs off when it is told to, notices when it
has run out of budget, and keeps publishing on what it last knew rather than
going silent. The estimator underneath is ``ReferenceStochastic``.

Like every model, it imports :mod:`scionarena.exposure.contracts` and nothing
else. It cannot see the substrate, and import-linter proves it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..exposure.contracts import (
    SLA,
    Capabilities,
    Observation,
    SessionLike,
    TopologySnapshot,
)
from .models import ReferenceStochastic

__all__ = ["BudgetedProber", "TOOL_USING_MODELS"]


class BudgetedProber(ReferenceStochastic):
    """Query, probe within budget, publish. Handles every refusal it can get."""

    def __init__(
        self,
        scopes: Sequence[tuple[str, str]] = (),
        *,
        probes_per_turn: int = 2,
        sla: SLA | None = None,
        limit: int = 20,
        n_hosts: int = 100,
    ):
        """``scopes`` is optional, and leaving it out is the normal case.

        A model submitted to ``bench`` arrives as an import path and a few
        constructor arguments (ADR 0013), chosen by someone who has not seen the
        world it will run in -- so it cannot be handed the source-destination
        pairs it serves. It asks for them instead, once per episode, through
        ``list_scopes``. Passing them in stays supported because a test that
        wants one scope should not have to build a world to name it.
        """
        super().__init__()
        self.scopes = list(scopes)
        #: Whether the scope list was configured or has to be asked for. A
        #: model told its scopes still re-asks after a reset, because a scope
        #: can be added or drained mid-run.
        self._scopes_given = bool(scopes)
        self.probes_per_turn = probes_per_turn
        self.sla = sla or SLA()
        self.limit = limit
        self.n_hosts = n_hosts
        self.capabilities = Capabilities(
            name="BudgetedProber",
            architecture="stochastic",
            version="0.1.0",
            authors="scionarena reference",
            distributional=True,
            demand_conditioned=True,
            monotone_in_demand=True,
            emits_assignment=True,
            self_consistent=True,
            staleness_aware=True,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            reports_confidence=True,
            uses_tools=True,
            manages_own_memory=True,
            respects_deadline=True,
            notes="Round-robins probes across border routers, backs off on "
            "rate limits, keeps advising after the probe budget is gone.",
        )
        #: What it has been refused, and how often. Its own bookkeeping: the
        #: harness records this too, but a model that cannot notice being
        #: refused cannot respond to it.
        self.refusals: dict[str, int] = {}
        self._turn = 0
        self._handle: str | None = None
        self._blind = False

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        super().reset(topo, seed)
        self.refusals = {}
        self._turn = 0
        self._handle = None
        self._blind = False
        if not self._scopes_given:
            self.scopes = []

    # ------------------------------------------------------------------ agent

    def _note(self, result: Any) -> bool:
        if getattr(result, "ok", False):
            return True
        code = str(getattr(result, "error", "unknown"))
        self.refusals[code] = self.refusals.get(code, 0) + 1
        if code == "budget_exhausted":
            self._blind = True
        return False

    def act(self, session: SessionLike, deadline_s: float) -> None:
        end = session.now + deadline_s
        if self._handle is None:
            opened = session.call("subscribe", stream="telemetry")
            if self._note(opened):
                self._handle = opened.data["handle"]
        if not self.scopes:
            # Costs a query, and a refusal here leaves the turn with nothing to
            # advise on -- which is the honest outcome, not an excuse to guess.
            listed = session.call("list_scopes")
            if self._note(listed):
                self.scopes = [(s["src"], s["dst"]) for s in listed.data["scopes"]]

        self._ingest(session)

        for index, (src, dst) in enumerate(self.scopes):
            if session.now >= end:
                break  # late is permitted and recorded; pointless is not
            found = session.call("query_paths", src=src, dst=dst, limit=self.limit)
            self._note(found)
            paths = session.known_paths(src, dst)
            if not paths:
                continue

            if not self._blind:
                self._probe(session, paths, index)

            advisory = self.advise(session.view(), paths, self.sla, self.n_hosts)
            weights = {k: float(v) for k, v in advisory.normalised().items()}
            if weights:
                self._note(
                    session.call(
                        "publish_advisory",
                        src=src,
                        dst=dst,
                        weights=weights,
                        meta={
                            "reason": advisory.reason[:200],
                            "blind": self._blind,
                            "turn": self._turn,
                        },
                    )
                )
        self._turn += 1

    def _probe(self, session: SessionLike, paths: Sequence[Any], offset: int) -> None:
        """Spend probes, rotating so consecutive ones leave through different
        border routers. A rate limit keyed on the router is the one limit a
        model can route around, and this is how."""
        observations: list[Observation] = []
        stride = max(1, len(paths) // max(1, self.probes_per_turn))
        for k in range(self.probes_per_turn):
            target = paths[(self._turn * self.probes_per_turn + offset + k * stride) % len(paths)]
            result = session.call("probe_path", path_id=target.path_id, kind="latency")
            if not self._note(result):
                continue
            latency = result.data.get("latency_ms")
            if latency is not None:
                observations.append(
                    Observation(
                        t=session.now, path_id=target.path_id, latency_ms=latency, source="scmp"
                    )
                )
        if observations:
            self.observe(observations, session.view())

    def _ingest(self, session: SessionLike) -> None:
        """Drain the feed and keep what is useful. The records arrive raw and
        stay raw: what is kept is a per-link estimate, and it is kept here."""
        if self._handle is None:
            return
        observations = [
            Observation(
                t=float(event.t),
                path_id=str(event.data["path_id"]),
                latency_ms=event.data.get("latency_ms"),
                loss=event.data.get("loss"),
                source="idint",
            )
            for event in session.drain(self._handle)
            if "path_id" in event.data
        ]
        if observations:
            self.observe(observations, session.view())


from .llm import LanguageModelAgent  # noqa: E402

TOOL_USING_MODELS = {"prober": BudgetedProber, "llm": LanguageModelAgent}
