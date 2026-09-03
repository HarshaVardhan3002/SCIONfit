"""The tool-using language model, its transport seam, and what it kept.

Two things had to exist before the headline comparison could be run at all, and
both are checked here: a seam that lets the model be exercised without a key, and
a measurement of what the model chose to retain -- which for a language model is
the experiment rather than a design detail, because the log outgrows any window
inside an episode and what is thrown away decides what can still be seen.

Nothing here calls an API. ``HttpTransport`` is exercised against its own request
shaping and its own failure path; the one thing a test must never do is depend on
a network, and the one thing this module must never do is let an offline
stand-in be reported as a language model.
"""

from __future__ import annotations

import json

import pytest

from scionarena.core.scenario import Scenario, TopologySpec
from scionarena.exposure.loading import load_model
from scionarena.exposure.loop import LoopConfig, busiest_scopes, resolve_drive, run_loop
from scionarena.reference.llm import (
    RETAIN_POLICIES,
    HttpTransport,
    LanguageModelAgent,
    Prompt,
    ReplayTransport,
    ScriptedTransport,
    Turn,
)


def scenario(seed: int = 7) -> Scenario:
    return Scenario(name="t", seed=seed, topology=TopologySpec(tier="smoke"))


def prompt(**changes: object) -> Prompt:
    base = dict(
        src="1-ff00:0:1",
        dst="1-ff00:0:9",
        paths=(("pa", 3, 20.0, 22.0), ("pb", 4, 30.0, None)),
        kept=("10.0 pa 21.00 0.0010",),
        fresh=("20.0 pa 22.00 0.0010",),
        turn=3,
        slot_s=30.0,
        probes_left=4.0,
    )
    base.update(changes)
    return Prompt(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the seam


def test_the_offline_transport_is_never_reported_as_a_language_model() -> None:
    """The rule the whole module is built around. A benchmark whose headline row
    can be produced without the thing it measures is worse than one with no row:
    the row would be quoted."""
    scripted = LanguageModelAgent(transport="scripted")
    assert scripted.capabilities.architecture == "llm_scripted"
    assert scripted.capabilities.architecture != "llm_api"


def test_a_prompt_carries_no_session_and_no_world() -> None:
    """It is what gets sent over a network. Anything reachable from it that
    reached the substrate would be a model reading the world directly."""
    fields = set(vars(prompt()))
    assert fields == {
        "src",
        "dst",
        "paths",
        "kept",
        "fresh",
        "turn",
        "slot_s",
        "probes_left",
    }


def test_the_digest_is_stable_and_ignores_the_slot() -> None:
    """A recording is filed under it, so it has to be the same for the same
    decision -- and the wall-clock slack left in the turn is not part of what
    the decision was made from."""
    assert prompt().digest() == prompt().digest()
    assert prompt().digest() == prompt(slot_s=11.0).digest()
    assert prompt().digest() != prompt(turn=4).digest()


def test_an_answer_wrapped_in_a_fence_still_parses() -> None:
    text = '```json\n{"probe": ["pa"], "weights": {"pa": 0.7, "pb": 0.3}, "reason": "x"}\n```'
    turn = Turn.from_json(text)
    assert turn.probe == ("pa",)
    assert turn.weights == {"pa": 0.7, "pb": 0.3}


def test_prose_gets_an_empty_turn_rather_than_an_exception() -> None:
    """A crash here would score a formatting slip as an inability to select
    paths, which is the wrong finding about a completely different thing."""
    assert Turn.from_json("I think path A looks good.").weights == {}
    assert Turn.from_json("").reason.startswith("unparseable")
    assert Turn.from_json("{not json at all}").reason.startswith("unparseable")


def test_a_nan_weight_is_dropped_rather_than_published() -> None:
    turn = Turn.from_json('{"weights": {"pa": 1.0, "pb": "NaN", "pc": null}}')
    assert set(turn.weights) == {"pa"}


def test_the_scripted_policy_measures_what_it_knows_least_about() -> None:
    turn = ScriptedTransport(probes=1).decide(prompt())
    assert turn.probe == ("pb",), "pb has never been observed"


def test_it_does_not_ask_for_probes_it_cannot_afford() -> None:
    assert ScriptedTransport(probes=3).decide(prompt(probes_left=0.0)).probe == ()


def test_an_unlimited_budget_does_not_overflow_the_probe_count() -> None:
    """Names the bug: ``probes_left`` is infinite under the default unlimited
    budget, and ``int(inf)`` raises -- which killed the first agentic run."""
    assert ScriptedTransport(probes=2).decide(prompt(probes_left=float("inf"))).probe


# --------------------------------------------------------------------------
# replay


def test_a_replay_reproduces_a_recorded_decision(tmp_path) -> None:
    """What makes a paid run auditable: the numbers can be recomputed by somebody
    with no key, from the file."""
    p = prompt()
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "digest": p.digest(),
                "answer": '{"probe": ["pb"], "weights": {"pa": 0.9, "pb": 0.1}}',
            }
        )
        + "\n",
        encoding="utf-8",
    )
    turn = ReplayTransport(str(path)).decide(p)
    assert turn.probe == ("pb",)
    assert turn.weights["pa"] == pytest.approx(0.9)


def test_a_replay_with_no_recording_refuses_rather_than_inventing_one(tmp_path) -> None:
    """A silently invented answer would report a run that never happened."""
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(KeyError, match="no recorded answer"):
        ReplayTransport(str(path)).decide(prompt())


def test_a_lenient_replay_counts_its_misses(tmp_path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    transport = ReplayTransport(str(path), strict=False)
    transport.decide(prompt())
    assert transport.misses == 1


# --------------------------------------------------------------------------
# http, without a network


def test_the_http_transport_refuses_to_exist_without_a_key(monkeypatch) -> None:
    """Network egress from every worker process is a deliberate choice rather
    than a default, and the message says so."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        HttpTransport(model="whatever")


def test_the_key_never_reaches_a_recorded_prompt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    transport = HttpTransport(model="m", record_to=str(tmp_path / "rec.jsonl"))
    body, headers = transport._body("hello")
    assert "sk-secret-value" not in json.dumps(body)
    assert headers["x-api-key"] == "sk-secret-value"
    assert "sk-secret-value" not in prompt().as_text()


def test_a_provider_that_is_down_is_not_a_model_that_cannot_select_paths(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    transport = HttpTransport(model="m", provider="openai", base_url="http://127.0.0.1:1/none")
    turn = transport.decide(prompt())
    assert turn.weights == {}
    assert transport.failures == 1
    assert turn.reason.startswith("transport failure")


def test_both_provider_shapes_are_parsed() -> None:
    anthropic = {"content": [{"text": '{"weights": {"pa": 1}}'}], "usage": {"input_tokens": 5}}
    openai = {"choices": [{"message": {"content": '{"weights": {"pa": 1}}'}}], "usage": {}}
    assert HttpTransport._answer(anthropic)[1] == 5
    assert '"pa"' in HttpTransport._answer(openai)[0]


def test_an_unknown_provider_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="provider must be one of"):
        HttpTransport(model="m", provider="acme")


# --------------------------------------------------------------------------
# what it kept


def test_every_retention_policy_is_a_variant_of_one_architecture() -> None:
    tags = {LanguageModelAgent(retain=r).capabilities.architecture for r in RETAIN_POLICIES}
    assert tags == {"llm_scripted"}, "the policy is a variant, not an architecture"


def test_an_unknown_retention_policy_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="retain must be one of"):
        LanguageModelAgent(retain="everything")


def test_keeping_nothing_keeps_nothing_and_says_so() -> None:
    agent = LanguageModelAgent(retain="none")
    agent._kept = ["a", "b"]
    agent._remember((), ["c"])
    assert agent._kept == []
    assert agent.report_state()["context_bytes"] == 0.0


def test_the_byte_budget_evicts_the_oldest_first() -> None:
    agent = LanguageModelAgent(retain="recent", context_bytes=12)
    agent._remember((), ["aaaaa", "bbbbb", "ccccc"])
    assert agent._kept == ["bbbbb", "ccccc"]
    assert agent.report_state()["context_evictions"] == 1.0


def test_per_path_keeps_a_few_of_each_rather_than_a_few_in_total() -> None:
    lines = [f"{t}.0 p{t % 2} 10.00 0.0010" for t in range(12)]
    agent = LanguageModelAgent(retain="per_path", context_bytes=10_000)
    agent._remember((), lines)
    paths = {line.split(" ")[1] for line in agent._kept}
    assert paths == {"p0", "p1"}, "both paths survive, which is the point of the policy"
    assert len(agent._kept) == 6


def test_the_log_is_retained_once_per_turn_not_once_per_scope() -> None:
    """Names the bug: retaining per scope applied the same records once per
    scope, so three scopes counted every record three times and
    ``context_retained`` -- the number the whole experiment turns on -- read
    three times too small.

    Asserted on the call count rather than on the byte totals. Comparing a
    one-scope run against a three-scope one was this test's first version and it
    is not a comparison: three scopes carry more traffic over more paths, so the
    log is genuinely several times larger and the ratio says nothing about how
    often it was counted.
    """
    sc = scenario()
    world = sc.build()
    agent = LanguageModelAgent(retain="recent", context_bytes=4_000)
    calls = {"remember": 0}
    inner = agent._remember

    def counted(asked, fresh):  # type: ignore[no-untyped-def]
        calls["remember"] += 1
        inner(asked, fresh)

    agent._remember = counted  # type: ignore[assignment]
    cycles = 12
    result = run_loop(
        agent,
        sc,
        busiest_scopes(world, 3),
        config=LoopConfig(cycles=cycles, decision_s=30.0, n_hosts=200),
        world=world,
    )
    assert calls["remember"] == cycles
    assert result.model_report["model_calls"] == 3 * cycles, "one decision per scope per turn"


def test_a_model_that_says_nothing_reports_nothing_rather_than_zero() -> None:
    """The existing convention: ``None`` is not measured and zero is a score."""
    from scionarena.instrument.metrics import compute

    sc = scenario()
    world = sc.build()
    result = run_loop(
        load_model("ema"),
        sc,
        busiest_scopes(world, 1),
        config=LoopConfig(cycles=8, decision_s=30.0, n_hosts=100),
        world=world,
    )
    assert result.model_report == {}
    assert compute(result.metric_input(), names=["context_bytes"])["context_bytes"] is None


def test_a_model_whose_bookkeeping_raises_does_not_cost_the_episode() -> None:
    from scionarena.exposure.loop import _model_report

    class Broken:
        def report_state(self) -> dict[str, float]:
            raise RuntimeError("no")

    class Weird:
        def report_state(self) -> dict[str, object]:
            return {"a": float("nan"), "b": float("inf"), "c": "text", "d": 2.0}

    assert _model_report(Broken()) == {}  # type: ignore[arg-type]
    assert _model_report(Weird()) == {"d": 2.0}  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# through the real loop


def test_it_loads_by_name_and_drives_itself() -> None:
    model = load_model("llm", args={"retain": "per_path", "context_bytes": 3000})
    assert resolve_drive(model) == "agentic"
    sc = scenario()
    world = sc.build()
    result = run_loop(
        model,
        sc,
        busiest_scopes(world, 2),
        config=LoopConfig(cycles=16, decision_s=30.0, n_hosts=200, record_forecasts=True),
        world=world,
    )
    assert result.drive == "agentic"
    assert result.session_summary["advisories"] > 0
    assert result.model_report["model_calls"] > 0
    assert result.forecasts


def test_the_retention_policy_changes_the_run() -> None:
    """Otherwise the context axis is a setting nothing reaches, and the
    context-rot question is unanswerable by measurement."""
    sc = scenario()
    reports = {}
    for retain in ("recent", "none"):
        world = sc.build()
        result = run_loop(
            LanguageModelAgent(retain=retain, context_bytes=2_000),
            sc,
            busiest_scopes(world, 2),
            config=LoopConfig(cycles=24, decision_s=30.0, n_hosts=200),
            world=world,
        )
        reports[retain] = result
    assert reports["recent"].report()["digest"] != reports["none"].report()["digest"]
    assert reports["recent"].model_report["context_bytes"] > 0.0
    assert reports["none"].model_report["context_bytes"] == 0.0


def test_it_survives_a_transport_that_answers_with_nothing() -> None:
    """A model whose provider is down still has to publish, and what it falls
    back to is its own estimator -- the same thing it does when a tool refuses
    it."""

    class Mute:
        name = "mute"

        def decide(self, prompt: Prompt) -> Turn:
            return Turn(reason="unparseable answer")

    sc = scenario()
    world = sc.build()
    agent = LanguageModelAgent()
    agent.transport = Mute()
    result = run_loop(
        agent,
        sc,
        busiest_scopes(world, 2),
        config=LoopConfig(cycles=10, decision_s=30.0, n_hosts=200),
        world=world,
    )
    assert result.session_summary["advisories"] > 0
    assert agent.report_state()["model_unparsed"] > 0


def test_a_weight_for_a_path_that_does_not_exist_is_dropped() -> None:
    class Hallucinating:
        name = "hallucinating"

        def decide(self, prompt: Prompt) -> Turn:
            return Turn(weights={"no-such-path": 1.0}, reason="made it up")

    sc = scenario()
    world = sc.build()
    agent = LanguageModelAgent()
    agent.transport = Hallucinating()
    result = run_loop(
        agent,
        sc,
        busiest_scopes(world, 1),
        config=LoopConfig(cycles=8, decision_s=30.0, n_hosts=200),
        world=world,
    )
    assert result.session_summary["advisories"] > 0, "it still published, on its own estimate"
