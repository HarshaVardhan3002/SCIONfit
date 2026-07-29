"""Substrate. The world as it actually is.

Topology, path segments and their cryptographic churn, link state, hosts,
clock, scenario engine, trace hashing. numpy-backed, array-indexed, no Python
object per path in the hot loop.

Nothing here may import a front-end or the exposure layer. The dependency
direction is ``core <- exposure <- {conformance, bench, gym, agent, deploy}``
and it is enforced by ``lint-imports`` in CI, not by convention.

Empty except for :mod:`scionarena.core.trace` until M1.
"""
