"""Bench backlog #9 (2/3): rejected tuner tries carry tune="rejected", tick=True.

Covers all three deterministic tuners in ``netproof/tuner.py``:
* ``tune_circuit`` — lines ~81-82 emit rejected tries explicitly with
  ``tick: True``; driven here with a fake ``check`` (analytic LED current
  ``(9-2)/R``) so no ngspice is needed and the score rises to the best
  E12 resistor then falls, guaranteeing rejected tries after the peak.
* ``tune_optics`` — a detuned mirror bench (M2 off by 2 degrees) forces
  coordinate-search tries that do not improve the score.
* ``tune_fiber`` — an overloaded link forces attenuator tries that do
  not improve the score.

In every case a rejected try must be identifiable as a progress tick:
``tune == "rejected"`` AND ``tick is True`` (not merely truthy, and not
missing). Kept tries are not asserted here.
"""
from __future__ import annotations

import json
from pathlib import Path

from netproof import tuner

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def _rejected(events):
    return [e for e in events if e.get("tune") == "rejected"]


def _assert_rejected_tries_tick(events):
    rejected = _rejected(events)
    assert rejected, f"expected at least one rejected try, got events: {events!r}"
    for event in rejected:
        assert event.get("stage") == "tune", event
        assert event.get("tick") is True, (
            f"rejected try must carry tick: True, got {event!r}"
        )
        assert "rejected" in event.get("message", ""), event
        # Note: circuit/optics rejected tries also carry "step"; the fiber
        # tuner (tuner.py ~157) emits no "step", so it is not required here.


def test_tune_circuit_rejected_tries_tick():
    """Fake-check circuit tuner: scores peak at R~560 ohm, later E12
    values are rejected, and every rejection ticks."""
    events = []

    def fake_check(cand):
        r = next(c for c in cand["components"] if c["id"] == "R1")
        current = (9.0 - 2.0) / r["value"]
        return {
            "findings": [],
            "netlist": [{"id": "D1", "type": "led", "current": current}],
            "power": {"r1": current**2 * r["value"]},
        }

    netlist = {
        "components": [
            {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
            {"id": "R1", "type": "resistor", "value": 100.0, "nodes": ["vcc", "led"]},
            {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"]},
        ]
    }
    out = tuner.tune_circuit(
        netlist, {"objective": "balanced"}, fake_check, events.append
    )
    assert out["score_after"] > out["score_before"]
    _assert_rejected_tries_tick(events)


def test_tune_optics_rejected_tries_tick():
    """Detuned mirror bench: coordinate search tries worse angles and
    rejects them, each with tick True."""
    bench = _load("optics_good")
    bench["elements"][2]["angle_deg"] = 137  # M2 off by 2 degrees
    events = []
    out = tuner.tune_optics(bench, {"objective": "max_power"}, events.append)
    assert out["report"]["received_mw"]["D1"] > 0.4
    _assert_rejected_tries_tick(events)


def test_tune_fiber_rejected_tries_tick():
    """Overloaded fiber link with a target the attenuator can overshoot:
    attenuation past the optimum (rx near the 0 dBm target) lowers the
    score, so those tries are rejected, each with tick True. (With the
    default target the score rises monotonically to 30 dB and nothing
    is ever rejected, so the default target cannot exercise this path.)"""
    events = []
    out = tuner.tune_fiber(
        _load("fiber_overload"), {"target_dbm": 0}, events.append
    )
    assert out["score_after"] >= out["score_before"]
    _assert_rejected_tries_tick(events)
