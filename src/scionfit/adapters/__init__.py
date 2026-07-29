"""Adapters onto the existing SCION tooling.

scionfit does not reimplement SCION.  Each adapter maps an external source
into the types in ``scionfit.interface`` so the same probes and the same
scenario definitions run at every level of fidelity:

  tier 0  scionpathml   real SCIONLab measurements, replayed
  tier 1  (built in)    analytical world, milliseconds, thousands of runs
  tier 2  dqnsim        BRITE topology + simulated beaconing
  tier 3  testbed       a real SCION stack, shaped through linkd

All three external adapters are stubs at v0.1: the mapping is specified and
the call shape is fixed, but each needs to be validated against the actual
upstream repository before it is trusted.  See docs/ADAPTERS.md.
"""
from . import dqnsim, scionpathml, testbed

__all__ = ["scionpathml", "dqnsim", "testbed"]
