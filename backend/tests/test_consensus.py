"""Consensus tests for Netproof MVP (agent 18/50).

What this tests
---------------
Three independent LLM runs of the same circuit should be reconciled
into one accepted netlist, instead of trusting any single run:

* three runs agree -> accepted;
* same graph under different node labels (``n1``/``n2`` vs ``a``/``b``)
  -> accepted as the same connectivity;
* value majority wins, else the median;
* a component seen in only 1 of 3 runs -> uncertain, NOT in the netlist;
* a 2-vs-1 type disagreement -> the majority type wins.

Fake LLM outputs only: the "runs" below are plain ``Netlist`` objects,
standing in for three already-parsed LLM outputs, plus one test that
builds the runs through ``llm.design`` with a fake client (queued JSON
replies). No network, no API key, no ngspice anywhere in this file.

Assumptions (``pipeline.py`` does not exist yet; ``llm.py`` landed
mid-task from a parallel agent)
-----------------------------------------------------------
Checked at task start 2026-11-15: ``~/workspace/netproof/backend/netproof/``
contained only ``__init__.py``, ``ir.py`` and ``parts.py``. There was
no ``pipeline.py``, no ``llm.py``, no ``run_image``, and no consensus
function anywhere in the repo. During this task parallel agents added
``llm.py`` (``read_schematic`` / ``design`` / ``repair``, returning
``Netlist``; no consensus helper), ``bom.py``, ``rules.py`` and
``spice.py``; ``pipeline.py`` still does not exist, and a repo-wide
grep finds "consensus" only in this test file. So there is still no
production consensus function to import. This file was NOT allowed to
create or edit those files (file owner: this test file only).

Therefore this file does two things, in priority order:

1. It tries to import a production consensus helper from
   ``netproof.pipeline`` (``consensus_netlists`` first, then
   ``consensus``, ``build_consensus``, ``run_consensus``) or from
   ``netproof.consensus``. If one exists, the tests exercise it through
   the ``_call_consensus`` adapter, which accepts a ``(netlist,
   uncertain)`` tuple, an object with ``.netlist`` / ``.uncertain``
   attributes, or a bare ``Netlist``.
2. Until that lands, it falls back to ``_consensus_reference``, a
   spec-faithful reference implementation defined in this file ONLY so
   the specified behaviour is executable and pinned down now. It is
   not production code and must be replaced by the real pipeline
   implementation; when that happens the tests will automatically
   switch to it (step 1) and the reference becomes dead code that can
   be deleted.

Reference semantics (the spec this file pins down)
--------------------------------------------------
* Input: a list of ``Netlist`` runs (normally 3).
* Node labels are canonicalised per run before any comparison, so a
  relabel does not look like a different circuit. Ground ``"0"`` is
  never renamed. Any other node gets a canonical label from the sorted
  tuple of component ids incident to it (ids only, not types/values,
  so a value or type disagreement cannot break the relabelling). The
  accepted netlist reports nodes in the first run's original labels,
  so an all-agree input comes back literally unchanged.
* A component id seen in >= 2 runs is included; seen in exactly 1 run
  it is ``uncertain`` and excluded from the netlist. A 1-1-1 type tie
  (three different types, no majority) is also uncertain/excluded.
* Type: majority type among the runs containing the id wins (2 vs 1).
* Value: voted on among the runs carrying the winning type only, so a
  minority type's value can never leak in. A value held by strictly
  more runs than any other value wins; otherwise the median
  (``statistics.median``; for two distinct values that is their mean).
* Nodes / rating / colour: majority among the winning-type runs,
  ties broken by run order.
* ``uncertain`` is a sorted list of plain component ids, matching the
  ``Report.uncertain: list[str]`` shape in ``ir.py``.

Known limits of the reference canonicalisation: nodes with identical
incident-id signatures (perfectly symmetric nodes) cannot be told
apart by this signature alone and are ordered by their original name,
so a relabel of a symmetric circuit could misalign. The fixtures here
avoid symmetry; a production implementation that needs symmetric
circuits should use proper graph canonicalisation.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass

import pytest

from netproof.ir import Component, ComponentType, Netlist

# ---------------------------------------------------------------------------
# Production import (preferred) / reference fallback
# ---------------------------------------------------------------------------

_PROD_CONSENSUS = None
for _mod, _names in (
    ("netproof.pipeline", ("consensus_netlists", "consensus", "build_consensus", "run_consensus")),
    ("netproof.consensus", ("consensus_netlists", "consensus", "build_consensus")),
):
    try:
        _m = __import__(_mod, fromlist=list(_names))
    except ImportError:
        continue
    for _name in _names:
        if hasattr(_m, _name):
            _PROD_CONSENSUS = getattr(_m, _name)
            break
    if _PROD_CONSENSUS is not None:
        break


@dataclass(frozen=True)
class ConsensusResult:
    """Reference result shape: accepted netlist + uncertain component ids."""

    netlist: Netlist
    uncertain: list[str]


def _canonicalise(netlist: Netlist) -> tuple[Netlist, dict[str, str], dict[str, str]]:
    """Rename nodes to canonical labels, keeping ``"0"`` as ground.

    Returns ``(canonical_netlist, original -> canonical, canonical ->
    original)``. Two runs of the same graph under different labels
    canonicalise to the same netlist (up to the symmetry limit noted
    in the module docstring).
    """
    incident: dict[str, tuple[str, ...]] = {}
    for comp in netlist.components:
        for node in comp.nodes:
            incident.setdefault(node, []).append(comp.id)
    signatures = {node: tuple(sorted(ids)) for node, ids in incident.items()}
    non_ground = sorted(
        (n for n in netlist.node_names() if n != "0"),
        key=lambda n: (signatures[n], n),
    )
    mapping: dict[str, str] = {"0": "0"}
    for i, node in enumerate(non_ground, start=1):
        mapping[node] = f"c{i}"
    inverse = {canon: orig for orig, canon in mapping.items()}
    canon_components = [
        Component(
            id=comp.id,
            type=comp.type,
            value=comp.value,
            nodes=[mapping[n] for n in comp.nodes],
            rating_watts=comp.rating_watts,
            color=comp.color,
        )
        for comp in netlist.components
    ]
    return Netlist(components=canon_components, title=netlist.title), mapping, inverse


def _majority_or_median(values: list[float]) -> float:
    """Most-common value wins if it strictly beats every other value.

    Otherwise the median (for two distinct values, their mean).
    """
    counts = Counter(values)
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] > ranked[1][1] and ranked[0][1] >= 2:
        return ranked[0][0]
    if len(ranked) == 1:
        return ranked[0][0]
    return float(statistics.median(values))


def _consensus_reference(runs: list[Netlist]) -> ConsensusResult:
    """Spec-faithful reference consensus (see module docstring)."""
    if not runs:
        raise ValueError("consensus needs at least one run")
    canon_runs = [_canonicalise(run) for run in runs]

    ids: list[str] = []
    for canon, _, _ in canon_runs:
        for comp in canon.components:
            if comp.id not in ids:
                ids.append(comp.id)

    accepted: list[Component] = []
    uncertain: list[str] = []
    for comp_id in ids:
        present = [
            (canon, inverse)
            for canon, _, inverse in canon_runs
            if canon.get(comp_id) is not None
        ]
        if len(present) < 2:
            uncertain.append(comp_id)
            continue
        type_counts = Counter(
            canon.get(comp_id).type for canon, _ in present  # type: ignore[union-attr]
        )
        ranked_types = type_counts.most_common()
        if len(ranked_types) > 1 and ranked_types[0][1] == ranked_types[1][1]:
            uncertain.append(comp_id)  # no type majority (e.g. 1-1-1)
            continue
        winning_type = ranked_types[0][0]
        majority = [
            (canon, inverse)
            for canon, inverse in present
            if canon.get(comp_id).type == winning_type  # type: ignore[union-attr]
        ]
        majority_comps = [canon.get(comp_id) for canon, _ in majority]  # type: ignore[union-attr]
        value = _majority_or_median([c.value for c in majority_comps])  # type: ignore[union-attr]
        node_tuples = Counter(tuple(c.nodes) for c in majority_comps)  # type: ignore[union-attr]
        canon_nodes = node_tuples.most_common(1)[0][0]
        # Report nodes in the first run's original labels where possible.
        inverse0 = majority[0][1]
        out_nodes = [inverse0.get(n, n) for n in canon_nodes]
        rating = Counter(c.rating_watts for c in majority_comps).most_common(1)[0][0]  # type: ignore[union-attr]
        color = Counter(c.color for c in majority_comps).most_common(1)[0][0]  # type: ignore[union-attr]
        accepted.append(
            Component(
                id=comp_id,
                type=winning_type,
                value=value,
                nodes=list(out_nodes),
                rating_watts=rating,
                color=color,
            )
        )
    accepted.sort(key=lambda c: c.id)
    return ConsensusResult(
        netlist=Netlist(components=accepted, title="consensus"),
        uncertain=sorted(uncertain),
    )


def _call_consensus(runs: list[Netlist]) -> tuple[Netlist, list[str]]:
    """Call the production consensus if present, else the reference.

    Normalises the return shape to ``(Netlist, uncertain_ids)``.
    """
    if _PROD_CONSENSUS is None:
        result = _consensus_reference(runs)
        return result.netlist, result.uncertain
    result = _PROD_CONSENSUS(runs)
    if isinstance(result, tuple) and len(result) == 2:
        netlist, uncertain = result
        return netlist, list(uncertain)
    if isinstance(result, dict):
        netlist = result.get("netlist") or result.get("accepted") or result.get("netlist_used")
        uncertain = result.get("uncertain") or result.get("uncertain_ids") or []
        return netlist, list(uncertain)
    if isinstance(result, Netlist):
        return result, []
    netlist = (
        getattr(result, "netlist", None)
        or getattr(result, "accepted", None)
        or getattr(result, "netlist_used", None)
    )
    uncertain = (
        getattr(result, "uncertain", None)
        or getattr(result, "uncertain_ids", None)
        or []
    )
    return netlist, list(uncertain)


# ---------------------------------------------------------------------------
# Fake LLM outputs: three parsed runs of a 9 V source + resistor + LED
# ---------------------------------------------------------------------------

# Run 1 labels: n1 (source/resistor), n2 (resistor/LED).
# Run 2 labels: a, b. Run 3 labels: x, y. Same graph in all three.


def _vsrc(nodes=("n1", "0")) -> Component:
    return Component(id="V1", type=ComponentType.VOLTAGE_SOURCE, value=9.0, nodes=list(nodes))


def _resistor(value: float = 100.0, nodes=("n1", "n2"), cid: str = "R1") -> Component:
    return Component(id=cid, type=ComponentType.RESISTOR, value=value, nodes=list(nodes))


def _led(nodes=("n2", "0"), cid: str = "D1", value: float = 2.0) -> Component:
    return Component(id=cid, type=ComponentType.LED, value=value, nodes=list(nodes))


def _capacitor(value: float = 1e-6, nodes=("n2", "0"), cid: str = "C1") -> Component:
    return Component(id=cid, type=ComponentType.CAPACITOR, value=value, nodes=list(nodes))


def _base_components(nodes1: str = "n1", nodes2: str = "n2") -> list[Component]:
    return [
        _vsrc(nodes=(nodes1, "0")),
        _resistor(nodes=(nodes1, nodes2)),
        _led(nodes=(nodes2, "0")),
    ]


def _run(nodes1: str = "n1", nodes2: str = "n2", resistor_value: float = 100.0) -> Netlist:
    comps = _base_components(nodes1, nodes2)
    comps[1] = _resistor(value=resistor_value, nodes=(nodes1, nodes2))
    return Netlist(components=comps)


def _by_id(netlist: Netlist) -> dict[str, Component]:
    return {comp.id: comp for comp in netlist.components}


def _canonical_edges(netlist: Netlist) -> list[tuple]:
    """Connectivity fingerprint: (id, type, value, canonical nodes)."""
    canon, _, _ = _canonicalise(netlist)
    return sorted(
        (c.id, c.type.value, c.value, tuple(c.nodes)) for c in canon.components
    )


def _assert_uncertain_contains(uncertain: list[str], comp_id: str) -> None:
    assert any(comp_id in entry for entry in uncertain), (
        f"expected {comp_id!r} in uncertain, got {uncertain!r}"
    )


# ---------------------------------------------------------------------------
# Fake LLM client (llm.py shape: client.messages.create(...) ->
# response with .content[0].text), used by one end-to-end test below
# ---------------------------------------------------------------------------

import json as _json
from types import SimpleNamespace as _SimpleNamespace


class _FakeMessages:
    """Queued canned JSON replies, one per ``messages.create`` call."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._replies.pop(0)
        return _SimpleNamespace(content=[_SimpleNamespace(text=text)])


class _FakeLlmClient:
    def __init__(self, replies: list[str]):
        self.messages = _FakeMessages(replies)


def _llm_json(nodes1: str, nodes2: str, resistor_value: float) -> str:
    """One fake LLM reply: strict JSON netlist, as llm.py demands."""
    return _json.dumps(
        {
            "components": [
                {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": [nodes1, "0"]},
                {"id": "R1", "type": "resistor", "value": resistor_value, "nodes": [nodes1, nodes2]},
                {"id": "D1", "type": "led", "value": 2.0, "nodes": [nodes2, "0"], "color": "red"},
            ],
            "title": "9V LED circuit",
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_three_runs_agree_accepted():
    """Three identical runs -> every component accepted, nothing uncertain."""
    runs = [_run(), _run(), _run()]
    netlist, uncertain = _call_consensus(runs)

    assert uncertain == []
    assert set(_by_id(netlist)) == {"V1", "R1", "D1"}
    assert _by_id(netlist)["V1"].value == 9.0
    assert _by_id(netlist)["R1"].value == 100.0
    assert _by_id(netlist)["R1"].type is ComponentType.RESISTOR
    assert _by_id(netlist)["D1"].value == 2.0
    # Ground preserved, original labels preserved on full agreement.
    assert netlist.has_ground()
    assert _by_id(netlist)["R1"].nodes == ["n1", "n2"]


def test_relabelled_nodes_accepted_as_same_connectivity():
    """n1/n2 vs a/b vs x/y: same graph -> accepted, same connectivity."""
    run1 = _run("n1", "n2")
    run2 = _run("a", "b")
    run3 = _run("x", "y")
    netlist, uncertain = _call_consensus([run1, run2, run3])

    assert uncertain == []
    assert set(_by_id(netlist)) == {"V1", "R1", "D1"}
    assert netlist.has_ground()
    # Connectivity, not literal labels, is what must match every run.
    assert _canonical_edges(netlist) == _canonical_edges(run1)
    assert _canonical_edges(netlist) == _canonical_edges(run2)
    assert _canonical_edges(netlist) == _canonical_edges(run3)


def test_value_majority_wins():
    """R1 = 100, 100, 220 ohms -> 100 (majority beats the outlier)."""
    runs = [_run(resistor_value=100.0), _run(resistor_value=100.0), _run(resistor_value=220.0)]
    netlist, uncertain = _call_consensus(runs)

    assert uncertain == []
    assert _by_id(netlist)["R1"].value == 100.0
    assert set(_by_id(netlist)) == {"V1", "R1", "D1"}


def test_value_no_majority_uses_median():
    """R1 = 100, 220, 330 ohms, no majority -> median 220."""
    runs = [_run(resistor_value=100.0), _run(resistor_value=220.0), _run(resistor_value=330.0)]
    netlist, uncertain = _call_consensus(runs)

    assert uncertain == []
    assert _by_id(netlist)["R1"].value == 220.0


def test_value_two_runs_median_is_mean_of_two():
    """Component in 2 runs with values 100 and 220 -> median 160."""
    run1 = _run(resistor_value=100.0)
    run2 = _run(resistor_value=220.0)
    netlist, uncertain = _call_consensus([run1, run2])

    assert uncertain == []
    assert _by_id(netlist)["R1"].value == pytest.approx(160.0)


def test_component_seen_once_is_uncertain_not_in_netlist():
    """C1 appears in only 1 of 3 runs -> uncertain, excluded from netlist."""
    run1, run2 = _run(), _run()
    run3_comps = _base_components()
    run3_comps.append(_capacitor())
    run3 = Netlist(components=run3_comps)

    netlist, uncertain = _call_consensus([run1, run2, run3])

    assert netlist.get("C1") is None
    _assert_uncertain_contains(uncertain, "C1")
    # The agreed components are still accepted.
    assert set(_by_id(netlist)) == {"V1", "R1", "D1"}


def test_component_seen_twice_is_included():
    """C1 appears in 2 of 3 runs -> included, not uncertain (boundary)."""
    run1_comps = _base_components()
    run1_comps.append(_capacitor())
    run2_comps = _base_components()
    run2_comps.append(_capacitor())
    netlist, uncertain = _call_consensus(
        [Netlist(components=run1_comps), Netlist(components=run2_comps), _run()]
    )

    assert netlist.get("C1") is not None
    assert _by_id(netlist)["C1"].value == pytest.approx(1e-6)
    assert not any("C1" in entry for entry in uncertain)


def test_type_majority_wins_two_vs_one():
    """R1 is a resistor in 2 runs, a capacitor in 1 -> resistor wins."""
    run1 = _run()
    run2 = _run()
    run3_comps = [
        _vsrc(),
        _capacitor(value=1e-6, nodes=("n1", "n2"), cid="R1"),
        _led(),
    ]
    run3 = Netlist(components=run3_comps)

    netlist, uncertain = _call_consensus([run1, run2, run3])

    assert uncertain == []
    assert _by_id(netlist)["R1"].type is ComponentType.RESISTOR
    assert _by_id(netlist)["R1"].value == 100.0


def test_type_majority_value_comes_from_majority_type():
    """Majority-type value vote must ignore the minority type's value.

    Resistors 100 / 220 ohms vs a 1 uF capacitor: the answer must be a
    resistor whose value comes from the resistor runs (median 160),
    never the capacitor's 1e-6.
    """
    run1 = _run(resistor_value=100.0)
    run2 = _run(resistor_value=220.0)
    run3 = Netlist(
        components=[
            _vsrc(),
            _capacitor(value=1e-6, nodes=("n1", "n2"), cid="R1"),
            _led(),
        ]
    )
    netlist, uncertain = _call_consensus([run1, run2, run3])

    assert _by_id(netlist)["R1"].type is ComponentType.RESISTOR
    assert _by_id(netlist)["R1"].value == pytest.approx(160.0)


def test_relabelled_nodes_with_value_disagreement():
    """Relabel + value disagreement combined: connectivity from the
    relabel, value from the majority (100, 100, 220 -> 100)."""
    run1 = _run("n1", "n2", resistor_value=100.0)
    run2 = _run("a", "b", resistor_value=100.0)
    run3 = _run("x", "y", resistor_value=220.0)
    netlist, uncertain = _call_consensus([run1, run2, run3])

    assert uncertain == []
    assert _by_id(netlist)["R1"].value == 100.0
    assert _canonical_edges(netlist) == _canonical_edges(run1)


def test_all_three_types_differ_is_uncertain():
    """R1 as resistor / capacitor / diode (1-1-1, no majority) ->
    uncertain and excluded, the rest still accepted."""
    run1 = _run()
    run2 = Netlist(
        components=[
            _vsrc(),
            _capacitor(value=1e-6, nodes=("n1", "n2"), cid="R1"),
            _led(),
        ]
    )
    run3 = Netlist(
        components=[
            _vsrc(),
            Component(id="R1", type=ComponentType.DIODE, value=0.7, nodes=["n1", "n2"]),
            _led(),
        ]
    )
    netlist, uncertain = _call_consensus([run1, run2, run3])

    assert netlist.get("R1") is None
    _assert_uncertain_contains(uncertain, "R1")
    assert set(_by_id(netlist)) == {"V1", "D1"}


def test_two_runs_agree_accepted():
    """Two agreeing runs -> accepted (inclusion needs >= 2 sightings)."""
    netlist, uncertain = _call_consensus([_run(), _run()])

    assert uncertain == []
    assert set(_by_id(netlist)) == {"V1", "R1", "D1"}
    assert _by_id(netlist)["R1"].value == 100.0


def test_runs_built_from_fake_llm_json():
    """End-to-end with fake LLM outputs: three ``llm.design`` calls
    against a fake client (queued JSON replies, no network/key) feed
    consensus. Run 2 is relabelled (a/b) and run 3 disagrees on R1's
    value, so the accepted netlist must keep the relabelled
    connectivity and the majority value 100."""
    llm = pytest.importorskip("netproof.llm", reason="llm.py not present")
    client = _FakeLlmClient(
        [
            _llm_json("n1", "n2", 100.0),
            _llm_json("a", "b", 100.0),
            _llm_json("x", "y", 220.0),
        ]
    )
    runs = [llm.design("light a red LED from a 9V battery", client=client) for _ in range(3)]
    assert len(client.messages.calls) == 3

    netlist, uncertain = _call_consensus(runs)

    assert uncertain == []
    assert set(_by_id(netlist)) == {"V1", "R1", "D1"}
    assert _by_id(netlist)["R1"].value == 100.0
    assert netlist.has_ground()
    assert _canonical_edges(netlist) == _canonical_edges(runs[0])
