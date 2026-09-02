"""M2: the exposure layer.

One test per acceptance criterion, plus the invariants they stand on. The ones
that matter most are the negative ones -- a model that calls nothing learns
nothing, a rate limit refuses rather than drops, a probe can fail -- because a
harness where everything succeeds measures nothing.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import numpy as np
import pytest

from scionarena.core.scenario import ProbeLimits, Scenario, TimelineEvent, TopologySpec
from scionarena.exposure import Budget, Cost, Session, tool_definitions
from scionarena.exposure.session import _hex, drive_episode, run_episode
from scionarena.exposure.tools import PROBE_KINDS, TOOLS, ToolSpec
from scionarena.reference import REFERENCE_MODELS, BudgetedProber

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def scenario(name: str = "m2", tier: str = "smoke", **changes) -> Scenario:
    return Scenario(
        name=name,
        seed=7,
        topology=TopologySpec(tier=tier),
        duration_s=600.0,
        step_s=1.0,
        **changes,
    )


def session(budget: Budget | None = None, **kwargs) -> Session:
    return Session(scenario().build(), budget=budget or Budget.unlimited(), seed=3, **kwargs)


def session_with(limits: ProbeLimits) -> Session:
    """A session whose scenario states what probing it permits."""
    return Session(scenario().with_(probe_limits=limits).build(), budget=Budget.unlimited(), seed=3)


def as_names(s: Session) -> list[str]:
    topo = s._world.topology
    return [topo.as_name(i) for i in range(topo.n_ases)]


def busiest_scope(s: Session) -> tuple[str, str]:
    """A (src, dst) with several paths. A scope with one path cannot show
    anything about spreading, refusing or rotating."""
    names = as_names(s)
    best, best_n = (names[0], names[1]), -1
    for src in names[:8]:
        for dst in names[:8]:
            if src == dst:
                continue
            n = len(
                s._world.paths_for(s._world.topology.as_index(src), s._world.topology.as_index(dst))
            )
            if n > best_n:
                best, best_n = (src, dst), n
    return best


@pytest.fixture
def sess() -> Session:
    return session()


@pytest.fixture
def scope(sess: Session) -> tuple[str, str]:
    return busiest_scope(sess)


# --------------------------------------------------------------------------
# 1. a model with no tool calls gets no information
# --------------------------------------------------------------------------


def test_a_model_that_calls_nothing_learns_nothing(sess: Session):
    """The first acceptance criterion, and the one everything else leans on."""
    before = sess.view()

    sess.advance(300.0)  # five minutes of beaconing, ageing and background load

    after = sess.view()
    assert after.paths == before.paths == ()
    assert after.interfaces == before.interfaces == {}


def test_the_view_only_grows_through_tool_results(sess: Session, scope):
    src, dst = scope
    assert sess.view().paths == ()

    sess.call("query_paths", src=src, dst=dst)

    assert len(sess.view().paths) > 0
    assert all(p.src == src and p.dst == dst for p in sess.view().paths)


def test_the_view_is_stamped_when_it_was_told_not_when_it_is_read(sess: Session, scope):
    sess.call("query_paths", src=scope[0], dst=scope[1])
    told_at = sess.view().t

    sess.advance(120.0)

    assert sess.view().t == told_at, "a stale view that claims to be current hides R10"
    assert sess.now >= told_at + 120.0


# --------------------------------------------------------------------------
# 2. rate limits, per border router, refused rather than dropped
# --------------------------------------------------------------------------


def test_probes_through_one_border_router_run_out_of_allowance(sess: Session, scope):
    src, dst = scope
    found = sess.call("query_paths", src=src, dst=dst)
    path_id = found.data["paths"][0]["path_id"]

    results = [sess.call("probe_path", path_id=path_id) for _ in range(8)]

    refused = [r for r in results if not r.ok and r.error == "rate_limited"]
    assert refused, "eight echoes down one router inside a second is not permitted"
    assert all(r.retry_after_s and r.retry_after_s > 0 for r in refused)


def test_a_refused_probe_is_a_result_not_a_silence(sess: Session, scope):
    src, dst = scope
    path_id = sess.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]
    for _ in range(8):
        last = sess.call("probe_path", path_id=path_id)

    assert last.ok is False
    assert last.error == "rate_limited"
    assert bool(last) is False
    logged = [r for r in sess.log if r.name == "probe_path" and not r.ok]
    assert logged and logged[-1].error == "rate_limited"
    assert logged[-1].cost.wall_clock_s > 0, "asking is not free, or polling the limiter is"


def test_waiting_restores_the_allowance(sess: Session, scope):
    src, dst = scope
    path_id = sess.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]
    for _ in range(8):
        sess.call("probe_path", path_id=path_id)

    sess.advance(5.0)

    assert sess.call("probe_path", path_id=path_id).ok


def two_routers(s: Session) -> tuple[str, str]:
    """Two path ids that leave through different border routers."""
    names = as_names(s)
    by_router: dict[str, str] = {}
    for src in names[:6]:
        for dst in names[:6]:
            if src == dst:
                continue
            found = s.call("query_paths", src=src, dst=dst)
            s.advance(1.0)  # the path server is rate limited too
            if not found.ok:
                continue
            for row in found.data["paths"]:
                by_router.setdefault(s.border_router(row["path_id"]), row["path_id"])
            if len(by_router) >= 2:
                return tuple(list(by_router.values())[:2])  # type: ignore[return-value]
    pytest.skip("no two paths leaving through different border routers in this fixture")


def test_different_border_routers_do_not_contend():
    """The limit is a property of the network, so spreading probes is a real
    mitigation. If the key were the model, it would not be."""
    s = session()
    first, second = two_routers(s)

    spent = [s.call("probe_path", path_id=first) for _ in range(6)]
    other = s.call("probe_path", path_id=second)

    assert any(r.error == "rate_limited" for r in spent), "the first router ran out"
    assert other.ok, "and the second one had nothing to do with it"


def test_a_bandwidth_test_has_its_own_much_tighter_limit(sess: Session, scope):
    src, dst = scope
    path_id = sess.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]

    first = sess.call("probe_path", path_id=path_id, kind="bandwidth")
    second = sess.call("probe_path", path_id=path_id, kind="bandwidth")

    assert first.ok
    assert second.error == "rate_limited", "one bwtest per router per 30s"


# --------------------------------------------------------------------------
# 3. budgets: exhaustion is survivable and recorded
# --------------------------------------------------------------------------


def test_running_out_of_probes_does_not_end_the_episode(scope):
    s = session(Budget(probe_units=1.0))
    src, dst = busiest_scope(s)
    path_id = s.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]

    first = s.call("probe_path", path_id=path_id)
    second = s.call("probe_path", path_id=path_id)
    published = s.call(
        "publish_advisory", src=src, dst=dst, weights={path_id: 1.0}, meta={"blind": True}
    )

    assert first.ok
    assert second.error == "budget_exhausted"
    assert published.ok, "publishing must survive going blind; that is the interesting case"
    assert s.budget.refusals["probe_units"] == 1


def test_exhaustion_names_the_dimension_it_ran_out_of():
    s = session(Budget(nbytes=300.0))
    src, dst = busiest_scope(s)

    s.call("query_paths", src=src, dst=dst)
    refused = s.call("query_paths", src=src, dst=dst)

    assert refused.error == "budget_exhausted"
    assert "nbytes" in refused.message
    assert s.budget.refusals.get("nbytes")


def test_a_call_that_could_not_be_paid_for_is_still_charged():
    """Otherwise failing is the cheap way to look at the network."""
    s = session(Budget(nbytes=300.0))
    src, dst = busiest_scope(s)
    s.call("query_paths", src=src, dst=dst)
    before = s.budget.spent

    s.call("query_paths", src=src, dst=dst)

    assert s.budget.spent.nbytes > before.nbytes


def test_the_compute_budget_is_optional_and_separate():
    s = session(Budget(tokens=100))

    assert s.think(60) is True
    assert s.think(60) is False
    assert s.budget.refusals["tokens"] == 1


# --------------------------------------------------------------------------
# 4. schemas are LLM function definitions as they stand
# --------------------------------------------------------------------------


NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
ALLOWED_KEYWORDS = {
    "type",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "default",
    "minimum",
    "maximum",
    "items",
}


@pytest.mark.parametrize("dialect", ["anthropic", "openai"])
def test_every_tool_is_a_valid_function_definition(dialect: str):
    definitions = tool_definitions(dialect)

    assert len(definitions) == len(TOOLS)
    for definition in definitions:
        body = definition if dialect == "anthropic" else definition["function"]
        schema = body["input_schema" if dialect == "anthropic" else "parameters"]
        assert NAME_RE.match(body["name"])
        assert len(body["description"]) > 40, "a one-word description is not a tool spec"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", ())) <= set(schema["properties"])
        assert json.loads(json.dumps(definition)) == definition


def test_no_schema_uses_a_keyword_the_validator_does_not_know():
    """The validator here is deliberately small. This test is what keeps that
    honest: grow a schema and either teach the validator or fail."""
    for spec in TOOLS.values():
        assert set(spec.parameters) <= ALLOWED_KEYWORDS
        for name, prop in spec.parameters["properties"].items():
            unknown = set(prop) - ALLOWED_KEYWORDS
            assert not unknown, f"{spec.name}.{name} uses {unknown}"
            assert prop.get("description"), f"{spec.name}.{name} is undocumented"


def test_arguments_are_validated_against_the_schema(sess: Session, scope):
    src, dst = scope

    assert sess.call("query_paths", src=src, dst=dst, nonsense=1).error == "invalid_arguments"
    assert sess.call("query_paths", src=src).error == "invalid_arguments"
    assert sess.call("query_paths", src=src, dst=dst, limit="many").error == "invalid_arguments"
    assert sess.call("subscribe", stream="feelings").error == "invalid_arguments"
    assert sess.call("nonexistent").error == "unknown_tool"


# --------------------------------------------------------------------------
# 5. the log replays to an identical state
# --------------------------------------------------------------------------


def test_the_log_replays_to_an_identical_session_state():
    sc = scenario("replay")
    original = Session(sc.build(), seed=3)
    src, dst = busiest_scope(original)
    drive_episode(
        REFERENCE_MODELS["reference"](), original, scopes=[(src, dst)], cycles=4, step_s=2.0
    )

    replayed = Session.replay(sc, original.log, seed=3)

    assert replayed.state_digest() == original.state_digest()
    assert len(replayed.log) == len(original.log)


def test_two_identical_sessions_agree_and_a_different_seed_does_not():
    sc = scenario("determinism")
    src, dst = busiest_scope(Session(sc.build()))

    def run(seed: int) -> str:
        s = Session(sc.build(), seed=seed)
        drive_episode(REFERENCE_MODELS["reference"](), s, scopes=[(src, dst)], cycles=3, step_s=2.0)
        return s.state_digest()

    assert run(3) == run(3)
    assert run(3) != run(4), "the probe stream must depend on the session seed"


# --------------------------------------------------------------------------
# 6. no model can reach the substrate
# --------------------------------------------------------------------------


SRC = Path(__file__).resolve().parents[1] / "src" / "scionarena"


def imported_names(path: Path) -> set[str]:
    """Every module a file names in an import statement, absolute or relative."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.relative_to(SRC).parts[:-1]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ("scionarena", *package[: len(package) - node.level + 1])
                out.add(".".join((*base, node.module)) if node.module else ".".join(base))
            elif node.module:
                out.add(node.module)
    return out


def test_no_model_names_the_substrate():
    """Import-linter proves this over the whole transitive graph in CI. This is
    the same rule read straight off the source, so it fails during development
    rather than at the end of the pipeline."""
    for path in sorted((SRC / "reference").glob("*.py")):
        named = imported_names(path)
        assert not {n for n in named if n.startswith("scionarena.core")}, path.name
        assert not {n for n in named if n.startswith("scionarena.exposure.session")}, path.name


def test_the_contract_module_imports_nothing_of_ours():
    """ADR 0008: the module a model imports has no edge into the substrate at
    all, which is why ``act`` takes a ``SessionLike`` protocol declared there."""
    named = imported_names(SRC / "exposure" / "contracts.py")

    assert not [n for n in named if n.startswith("scionarena")]


# --------------------------------------------------------------------------
# 7. the existing reference models run unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(REFERENCE_MODELS))
def test_every_reference_model_runs_through_a_session_unchanged(name: str):
    s = session(Budget.modest())
    src, dst = busiest_scope(s)

    drive_episode(REFERENCE_MODELS[name](), s, scopes=[(src, dst)], cycles=3, step_s=5.0)

    assert s.advisories, f"{name} published nothing"
    assert s.budget.spent.probe_units > 0, f"{name} was not charged for looking"
    assert all(abs(sum(a["weights"].values()) - 1.0) < 1e-9 for a in s.advisories)


def test_a_tool_using_model_drives_itself():
    s = session(Budget.modest())
    src, dst = busiest_scope(s)
    model = BudgetedProber([(src, dst)])

    run_episode(model, s, scopes=[(src, dst)], cycles=4, step_s=5.0, deadline_s=1.0)

    assert s.advisories
    assert any(r.name == "subscribe" for r in s.log), "it never opened a feed"
    assert model.capabilities.uses_tools is True


def test_a_tool_using_model_keeps_going_after_it_goes_blind():
    s = session(Budget(probe_units=3.0))
    src, dst = busiest_scope(s)
    model = BudgetedProber([(src, dst)], probes_per_turn=2)

    run_episode(model, s, scopes=[(src, dst)], cycles=5, step_s=5.0, deadline_s=1.0)

    assert model.refusals.get("budget_exhausted"), "it never noticed"
    assert len(s.advisories) >= 4, "it stopped advising when it stopped seeing"


def test_a_late_call_is_recorded_rather_than_refused():
    s = session()
    src, dst = busiest_scope(s)
    s.deadline_s = s.now + 0.001

    s.advance(1.0)
    result = s.call("query_paths", src=src, dst=dst)

    assert result.ok
    assert s.log[-1].late is True


# --------------------------------------------------------------------------
# 8. a sixth tool touches only the registry
# --------------------------------------------------------------------------


def test_adding_a_sixth_tool_changes_nothing_else():
    """The registry is data and ``Session`` never names a tool. If this test
    needs a change anywhere but here and tools.py, that has stopped being true."""

    def count_paths(ctx, args):
        return {"n": len(ctx.lookup_paths(str(args["src"]), str(args["dst"]), 100))}

    sixth = ToolSpec(
        name="count_paths",
        description="Count the paths between two ASes without returning them.",
        parameters={
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source ISD-AS."},
                "dst": {"type": "string", "description": "Destination ISD-AS."},
            },
            "required": ["src", "dst"],
            "additionalProperties": False,
        },
        handler=count_paths,
        cost=lambda args, payload: Cost(0.001, nbytes=16),
    )
    registry = {**TOOLS, "count_paths": sixth}
    s = Session(scenario().build(), registry=registry, seed=3)
    src, dst = busiest_scope(s)

    result = s.call("count_paths", src=src, dst=dst)

    assert result.ok and result.data["n"] > 0
    assert result.cost.nbytes == 16
    assert len(tool_definitions("anthropic", registry)) == 6


# --------------------------------------------------------------------------
# costs are charged, and differentiated
# --------------------------------------------------------------------------


def test_the_three_probe_kinds_cost_different_amounts(scope):
    costs = {}
    for kind in PROBE_KINDS:
        s = session()
        src, dst = busiest_scope(s)
        path_id = s.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]
        result = s.call("probe_path", path_id=path_id, kind=kind)
        assert result.ok, f"{kind} probe failed: {result.error}"
        costs[kind] = result.cost

    assert costs["latency"].probe_units < costs["loss"].probe_units < costs["bandwidth"].probe_units
    assert costs["latency"].nbytes < costs["loss"].nbytes < costs["bandwidth"].nbytes
    assert costs["bandwidth"].wall_clock_s > 10 * costs["latency"].wall_clock_s
    assert costs["bandwidth"].probe_units == 100 * costs["latency"].probe_units


def test_charging_wall_clock_moves_the_world(sess: Session, scope):
    src, dst = scope
    path_id = sess.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]
    before = sess.now

    sess.call("probe_path", path_id=path_id, kind="bandwidth")

    assert sess.now == pytest.approx(before + 2.0, abs=1e-6), "invariant 3, in the cost path"


def test_a_bandwidth_test_loads_the_path_it_measures(sess: Session, scope):
    """Measurement disturbs the measured. A harness where it does not teaches a
    model that looking is free, which is the opposite of the lesson."""
    src, dst = scope
    path_id = sess.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]
    quiet = sess._world.links.path_metrics(sess.path_info(path_id).handle.ifaces)

    result = sess.call("probe_path", path_id=path_id, kind="bandwidth")

    assert result.data["latency_ms"] >= quiet.latency_ms
    assert result.data["offered_mbps"] > 0
    after = sess._world.links.path_metrics(sess.path_info(path_id).handle.ifaces)
    assert after.bandwidth_mbps == pytest.approx(quiet.bandwidth_mbps, rel=1e-6), (
        "the test's own load has to go away when the test ends"
    )


def test_publishing_is_nearly_free_and_changes_the_world(sess: Session, scope):
    src, dst = scope
    paths = sess.call("query_paths", src=src, dst=dst).data["paths"]
    weights = {p["path_id"]: 1.0 for p in paths}

    result = sess.call("publish_advisory", src=src, dst=dst, weights=weights)

    assert result.ok
    assert result.cost.probe_units == 0
    assert sess.advisories[-1]["weights"][paths[0]["path_id"]] == pytest.approx(1 / len(paths))


def test_an_advisory_naming_a_path_nobody_offered_is_rejected(sess: Session, scope):
    src, dst = scope

    result = sess.call("publish_advisory", src=src, dst=dst, weights={"deadbeef": 1.0})

    assert result.error == "unknown_path"


# --------------------------------------------------------------------------
# probes must be able to fail
# --------------------------------------------------------------------------


def test_a_probe_can_time_out():
    """Invariant 6. A probe nothing fails measures nothing."""
    sc = scenario("lossy").then(
        TimelineEvent(at_s=1.0, kind="link_degrade", params={"link": 0, "factor": 0.001}),
        TimelineEvent(at_s=1.0, kind="demand_surge", params={"mbps": 5000.0}),
    )
    s = Session(sc.build(), seed=11)
    s.advance(2.0)
    src, dst = busiest_scope(s)
    ids = [p["path_id"] for p in s.call("query_paths", src=src, dst=dst).data["paths"]]

    outcomes = []
    for _ in range(30):
        for path_id in ids:
            outcomes.append(s.call("probe_path", path_id=path_id, kind="loss"))
        s.advance(1.0)

    errors = {r.error for r in outcomes if not r.ok}
    assert outcomes, "nothing was probed"
    assert any(r.ok for r in outcomes), "everything failing is as useless as nothing failing"
    assert "probe_timeout" in errors or any(
        None in r.data.get("samples_ms", []) for r in outcomes if r.ok
    ), "a saturated, degraded path lost nothing at all"


def test_the_probe_allowance_is_a_scenario_parameter_not_a_protocol_constant():
    """Q6. The three limits were constants in ``exposure/tools.py``, written as
    though a border router enforced them. It does not: the SCMP specification
    says only that SCMP *may* be rate limited, and there is no limiter in the
    router at all. What a deployment permits is an operational choice that may
    be zero.

    So a run has to be able to state which regime it assumed, and the M6 sweep
    has to be able to vary it. Before this, two runs made under different
    assumptions about the price of information were indistinguishable in the
    report -- and every budget result the harness produces is conditional on
    exactly that number.
    """
    generous = session_with(ProbeLimits(scmp_calls=50.0, scmp_per_s=1.0))
    stingy = session_with(ProbeLimits(scmp_calls=1.0, scmp_per_s=1.0))

    def echoes_before_refusal(s: Session) -> int:
        src, dst = busiest_scope(s)
        path_id = s.call("query_paths", src=src, dst=dst).data["paths"][0]["path_id"]
        allowed = 0
        for _ in range(12):
            if not s.call("probe_path", path_id=path_id).ok:
                break
            allowed += 1
        return allowed

    assert echoes_before_refusal(stingy) < echoes_before_refusal(generous)
    assert Scenario().probe_limits == ProbeLimits(), "the defaults are the old constants"
    assert "probe_limits" in Scenario().to_dict(), "a run that does not say is not reproducible"


def test_a_bandwidth_probe_straddling_a_grid_tick_leaves_no_negative_load():
    """C2. The probe used to add its own load straight to the link state, run
    for two seconds, then subtract it. A host grid tick inside those two seconds
    rebuilds demand from scratch and drops the contribution, so the subtraction
    landed on a baseline that never carried it and left the interface at minus
    the probe's own rate until the next tick.

    Nothing reported it. The probe returned a plausible number, the model was
    charged correctly, and the link sat at negative offered load -- so whichever
    model probed most was measuring a network its own instrumentation had
    damaged. ``step_s`` is 1.0 and ``BWTEST_S`` is 2.0, so every bandwidth probe
    in a closed-loop run crosses at least one tick.
    """
    s = session()
    world = s._world
    topo = world.topology
    hsrc, hdst = busiest_scope(s)
    world.add_scope(topo.as_index(hsrc), topo.as_index(hdst))
    world.step(1.0)

    # Probe a path the hosts are *not* using, so the damage is not masked by
    # traffic that happens to be larger than it. On a busy interface the same
    # bug understates the load by the probe's rate instead of going negative,
    # which is just as wrong and much harder to see.
    busy = set(np.nonzero(world.links.demand_mbps)[0].tolist())
    idle = next(
        (
            (a, b, path, egress)
            for a in range(topo.n_ases)
            for b in range(topo.n_ases)
            if a != b
            for path in world.paths_for(a, b)
            for egress in [[int(i) for i in path.ifaces[::2]]]
            if egress and not busy.intersection(egress)
        ),
        None,
    )
    if idle is None:
        pytest.skip("every path in this fixture carries host load")
    a, b, path, egress = idle
    s.call("query_paths", src=topo.as_name(a), dst=topo.as_name(b))
    path_id = _hex(path.path_id(world.identity_policy))

    before = world.links.demand_mbps[egress].copy()
    result = s.call("probe_path", path_id=path_id, kind="bandwidth")
    assert result.ok, result.message

    after = world.links.demand_mbps[egress]
    assert after == pytest.approx(before), (
        f"the probe left {after} on interfaces that carried {before} before it ran"
    )
    assert float(world.links.demand_mbps.min()) >= -1e-9, "an interface is offering negative load"
    assert not world._probe_load, "the probe's hold outlived the probe"


def test_a_filtered_path_stops_being_probeable():
    s = session()
    src, dst = busiest_scope(s)
    rows = s.call("query_paths", src=src, dst=dst).data["paths"]
    transit = None
    for row in rows:
        hops = [iface.split("#")[0] for iface in row["interfaces"]]
        middle = [h for h in hops if h not in (src, dst)]
        if middle:
            transit, path_id = middle[0], row["path_id"]
            break
    if transit is None:
        pytest.skip("no multi-hop path in this fixture")

    s._world.clock.at(s.now + 1.0, "as_policy_filter", {"as_": s._world.topology.as_index(transit)})
    s.advance(2.0)
    result = s.call("probe_path", path_id=path_id)

    assert result.error == "path_unavailable"
    assert "no longer offered" in result.message


# --------------------------------------------------------------------------
# streams deliver raw records and charge for them
# --------------------------------------------------------------------------


def test_a_telemetry_record_is_raw(sess: Session, scope):
    src, dst = scope
    paths = sess.call("query_paths", src=src, dst=dst).data["paths"]
    sess.call("subscribe", stream="telemetry")
    sess.call("publish_advisory", src=src, dst=dst, weights={paths[0]["path_id"]: 1.0})

    sess.advance(1.0)
    records = sess.drain("telemetry-0")

    assert records
    assert set(records[0].data) == {
        "path_id",
        "src",
        "dst",
        "latency_ms",
        "loss",
        "available_mbps",
        "share",
        "source",
    }, "a derived field here is invariant 1 going quietly"


def test_telemetry_only_covers_paths_that_carry_traffic(sess: Session, scope):
    src, dst = scope
    paths = sess.call("query_paths", src=src, dst=dst).data["paths"]
    if len(paths) < 2:
        pytest.skip("needs a scope with more than one path")
    sess.call("subscribe", stream="telemetry")
    sess.call("publish_advisory", src=src, dst=dst, weights={paths[0]["path_id"]: 1.0})

    sess.advance(1.0)
    seen = {r.data["path_id"] for r in sess.drain("telemetry-0")}

    assert paths[0]["path_id"] in seen
    assert paths[1]["path_id"] not in seen, "you cannot measure a path nobody is using"


def test_the_beacon_feed_shows_re_signing_keeping_the_structural_id():
    """The failure R4 exists for, made visible: same interfaces, new material."""
    s = session()
    s.call("subscribe", stream="beacons")
    s._world.segments.rebeacon()

    s.advance(1.0)
    records = s.drain("beacons-0")

    assert records
    assert all(r.kind == "resigned" for r in records)
    assert all(r.data["generation"] >= 1 for r in records)


def test_a_subscription_is_charged_on_delivery_and_draining_is_free(sess: Session, scope):
    src, dst = scope
    paths = sess.call("query_paths", src=src, dst=dst).data["paths"]
    sess.call("subscribe", stream="telemetry")
    sess.call("publish_advisory", src=src, dst=dst, weights={paths[0]["path_id"]: 1.0})
    sess.advance(1.0)
    charged = sess.budget.spent.nbytes

    sess.drain("telemetry-0")

    assert sess.subscriptions["telemetry-0"].nbytes > 0
    assert sess.budget.spent.nbytes == charged


def test_a_starved_subscription_says_so_rather_than_thinning_out():
    s = session()
    src, dst = busiest_scope(s)
    paths = s.call("query_paths", src=src, dst=dst).data["paths"]
    s.call("subscribe", stream="telemetry")
    s.call("publish_advisory", src=src, dst=dst, weights={p["path_id"]: 1.0 for p in paths})
    # Enough left for a record or two, and then the feed starves.
    s.budget.nbytes = s.budget.spent.nbytes + 300.0

    for _ in range(20):
        s.advance(1.0)

    assert s.subscriptions["telemetry-0"].dropped > 0
    assert s.budget.refusals.get("nbytes")


def test_history_returns_raw_records_and_costs_by_volume(sess: Session, scope):
    src, dst = scope
    paths = sess.call("query_paths", src=src, dst=dst).data["paths"]
    sess.call("subscribe", stream="telemetry")
    sess.call("publish_advisory", src=src, dst=dst, weights={p["path_id"]: 1.0 for p in paths})
    for _ in range(5):
        sess.advance(1.0)

    small = sess.call("get_history", limit=1)
    everything = sess.call("get_history")

    assert small.data["n_events"] == 1
    assert everything.data["n_events"] > 1
    assert everything.cost.wall_clock_s > small.cost.wall_clock_s
    assert everything.cost.nbytes > small.cost.nbytes


def test_history_can_be_filtered_without_being_summarised(sess: Session, scope):
    src, dst = scope
    paths = sess.call("query_paths", src=src, dst=dst).data["paths"]
    sess.call("publish_advisory", src=src, dst=dst, weights={paths[0]["path_id"]: 1.0})

    result = sess.call("get_history", filter={"stream": "tools"})

    assert result.data["n_events"] == 1
    assert result.data["events"][0]["kind"] == "advisory"


def test_a_stream_nobody_subscribed_to_is_not_recorded(sess: Session):
    """Cheap where it matters: at the realistic tier a beacon feed nobody opened
    would be the most expensive thing in the loop."""
    sess._world.segments.rebeacon()
    sess.advance(1.0)

    assert len(sess.events) == 0
