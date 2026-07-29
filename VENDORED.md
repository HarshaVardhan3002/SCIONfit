# Vendored and pinned upstreams

We do not reinvent SCION. Anything taken from an upstream is listed here with the pinned
revision and what was changed. An entry with no pinned revision is not vendored yet.

**Rule.** If an upstream does the job, depend on it or vendor it with a pinned hash and an
entry here. Reimplementing gets an ADR that says what the upstream cannot express. "It was
easier" is not a justification.

| upstream | used as | pinned at | vendored? | changes |
|---|---|---|---|---|
| [ietf-scion-testbed](https://github.com/scionassociation/ietf-scion-testbed) | tier-3 backend; `linkd` link shaping; ID-INT tracing | — | no | needs a Proxmox host — see Q4 |
| [scion-dqn-sim](https://github.com/netsys-lab) | tier-2 backend; BRITE topology; simulated beaconing | — | no | 4 commits, WIP. Pin a hash before depending on it; expect to fork |
| [ScionPathML](https://github.com/netsys-lab) | tier-0 replay traces; QoE profiles; calibration of the tier-1 link model | — | no | 4 ASes only, homogeneous |
| SEED emulator | alternative tier-3 if testbed hardware is unavailable | — | not in this repo | heavier than `linkd` for shaping |
| Gymnasium | `gym/` front-end API | — | no | dependency, behind an extra, from M7 |
| CAIDA AS-relationships | M1 topology generator input | — | no | BGP concepts; the mapping onto ISD core / parent-child / peering is `ASSUMPTION(Q5)` |

Nothing is vendored as of M0. The adapters in `src/scionarena/backends/` are stubs written
against the *documented* shape of each upstream and **none has been validated against the
actual repository**. See `docs/ADAPTERS.md`.
