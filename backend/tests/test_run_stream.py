"""``_run_stream`` pipeline-wiring regression tests (P4 gap b).

File owner: backend/tests/test_run_stream.py ONLY.

Drives the real ``server._run_stream`` (the SSE adapter behind
``/run-image`` and ``/run-goal``) with the LLM stubbed and the
analytic fake simulator installed, and pins the wiring the mock
streams cannot prove:

* image: ``llm.read_schematic`` is called exactly 3 times (3
  parallel reads), the >= 2/3 consensus excludes a component seen
  in only 1 run (it lands in ``uncertain``, not in the netlist),
  and the terminal ``result`` event carries ``done: true``.
* goal: ``llm.design`` is called 4 times (one per strategist in
  the design team, bench #8), the returned netlist is the PASS
  candidate, not the first one, the Designer / Judge / Physics
  progress events carry the dock dialect (``state`` / ``score`` /
  ``calls`` / ``physics``), and the terminal ``result`` event
  carries the ``leaderboard`` (winner first).

No network, no LLM key, no ngspice: ``llm.read_schematic`` /
``llm.design`` are monkeypatched stubs and the simulator is the
same analytic series-loop fake used by test_endpoint_drift.py
(node voltages keyed to the netlist's own node names).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netproof import llm as llm_module
from netproof import server as server_module
from netproof.ir import Component, Netlist


class _AnalyticSeriesSimulator:
    """Fake simulator for the 9 V source / resistor / LED loop.

    Returns node voltages keyed to the netlist's own node names and
    lowercase branch-current / power keys, exactly like
    ``spice.parse_op_output`` does (ngspice lowercases keys).
    """

    def run(self, netlist: Netlist) -> dict:
        source = next(
            c for c in netlist.components if c.type == "voltage_source"
        )
        resistor = next(c for c in netlist.components if c.type == "resistor")
        led = next(c for c in netlist.components if c.type == "led")
        current = (source.value - led.value) / resistor.value
        node_voltages = {
            source.nodes[0]: source.value,
            source.nodes[1]: 0.0,
            led.nodes[0]: led.value,
            led.nodes[1]: 0.0,
        }
        return {
            "node_voltages": node_voltages,
            "branch_currents": {
                source.id.lower(): -current,
                resistor.id.lower(): current,
                led.id.lower(): current,
            },
            "power": {
                resistor.id.lower(): current**2 * resistor.value,
                led.id.lower(): current * led.value,
                source.id.lower(): -source.value * current,
            },
        }


@pytest.fixture(autouse=True)
def _fake_simulator():
    server_module.set_simulator(_AnalyticSeriesSimulator())
    yield
    server_module.set_simulator(None)


def _good_netlist() -> Netlist:
    return Netlist.model_validate(
        {
            "title": "good LED 9V",
            "components": [
                {"id": "V1", "type": "voltage_source", "value": 9.0,
                 "nodes": ["vcc", "0"]},
                {"id": "R1", "type": "resistor", "value": 470.0,
                 "nodes": ["vcc", "led"], "rating_watts": 0.25},
                {"id": "D1", "type": "led", "value": 2.0,
                 "nodes": ["led", "0"], "color": "red"},
            ],
        }
    )


def _overcurrent_netlist() -> Netlist:
    bad = _good_netlist()
    return Netlist(
        title="led_overcurrent",
        components=[
            Component.model_validate({**c.model_dump(), "value": 100.0})
            if c.id == "R1"
            else c
            for c in bad.components
        ],
    )


def _collect(kind: str, **kwargs) -> list[dict]:
    """Drive ``_run_stream`` to completion; return parsed SSE frames
    as ``[{"event": ..., "data": {...}}, ...]``."""
    frames: list[dict] = []

    async def _drive() -> None:
        async for event in server_module._run_stream(kind, mock=False, **kwargs):
            frames.append(
                {"event": event["event"], "data": json.loads(event["data"])}
            )

    asyncio.run(_drive())
    return frames


def _result(frames: list[dict]) -> dict:
    results = [f["data"] for f in frames if f["event"] == "result"]
    assert len(results) == 1, f"expected exactly one result event, got {len(results)}"
    return results[0]


def test_run_stream_image_three_reads_consensus_excludes_seen_once(
    monkeypatch: pytest.MonkeyPatch,
):
    """3 parallel reads; a component seen in only 1 run is excluded
    from the consensus netlist and reported in ``uncertain``."""
    reads: list[bytes] = []
    good = _good_netlist()
    # Third read hallucinates an extra capacitor leg (seen once).
    third = Netlist(
        title="good LED 9V",
        components=[
            *good.components,
            Component(
                id="C1",
                type="capacitor",
                value=1e-6,
                nodes=["vcc", "float1"],
            ),
        ],
    )
    script = iter([good, good, third])

    def fake_read(image_bytes, client=None):
        reads.append(bytes(image_bytes))
        return next(script)

    monkeypatch.setattr(llm_module, "read_schematic", fake_read)

    frames = _collect(
        "image", image_bytes=b"\x89PNG fake schematic", filename="s.png"
    )

    # Exactly 3 reads: _run_stream -> pipeline.run_image (3 parallel).
    assert len(reads) == 3
    assert all(b == b"\x89PNG fake schematic" for b in reads)
    # No error event; progress events never carry a netlist (R4).
    assert [f for f in frames if f["event"] == "error"] == []
    assert all(
        "netlist" not in f["data"] for f in frames if f["event"] == "progress"
    )
    result = _result(frames)
    assert result["done"] is True
    assert result["mock"] is False
    assert result["verdict"] == "PASS"
    # Consensus: C1 seen once -> excluded from the netlist, uncertain.
    used_ids = {c["id"] for c in result["netlist_used"]["components"]}
    assert used_ids == {"V1", "R1", "D1"}
    assert any("C1" in u for u in result["uncertain"])


def test_run_stream_image_result_carries_unmodeled(
    monkeypatch: pytest.MonkeyPatch,
):
    """The server's final ``result`` event must carry ``unmodeled``:
    the pipeline's terminal event is skipped by ``_run_stream``, so
    only the ``done`` branch can forward it (regression: it was
    silently dropped, and the UI read a field that never arrived)."""
    unmodeled = [{"id": "U1", "type": "IC", "note": "nRF52"}]
    good = _good_netlist()

    def fake_read(image_bytes, client=None):
        return (good, unmodeled)

    monkeypatch.setattr(llm_module, "read_schematic", fake_read)
    frames = _collect(
        "image", image_bytes=b"\x89PNG fake schematic", filename="s.png"
    )
    result = _result(frames)
    assert result["done"] is True
    # Unmodeled parts cap the verdict at WARN (PASS would be a lie
    # about a circuit the model could not fully simulate).
    assert result["verdict"] == "WARN"
    assert result["unmodeled"] == unmodeled


def test_run_stream_goal_team_picks_pass_candidate(
    monkeypatch: pytest.MonkeyPatch,
):
    """4 design calls (one per team strategist); the returned
    netlist is the PASS candidate, not the first (STOP) one; the
    Designer / Judge / Physics progress events carry the dock
    dialect and the terminal result carries the leaderboard."""
    designs: list[str] = []
    script = iter(
        [
            _overcurrent_netlist(),
            _overcurrent_netlist(),
            _good_netlist(),
            _good_netlist(),
        ]
    )

    def fake_design(goal, client=None):
        designs.append(goal)
        return next(script)

    monkeypatch.setattr(llm_module, "design", fake_design)

    frames = _collect("goal", goal="9V LED circuit, red LED, 15 mA")

    # Team of 4: llm.design called once per strategist, each with the
    # goal plus its strategy text.
    assert len(designs) == 4
    assert all(g.startswith("9V LED circuit, red LED, 15 mA") for g in designs)
    assert [f for f in frames if f["event"] == "error"] == []
    progress = [f["data"] for f in frames if f["event"] == "progress"]
    # Dock dialect: per-designer done events carry state/score/calls.
    designer_done = [
        d
        for d in progress
        if d["agent"].startswith("Designer ")
        and d.get("state") == "done"
    ]
    assert len(designer_done) == 4
    assert all(
        isinstance(d.get("score"), (int, float))
        and d.get("calls") == 1
        and d.get("physics") == 1
        for d in designer_done
    )
    judge = [d for d in progress if d["agent"] == "Judge"]
    assert len(judge) == 1 and judge[0].get("state") == "done"
    assert "Winner: Designer" in judge[0]["message"]
    physics_done = [
        d
        for d in progress
        if d["agent"] == "Physics" and d.get("state") == "done"
    ]
    assert len(physics_done) == 1
    assert physics_done[0]["message"].startswith("Verdict PASS")
    result = _result(frames)
    assert result["done"] is True
    assert result["mock"] is False
    assert result["verdict"] == "PASS"
    # The PASS candidate (R1 = 470), not the first STOP candidate.
    used = {c["id"]: c for c in result["netlist_used"]["components"]}
    assert used["R1"]["value"] == 470.0
    assert result["findings"] == []
    # Leaderboard: winner first, one entry per designer.
    board = result["leaderboard"]
    assert len(board) == 4
    assert board[0]["verdict"] == "PASS"
    assert {b["name"] for b in board} == {
        "Faithful",
        "Simple",
        "Safe",
        "Efficient",
    }
