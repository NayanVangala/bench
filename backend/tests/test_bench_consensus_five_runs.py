"""Bench backlog #9 (3/3): consensus over 5 runs is a MAJORITY rule.

Intended behaviour: with 5 runs a part seen in 2/5 must be EXCLUDED
(uncertain) and a part seen in 3/5 must be INCLUDED.

Finding (read before writing this test): the handoff premise was wrong
about the current code. ``backend/netproof/pipeline.py``,
``consensus_netlists``, line 843, is NOT an absolute ``>= 2`` threshold:

    # A part must be seen by a majority of the readers (2 of 3, 3 of 5),
    # and never fewer than 2.
    if len(present) < max(2, len(runs) // 2 + 1):
        uncertain.append(comp_id)
        continue

For 5 runs that is ``max(2, 5 // 2 + 1) = 3``: 2/5 is excluded, 3/5 is
included. So this test documents the intended majority behaviour AND
PASSES against the current code. (The stale docstrings still say
"seen in >= 2 runs it is included" — pipeline.py module docstring and
the ``consensus_netlists`` docstring — but the code itself is majority.
No source file was edited here, per the brief; the coordinator may
decide separately whether the docstrings need fixing.)
"""
from __future__ import annotations

from netproof.ir import Component, ComponentType, Netlist
from netproof.pipeline import consensus_netlists


def _vsrc():
    return Component(
        id="V1", type=ComponentType.VOLTAGE_SOURCE, value=9.0, nodes=["n1", "0"]
    )


def _resistor():
    return Component(
        id="R1", type=ComponentType.RESISTOR, value=100.0, nodes=["n1", "n2"]
    )


def _led():
    return Component(
        id="D1", type=ComponentType.LED, value=2.0, nodes=["n2", "0"]
    )


def _capacitor(cid):
    return Component(
        id=cid, type=ComponentType.CAPACITOR, value=1e-6, nodes=["n2", "0"]
    )


def _run(extra_ids):
    comps = [_vsrc(), _resistor(), _led()]
    comps += [_capacitor(cid) for cid in extra_ids]
    return Netlist(components=comps)


def test_consensus_five_runs_majority_threshold():
    """2/5 excluded, 3/5 included.

    C1 appears in runs 1-2 only (2/5, a minority) -> uncertain, excluded.
    C2 appears in runs 1-3 (3/5, a majority) -> included, not uncertain.
    The always-present V1/R1/D1 are included in both cases.
    """
    runs = [
        _run(["C1", "C2"]),  # run 1: both
        _run(["C1", "C2"]),  # run 2: both
        _run(["C2"]),  # run 3: C2 only -> C2 reaches 3/5, C1 stays 2/5
        _run([]),  # run 4: neither
        _run([]),  # run 5: neither
    ]
    netlist, uncertain = consensus_netlists(runs)

    assert netlist.get("C1") is None, "2/5 part C1 must be excluded"
    assert "C1" in uncertain, f"2/5 part C1 must be uncertain, got {uncertain!r}"
    assert netlist.get("C2") is not None, "3/5 part C2 must be included"
    assert netlist.get("C2").value == 1e-6
    assert "C2" not in uncertain, f"3/5 part C2 must not be uncertain, got {uncertain!r}"
    assert {c.id for c in netlist.components} == {"V1", "R1", "D1", "C2"}
