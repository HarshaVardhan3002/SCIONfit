# Adapter status

`scionarena` does not reimplement SCION. It maps external sources onto the types
in `scionarena.exposure.contracts` so that one scenario definition and one probe
suite run at every level of fidelity.

| Tier | Module | Upstream | Status |
|---|---|---|---|
| 0 | `backends.scionpathml` | [ScionPathML](https://arxiv.org/abs/2509.07154) | **unvalidated stub** |
| 1 | `backends.analytical` | built in | working; rewritten in M1 |
| 2 | `backends.dqnsim` | [scion-dqn-sim](https://github.com/netsys-lab/scion-dqn-sim) | **unvalidated stub** |
| 3 | `backends.testbed` | [ietf-scion-testbed](https://github.com/netsys-lab/ietf-scion-testbed) | **unvalidated stub** |

**Unvalidated means the type mapping is written and the call shape is fixed,
but it has never been run against a real export or a live instance.** Do not
report numbers from these tiers until the corresponding checklist below is
done.

## Tier 0 — ScionPathML

Real SCIONLab measurements: RTT, loss, jitter, bandwidth, per-hop RTT, path
fingerprints, collected every 30 minutes across four ASes over four weeks.

To validate:

- [ ] Obtain a real CSV export and check `COLUMN_MAP` against its header row.
- [ ] Confirm how the toolkit represents a missing measurement. If it writes
      `0` rather than an empty field, probe R3 becomes untestable on tier 0 and
      we must say so rather than silently mapping zeros to `None`.
- [ ] Confirm the path fingerprint is stable across measurement cycles, since
      we use it as `path_id`.
- [ ] Decide whether their five benchmark tasks are wrapped here or left alone.
      Their Task 4 (multi-objective recommendation) scores whether the
      **top-ranked** path met a QoE target, which rewards exactly the
      concentration behaviour probe R8 flags. Worth wrapping specifically so
      the two scores can be shown side by side.

## Tier 2 — scion-dqn-sim

BRITE topology generation plus a simulated SCION control plane: beaconing, path
discovery, segment registration. Also gravity/uniform traffic with diurnal and
weekly patterns, and six baseline selectors.

To validate:

- [ ] Clone and run `evaluation/01_generate_topology.py`; check the real
      `scion_topology.json` schema against `load_topology`.
- [ ] Upstream is marked work in progress. Pin a commit.
- [ ] Their evaluation uses a single source-destination pair and a single
      agent. Confirm whether their traffic model can be driven with a
      population of agents, or whether we drive it externally.

## Tier 3 — ietf-scion-testbed

A real 12-AS SCION network as Proxmox LXC containers: border routers, control
service, sciond, plus `linkd`, a per-AS shaping daemon applying tc netem/tbf
over REST.

This is the tier that makes the ladder worth building. The same scenario that
perturbs a link in the tier-1 analytical world can perturb a real inter-AS link
here, which turns the sim-to-real gap into something measurable.

To validate:

- [ ] Read `linkd`'s actual REST surface and correct the endpoints in
      `backends/testbed.py`. Everything there is inferred from the README.
- [ ] Establish what hardware access we have. This tier needs a Proxmox host
      and will never run in CI.
- [ ] Check whether ID-INT path tracing and the border-router RTT/traffic
      metrics can be read as `Observation`s. If so, tier 3 gives us real
      telemetry under controlled perturbation, which is the strongest evidence
      any of this can produce.
- [ ] Define the scenario schema first, then implement `scenario_to_shapes`.
      Portability across tiers is the whole point and it depends on that schema
      being fixed before the adapters harden.
