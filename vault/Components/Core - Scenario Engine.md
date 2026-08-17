# Core - Scenario Engine (`scionarena.core.scenario`)

> [scenario.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/scenario.py)

---

## 📌 Role & Responsibilities
Defines portable scenario specifications and orchestrates the live simulation world ([Substrate](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/scenario.py#L427)).

## 📜 Portable Scenario Specification
A [`Scenario`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/scenario.py#L249) instance describes:
* `topology`: Scale tier (`smoke`, `dev`, `realistic`, `stress`) or CAIDA/DQN-Sim topology source.
* `duration_s`: Total simulated episode duration.
* `step_s`: Base world simulation tick rate.
* `identity_policy`: Path ID aliasing policy (`structural` vs `crypto_bound`).
* `link_params` & `background_params`: Physics and congestion constants.
* `timeline`: Sequence of scheduled disruptions ([TimelineEvent](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/scenario.py#L111)).

## ⚡ Supported Timeline Events
* `link_degrade`: Degrades link capacity/health multiplier (e.g. fiber cut or degradation).
* `link_restore`: Restores degraded interface.
* `as_policy_filter`: Segments vanish from path lookup responses without physical link failure.
* `demand_surge`: Injects exogenous scheduled load spikes on specific scopes.
* `topology_change`: Rare, scheduled topological restructuring.
* `advisory_apply`: Published model distribution reaching host population after decision delay.
