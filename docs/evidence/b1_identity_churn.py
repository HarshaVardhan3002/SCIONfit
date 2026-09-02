"""What the corrected beacon cadence does to identity churn.

B1: ``BeaconPolicy.interval_s`` defaulted to 300 s and upstream re-beacons every
5 s. This measures the consequence rather than restating the constant -- how many
distinct identifiers a model keyed on ``path_id`` would see for one unchanging set
of paths, at both cadences.

Run it from the repo root::

    python docs/evidence/b1_identity_churn.py

The structural ids are identical in both runs and that is the point: the interface
sequences never move. Q1 is resolved -- the deployed SCION fingerprint *is* the
structural one -- so this measures what a model keyed on the wrong identifier
throws away, not what SCION does to a correct one.
"""

from __future__ import annotations

from scionarena.core.scenario import Scenario, Substrate, TopologySpec
from scionarena.core.segments import BeaconPolicy

DURATION_S = 6_000.0
STEP_S = 5.0
SEED = 7
CADENCES = (("assumed (M1-M4)", 300.0), ("upstream (corrected)", 5.0))


def busiest_scope(world: Substrate) -> tuple[int, int]:
    """The (src, dst) with the most paths. A scope with one path shows nothing."""
    best, most = (0, 1), -1
    for a in range(world.topology.n_ases):
        for b in range(world.topology.n_ases):
            if a != b and (n := len(world.paths_for(a, b))) > most:
                best, most = (a, b), n
    return best


def churn(interval_s: float) -> tuple[int, int, int, int]:
    """Distinct identifiers, path count, re-signings, structural ids."""
    world = Scenario(
        name=f"churn-{interval_s:g}",
        seed=SEED,
        topology=TopologySpec(tier="smoke"),
        duration_s=DURATION_S,
        step_s=STEP_S,
        identity_policy="crypto_bound",
        beaconing=BeaconPolicy(interval_s=interval_s),
    ).build()
    src, dst = busiest_scope(world)

    paths = world.paths_for(src, dst)
    structural = {p.structural_id for p in paths}
    seen = set(world.path_ids(paths))
    resignings = 0

    elapsed = 0.0
    while elapsed < DURATION_S:
        world.step(STEP_S)
        elapsed += STEP_S
        resignings += len(world.segments.drain_resigned())
        seen.update(world.path_ids(world.paths_for(src, dst)))

    still = {p.structural_id for p in world.paths_for(src, dst)}
    assert still == structural, "the interface sequences moved, which would invalidate this"
    return len(seen), len(paths), resignings, len(structural)


def main() -> None:
    row = "{:<22} {:>11} {:>13} {:>7} {:>13} {:>12}"
    print(
        row.format(
            "beacon interval", "interval_s", "identifiers", "paths", "re-signings", "structural"
        )
    )
    print("-" * 84)
    results = []
    for label, interval_s in CADENCES:
        ids, paths, resignings, structural = churn(interval_s)
        results.append((ids, resignings))
        print(
            row.format(
                label, f"{interval_s:g}s", ids, paths, resignings, f"{structural}/{structural}"
            )
        )

    (base_ids, base_signings), (now_ids, now_signings) = results
    print()
    print(f"{DURATION_S:g}s, identity_policy=crypto_bound, seed {SEED}, smoke tier")
    print(
        f"a model keyed on path_id sees {now_ids} distinct identifiers where it saw "
        f"{base_ids}: {now_ids / base_ids:.1f}x"
    )
    print(
        f"re-signings {now_signings} against {base_signings}: {now_signings / base_signings:.0f}x"
    )
    print()
    print("Structural ids are unchanged in both. The interface sequences never moved, so")
    print("this is what a model keyed on the wrong identifier loses -- not what SCION does.")


if __name__ == "__main__":
    main()
