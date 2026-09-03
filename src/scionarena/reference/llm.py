"""A tool-using language model, and the seam that lets it be measured at all.

The headline comparison this harness exists for is a language model that decides
in seconds against classical models that decide in microseconds. Almost
everything it needs was already here: ``ToolUsingModel`` and ``act``, six tools
with real costs, a budget, the parity pair, and -- since ADR 0021 -- a clock that
charges a model for its own deliberation. Two things were not, and both are in
this module.

**The transport seam.** A language model reaches the harness over a network, and
a benchmark cannot depend on that. So :class:`LanguageModelAgent` owns the loop
and delegates exactly one decision per scope per turn to a :class:`Transport`.
``scripted`` is a deterministic policy in Python and is what CI runs; ``http`` is
a real model behind the Anthropic or OpenAI API; ``replay`` reads back a recorded
run, which is what makes a paid run auditable by someone with no key.

``scripted`` is **not a language model and is never reported as one.** Its
architecture tag is ``llm_scripted``, the report groups by architecture, and the
two cannot land in one row. What it is for is every seam the API model will use:
that a model of this shape loads by name, drives itself agentically, pays for its
thinking, scores on every family and survives a parity pair.

**What the model kept.** Invariant 1 says the harness never summarises -- the
model gets the raw log and decides what to retain. For a gradient-boosted model
that is a design detail. For a language model it is the experiment: the log
outgrows any context window inside an episode, and what is thrown away decides
what can still be seen. :meth:`LanguageModelAgent.report_state` is how that
reaches a metric.

No dependency. The HTTP transport is ``urllib`` and ``json``, because a benchmark
that needed a vendor SDK would have that SDK's version in every result file.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..exposure.contracts import (
    SLA,
    Capabilities,
    Observation,
    SessionLike,
    TopologySnapshot,
)
from .models import ReferenceStochastic

__all__ = [
    "LanguageModelAgent",
    "Prompt",
    "Turn",
    "Transport",
    "ScriptedTransport",
    "HttpTransport",
    "ReplayTransport",
    "RETAIN_POLICIES",
]

#: What a model may do with a log that outgrows what it can hold. The whole
#: point of having more than one: the context-rot question is answered by
#: sweeping this, not by arguing about it.
RETAIN_POLICIES = ("recent", "per_path", "none")

#: Default context budget in bytes. Small on purpose -- an episode at the
#: realistic tier produces far more than this, so the policy is exercised rather
#: than being a setting nothing ever reaches.
DEFAULT_CONTEXT_BYTES = 24_000

#: Per-path records kept under ``retain="per_path"``.
PER_PATH_KEEP = 3

#: How long an HTTP transport waits before giving up on one decision. A model
#: that has not answered by then has missed its slot, which is a finding rather
#: than a reason to hang the sweep.
HTTP_TIMEOUT_S = 60.0

#: Where each provider's key comes from. Read from the environment, never
#: written to a result file, never included in a recorded prompt.
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

_ENDPOINT = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
}

SYSTEM = """You are choosing how to split traffic across SCION paths for one
source-destination scope. You are given the paths, whatever measurements you
chose to keep from earlier turns, and this turn's new measurements. Nothing is
summarised for you.

Answer with one JSON object and nothing else:
  {"probe": [path ids to measure next turn],
   "weights": {path id: share},
   "keep": [record ids worth carrying forward],
   "reason": "one short sentence"}

Weights are a distribution over the paths given; they need not sum to one and
will be normalised. Probing costs bandwidth and is rate limited, so ask for few.
Anything you do not keep is gone."""


# --------------------------------------------------------------------------
# the seam


@dataclass(frozen=True)
class Prompt:
    """Everything one decision is made from. Plain data, no session, no world."""

    src: str
    dst: str
    #: ``(path id, hop count, declared latency, last observed latency or None)``.
    paths: tuple[tuple[str, int, float, float | None], ...]
    #: What the model chose to keep from earlier turns, as raw record lines.
    kept: tuple[str, ...]
    #: This turn's records, not yet retained or discarded.
    fresh: tuple[str, ...]
    turn: int
    #: Simulated seconds left before this decision is late.
    slot_s: float
    #: Probe calls still affordable, as the session reports them.
    probes_left: float

    def digest(self) -> str:
        """A stable key for this decision. What a recording is filed under."""
        return hashlib.sha256(
            json.dumps(
                {
                    "src": self.src,
                    "dst": self.dst,
                    "paths": [list(p) for p in self.paths],
                    "kept": list(self.kept),
                    "fresh": list(self.fresh),
                    "turn": self.turn,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]

    def as_text(self) -> str:
        lines = [f"scope {self.src} -> {self.dst}, turn {self.turn}"]
        lines.append(f"slot {self.slot_s:.1f}s, probes affordable {self.probes_left:.0f}")
        lines.append("paths (id, hops, declared_ms, last_observed_ms):")
        lines += [
            f"  {p[0]} {p[1]} {p[2]:.1f} {'-' if p[3] is None else f'{p[3]:.1f}'}"
            for p in self.paths
        ]
        if self.kept:
            lines.append("kept:")
            lines += [f"  {line}" for line in self.kept]
        lines.append("new:")
        lines += [f"  {line}" for line in self.fresh] or ["  (nothing)"]
        return "\n".join(lines)


@dataclass(frozen=True)
class Turn:
    """What the model decided. Everything is advisory -- the agent validates it."""

    probe: tuple[str, ...] = ()
    weights: Mapping[str, float] = field(default_factory=dict)
    keep: tuple[str, ...] = ()
    reason: str = ""
    #: Tokens the provider says it spent, when it says. Zero offline.
    tokens: int = 0

    @staticmethod
    def from_json(text: str) -> Turn:
        """Parse a model's answer, tolerating the fence it usually wraps it in.

        A model that answers with prose gets an empty turn rather than an
        exception: the agent then publishes on what it already knew, which is
        the same thing it does when a tool refuses it. A crash here would score
        a formatting slip as a model that cannot select paths.
        """
        body = text.strip()
        if "```" in body:
            chunks = [c for c in body.split("```") if "{" in c]
            body = chunks[0] if chunks else body
            body = body.partition("\n")[2] if body.lstrip().startswith("json") else body
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return Turn(reason="unparseable answer")
        try:
            data = json.loads(body[start : end + 1])
        except (ValueError, TypeError):
            return Turn(reason="unparseable answer")
        if not isinstance(data, dict):
            return Turn(reason="unparseable answer")
        weights = data.get("weights")
        return Turn(
            probe=tuple(str(p) for p in data.get("probe", []) if isinstance(p, (str, int))),
            weights=(
                {str(k): float(v) for k, v in weights.items() if _finite(v)}
                if isinstance(weights, dict)
                else {}
            ),
            keep=tuple(str(k) for k in data.get("keep", []) if isinstance(k, (str, int))),
            reason=str(data.get("reason", ""))[:200],
        )


def _finite(value: Any) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


@runtime_checkable
class Transport(Protocol):
    """One decision, one answer. The only thing that differs between an offline
    stand-in and a paid API call."""

    def decide(self, prompt: Prompt) -> Turn: ...


class ScriptedTransport:
    """A deterministic policy in Python. **Not a language model.**

    It exists so that every seam the API model uses is exercised without a key: a
    model of this shape loading by name, driving itself, paying for its thinking,
    being scored on every family, and surviving a parity pair. Its architecture
    tag is ``llm_scripted`` for exactly this reason -- the report groups by
    architecture, so it can never share a row with a real one.

    The policy: probe the paths it knows least about, weight by the inverse of
    the best estimate it has, and keep the newest record per path.
    """

    name = "scripted"

    def __init__(self, probes: int = 2, sharpness: float = 2.0) -> None:
        self.probes = int(probes)
        self.sharpness = float(sharpness)

    def decide(self, prompt: Prompt) -> Turn:
        if not prompt.paths:
            return Turn(reason="no paths")
        unseen = [p for p in prompt.paths if p[3] is None]
        # Least known first, then longest since anything was learned. Ties break
        # on the identifier so two runs of one seed agree.
        order = sorted(prompt.paths, key=lambda p: ((p[3] is not None), p[0]))
        # ``probes_left`` is infinite under an unlimited budget, which is the
        # default, so it is clamped before it reaches ``int``.
        affordable = self.probes if prompt.probes_left == float("inf") else int(prompt.probes_left)
        want = max(0, min(self.probes, len(order), affordable))
        estimates = {p[0]: (p[3] if p[3] is not None else p[2]) for p in prompt.paths}
        best = min(estimates.values()) or 1.0
        weights = {pid: (best / v) ** self.sharpness for pid, v in estimates.items() if v > 0}
        keep = tuple(line for line in (prompt.kept + prompt.fresh))[-len(prompt.paths) * 2 :]
        return Turn(
            probe=tuple(p[0] for p in order[:want]),
            weights=weights or dict.fromkeys(estimates, 1.0),
            keep=keep,
            reason=f"{len(unseen)} unmeasured, weighting by inverse estimate",
        )


class ReplayTransport:
    """Replays a recorded run from a JSONL file, keyed by prompt digest.

    What makes a paid run auditable: the numbers can be recomputed by somebody
    with no key, from the file, and any disagreement is a harness change rather
    than a different sample from the model.
    """

    name = "replay"

    def __init__(self, path: str, strict: bool = True) -> None:
        self.path = Path(path)
        self.strict = bool(strict)
        self.misses = 0
        self._by_digest: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                digest = record.get("digest")
                if isinstance(digest, str):
                    self._by_digest[digest] = record

    def decide(self, prompt: Prompt) -> Turn:
        record = self._by_digest.get(prompt.digest())
        if record is None:
            self.misses += 1
            if self.strict:
                raise KeyError(
                    f"no recorded answer for {prompt.digest()} in {self.path}. A replay that "
                    "silently invented one would report a run that never happened; re-record "
                    "instead, or pass strict=False to score the misses as refusals."
                )
            return Turn(reason="no recording for this prompt")
        return Turn.from_json(record.get("answer", ""))


class HttpTransport:
    """A real model over the Anthropic or OpenAI API. Standard library only.

    The key is read from the environment and never leaves it: it is not written
    to a result file, not included in a recorded prompt, and not carried in any
    exception message this raises.
    """

    name = "http"

    def __init__(
        self,
        model: str,
        provider: str = "anthropic",
        base_url: str = "",
        timeout_s: float = HTTP_TIMEOUT_S,
        max_tokens: int = 700,
        record_to: str = "",
    ) -> None:
        if provider not in KEY_ENV:
            raise ValueError(f"provider must be one of {sorted(KEY_ENV)}, not {provider!r}")
        self.provider = provider
        self.model = model
        self.url = base_url or _ENDPOINT[provider]
        self.timeout_s = float(timeout_s)
        self.max_tokens = int(max_tokens)
        self.record_to = Path(record_to) if record_to else None
        self.calls = 0
        self.failures = 0
        key = os.environ.get(KEY_ENV[provider], "")
        if not key:
            raise RuntimeError(
                f"{KEY_ENV[provider]} is not set. A sweep that reaches an API needs a key and "
                "network egress from every worker process; both are deliberate choices rather "
                "than defaults, and the offline transports ('scripted', 'replay') need neither."
            )
        self._key = key

    # -- request shaping -----------------------------------------------------

    def _body(self, text: str) -> tuple[dict[str, Any], dict[str, str]]:
        if self.provider == "anthropic":
            return (
                {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": SYSTEM,
                    "messages": [{"role": "user", "content": text}],
                },
                {
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        return (
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": text},
                ],
            },
            {"authorization": f"Bearer {self._key}", "content-type": "application/json"},
        )

    @staticmethod
    def _answer(payload: Mapping[str, Any]) -> tuple[str, int]:
        if "content" in payload:  # anthropic
            parts = payload.get("content") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            usage = payload.get("usage") or {}
            return text, int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        choices = payload.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = (choices[0].get("message") or {}).get("content", "") or ""
        usage = payload.get("usage") or {}
        return text, int(usage.get("total_tokens", 0))

    def decide(self, prompt: Prompt) -> Turn:
        body, headers = self._body(prompt.as_text())
        request = urllib.request.Request(  # noqa: S310 -- the URL is a constructor argument
            self.url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                payload = json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # A provider that is down is not a model that cannot select paths.
            # The turn is empty, the agent publishes on what it knew, and the
            # failure is counted where a report can see it.
            self.failures += 1
            return Turn(reason=f"transport failure: {type(exc).__name__}")
        text, tokens = self._answer(payload)
        if self.record_to is not None:
            self.record_to.parent.mkdir(parents=True, exist_ok=True)
            with self.record_to.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "digest": prompt.digest(),
                            "model": self.model,
                            "provider": self.provider,
                            "prompt": prompt.as_text(),
                            "answer": text,
                            "tokens": tokens,
                        }
                    )
                    + "\n"
                )
        turn = Turn.from_json(text)
        return Turn(turn.probe, turn.weights, turn.keep, turn.reason, tokens)


def _transport(kind: str, **kwargs: Any) -> Transport:
    if kind == "scripted":
        return ScriptedTransport(**kwargs)
    if kind == "replay":
        return ReplayTransport(**kwargs)
    if kind == "http":
        return HttpTransport(**kwargs)
    raise ValueError(f"transport must be 'scripted', 'replay' or 'http', not {kind!r}")


# --------------------------------------------------------------------------
# the agent


class LanguageModelAgent(ReferenceStochastic):
    """Drives itself through the tools and asks a transport what to do.

    The estimator underneath is ``ReferenceStochastic``, exactly as it is for
    ``BudgetedProber``, so that the fixed-cycle half of a parity pair is a
    forecaster of known quality and the difference between the halves is the
    agency rather than the arithmetic.
    """

    def __init__(
        self,
        transport: str = "scripted",
        *,
        retain: str = "recent",
        context_bytes: int = DEFAULT_CONTEXT_BYTES,
        sla: SLA | None = None,
        limit: int = 20,
        n_hosts: int = 100,
        **transport_kwargs: Any,
    ) -> None:
        super().__init__()
        if retain not in RETAIN_POLICIES:
            raise ValueError(f"retain must be one of {list(RETAIN_POLICIES)}, not {retain!r}")
        self.transport = _transport(transport, **transport_kwargs)
        self.retain = retain
        self.context_bytes = int(context_bytes)
        self.sla = sla or SLA()
        self.limit = limit
        self.n_hosts = n_hosts
        self.scopes: list[tuple[str, str]] = []
        # `llm_scripted` is a different architecture from `llm_api` on purpose.
        # The report groups by architecture, and an offline stand-in that could
        # share a row with a real language model would let the headline number
        # be produced without the thing it measures.
        kind = getattr(self.transport, "name", transport)
        self.capabilities = Capabilities(
            name=f"LanguageModelAgent({kind}/{retain})",
            architecture="llm_api" if kind == "http" else f"llm_{kind}",
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
            respects_deadline=False,
            notes=(
                f"Decides through the {kind} transport, retaining {retain} within "
                f"{context_bytes} bytes. What it drops is the experiment."
            ),
        )
        self._reset_agent()

    # ------------------------------------------------------------------ state

    def _reset_agent(self) -> None:
        #: What the model chose to carry forward, newest last. The harness never
        #: touches it -- eviction is the model's own doing and is what
        #: ``context_retained`` measures.
        self._kept: list[str] = []
        self._turn = 0
        self._handle: str | None = None
        self._decisions = 0
        self._tokens = 0
        self._evictions = 0
        self._offered = 0
        self._refusals: dict[str, int] = {}
        self._unparsed = 0

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        super().reset(topo, seed)
        self._reset_agent()
        self.scopes = []

    def report_state(self) -> Mapping[str, float]:
        """What the model kept, for the metrics that ask (ADR 0024).

        Read once, after the last turn, by the driver. Nothing here reaches the
        model, so reading it cannot change the run.
        """
        kept = sum(len(line) for line in self._kept)
        return {
            "context_bytes": float(kept),
            "context_offered_bytes": float(self._offered),
            "context_retained": (kept / self._offered) if self._offered else 0.0,
            "context_evictions": float(self._evictions),
            "model_calls": float(self._decisions),
            "model_tokens": float(self._tokens),
            "model_unparsed": float(self._unparsed),
        }

    # ------------------------------------------------------------------ agent

    def _note(self, result: Any) -> bool:
        if getattr(result, "ok", False):
            return True
        code = str(getattr(result, "error", "unknown"))
        self._refusals[code] = self._refusals.get(code, 0) + 1
        return False

    def act(self, session: SessionLike, deadline_s: float) -> None:
        end = session.now + deadline_s
        if self._handle is None:
            opened = session.call("subscribe", stream="telemetry")
            if self._note(opened):
                self._handle = opened.data["handle"]
        if not self.scopes:
            listed = session.call("list_scopes")
            if self._note(listed):
                self.scopes = [(s["src"], s["dst"]) for s in listed.data["scopes"]]

        fresh = self._drain(session)
        # One turn, one set of new records, one retention decision. Retaining
        # per *scope* was this agent's first version and it counted the same
        # records once per scope: three scopes made the log look three times
        # larger than it was, and ``context_retained`` -- the number the whole
        # experiment turns on -- read three times too small.
        asked: list[str] = []
        for src, dst in self.scopes:
            if session.now >= end:
                break
            found = session.call("query_paths", src=src, dst=dst, limit=self.limit)
            self._note(found)
            paths = session.known_paths(src, dst)
            if not paths:
                continue
            asked += list(
                self._one_scope(session, src, dst, paths, fresh, max(0.0, end - session.now))
            )
        self._offered += sum(len(line) for line in fresh)
        self._remember(tuple(asked), fresh)
        self._turn += 1

    def _one_scope(
        self,
        session: SessionLike,
        src: str,
        dst: str,
        paths: Sequence[Any],
        fresh: Sequence[str],
        slot_s: float,
    ) -> tuple[str, ...]:
        """Decide and publish for one scope; return what it asked to keep."""
        view = session.view()
        estimates = self.predict(view, list(paths))
        prompt = Prompt(
            src=src,
            dst=dst,
            paths=tuple(
                (
                    str(p.path_id),
                    len(p.interfaces),
                    float(estimates[p.path_id].latency_ms.point) if p.path_id in estimates else 0.0,
                    self._last_seen(p.path_id, fresh),
                )
                for p in paths
            ),
            kept=tuple(self._kept),
            fresh=tuple(fresh),
            turn=self._turn,
            slot_s=slot_s,
            probes_left=self._probes_left(session),
        )
        turn = self.transport.decide(prompt)
        self._decisions += 1
        self._tokens += int(turn.tokens)
        if turn.reason.startswith("unparseable"):
            self._unparsed += 1

        known = {str(p.path_id) for p in paths}
        self._probe(session, [pid for pid in turn.probe if pid in known])

        weights = {k: max(0.0, float(v)) for k, v in turn.weights.items() if k in known}
        if not weights or sum(weights.values()) <= 0.0:
            # A model that answered with prose, or with paths that do not exist,
            # still has to publish. Falling back to its own estimator is the same
            # thing it does when a tool refuses it, and it keeps a formatting
            # slip from scoring as an inability to select paths.
            weights = dict(self.advise(view, list(paths), self.sla, self.n_hosts).normalised())
        total = sum(weights.values()) or 1.0
        self._note(
            session.call(
                "publish_advisory",
                src=src,
                dst=dst,
                weights={k: v / total for k, v in weights.items()},
                meta={"reason": turn.reason[:200], "turn": self._turn, "kept": len(self._kept)},
            )
        )
        return turn.keep

    # ---------------------------------------------------------------- context

    def _remember(self, asked: Sequence[str], fresh: Sequence[str]) -> None:
        """Apply the model's retention choice, then its budget.

        Two separate things, and the order matters. What the model *asked* to
        keep is its decision and is recorded as such; the byte budget is the
        window it has to live inside, and what the budget takes away is an
        eviction. Conflating them would make a model that manages its context
        well indistinguishable from one that was simply given a bigger window.
        """
        if self.retain == "none":
            self._evictions += len(self._kept)
            self._kept = []
            return
        offered = list(self._kept) + list(fresh)
        if asked:
            wanted = set(asked)
            # Whole lines only. The first version also matched on the line's
            # first token as a convenience, and the first token is the
            # *timestamp*: a model that asked to keep the id "20.0" kept every
            # unrelated record taken at t=20 and was scored as though it had
            # chosen them. A retention metric that can be satisfied by accident
            # measures nothing.
            chosen = [line for line in offered if line in wanted]
            kept = chosen or offered
        else:
            kept = offered
        if self.retain == "per_path":
            seen: dict[str, int] = {}
            out: list[str] = []
            for line in reversed(kept):
                key = line.split(" ", 2)[1] if line.count(" ") >= 2 else line
                seen[key] = seen.get(key, 0) + 1
                if seen[key] <= PER_PATH_KEEP:
                    out.append(line)
            kept = list(reversed(out))
        dropped = len(offered) - len(kept)
        while kept and sum(len(line) for line in kept) > self.context_bytes:
            kept.pop(0)
            dropped += 1
        self._evictions += max(0, dropped)
        self._kept = kept

    def _drain(self, session: SessionLike) -> list[str]:
        """The raw feed, rendered one record per line and not summarised.

        A line is ``t path_id latency_ms loss``. That is the record as it
        arrived; nothing is averaged, bucketed or dropped here, because deciding
        what to drop is the model's job and measuring that decision is the point.
        """
        if self._handle is None:
            return []
        lines: list[str] = []
        observations: list[Observation] = []
        for event in session.drain(self._handle):
            data = event.data
            if "path_id" not in data:
                continue
            latency = data.get("latency_ms")
            loss = data.get("loss")
            lines.append(
                f"{float(event.t):.1f} {data['path_id']} "
                f"{'-' if latency is None else f'{float(latency):.2f}'} "
                f"{'-' if loss is None else f'{float(loss):.4f}'}"
            )
            observations.append(
                Observation(
                    t=float(event.t),
                    path_id=str(data["path_id"]),
                    latency_ms=latency,
                    loss=loss,
                    source="idint",
                )
            )
        if observations:
            self.observe(observations, session.view())
        return lines

    # ---------------------------------------------------------------- helpers

    def _probe(self, session: SessionLike, path_ids: Iterable[str]) -> None:
        observations: list[Observation] = []
        for path_id in path_ids:
            result = session.call("probe_path", path_id=path_id, kind="latency")
            if not self._note(result):
                continue
            latency = result.data.get("latency_ms")
            if latency is not None:
                observations.append(
                    Observation(t=session.now, path_id=path_id, latency_ms=latency, source="scmp")
                )
        if observations:
            self.observe(observations, session.view())

    def _last_seen(self, path_id: str, fresh: Sequence[str] = ()) -> float | None:
        """The newest observed latency for a path, from what the model can see.

        Deliberately read out of the lines rather than out of the estimator: it
        is what the model can still see, which is the quantity the retention
        policy is being measured on.

        ``fresh`` is this turn's records, and leaving them out was a bug. The
        prompt is built before ``_remember`` runs, so a path measured moments
        ago was presented as never observed -- and the scripted policy picks
        its probe targets by exactly that field, so it re-probed paths it had
        just paid to measure. That is a charge against the budget, in a harness
        whose second invariant is that every call is charged.
        """
        for line in reversed(list(self._kept) + list(fresh)):
            parts = line.split(" ")
            if len(parts) >= 3 and parts[1] == path_id and parts[2] != "-":
                try:
                    return float(parts[2])
                except ValueError:
                    return None
        return None

    @staticmethod
    def _probes_left(session: SessionLike) -> float:
        budget = getattr(session, "budget", None)
        remaining = getattr(budget, "remaining", None)
        if not callable(remaining):
            return float("inf")
        try:
            return float(remaining().get("probe_units", float("inf")))
        except Exception:  # noqa: BLE001 -- a budget that will not say is not a failure
            return 0.0
