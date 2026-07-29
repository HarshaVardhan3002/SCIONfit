"""Backends: one substrate, four levels of fidelity.

scionarena does not reimplement SCION.  Each backend maps an external source
into the types in ``scionarena.exposure.contracts`` so the same scenario runs
at every tier:

  tier 0  scionpathml   real SCIONLab measurements, replayed
  tier 1  analytical    analytical world, milliseconds, thousands of runs
  tier 2  dqnsim        BRITE topology + simulated beaconing
  tier 3  testbed       a real SCION stack, shaped through linkd

The three external backends are stubs at v0.1: the mapping is specified and
the call shape is fixed, but each needs to be validated against the actual
upstream repository before it is trusted.  See docs/ADAPTERS.md.

``analytical`` is not imported here: it is the 6-path toy inherited from v0.1
and it is rewritten against the corrected domain model in M1.
"""

from . import dqnsim, scionpathml, testbed

__all__ = ["scionpathml", "dqnsim", "testbed"]
