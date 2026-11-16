"""Tuners: each improves (or keeps) the score, never adds errors, and streams progress."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import netproof.server as server
from netproof import tuner, optics

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def _stops(report):
    return sum(1 for f in report["findings"] if f["severity"] == "STOP")


def test_fiber_tuner_fixes_overload():
    events = []
    out = tuner.tune_fiber(_load("fiber_overload"), {"objective": "balanced"}, events.append)
    assert _stops(out["report"]) == 0 and out["score_after"] > out["score_before"]
    assert any("Attenuation" in c for c in out["changes"]) and events


def test_fiber_tuner_fixes_polish_mismatch():
    out = tuner.tune_fiber(_load("fiber_apc_mismatch"), {}, lambda e: None)
    assert _stops(out["report"]) == 0 and any("matched polish" in c for c in out["changes"])


def test_optics_autofix_ends_stray_beam():
    out = tuner.tune_optics(_load("optics_stray"), {"objective": "max_power"}, lambda e: None)
    assert out["report"]["stray_beams"] == 0 and _stops(out["report"]) == 0
    assert any(e["type"] == "dump" for e in out["design"]["elements"])


def test_optics_tuner_realigns_detuned_mirror():
    bench = _load("optics_good")
    bench["elements"][2]["angle_deg"] = 137  # M2 off by 2 degrees
    assert optics.trace_bench(bench)["received_mw"].get("D1", 0) == 0
    out = tuner.tune_optics(bench, {"objective": "max_power"}, lambda e: None)
    assert out["report"]["received_mw"]["D1"] > 0.4 and out["score_after"] > out["score_before"]


def test_optics_overload_gets_nd_filter():
    out = tuner.tune_optics(_load("optics_overload"), {}, lambda e: None)
    assert any(e["type"] == "filter" and e["id"].startswith("ND") for e in out["design"]["elements"])
    assert _stops(out["report"]) == 0


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_tune_endpoint_streams_circuit():
    body = {"design": _load("led_overcurrent"), "prefs": {"objective": "balanced"}}
    text = TestClient(server.app).post("/tune/circuit", json=body).text
    events = [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:")]
    done = events[-1]
    assert done["stage"] == "done" and done["report"]["verdict"] == "PASS"
    r1 = next(c for c in done["design"]["components"] if c["id"] == "R1")
    assert 470 <= r1["value"] <= 820  # ~12 mA from 9 V with a ~2 V LED
    assert any(e["stage"] == "tune" for e in events[:-1])


# ---------------------------------------------------------------------------
# Bounded cost guard (swarm/tuner-guard): caps stop the search cleanly
# ---------------------------------------------------------------------------

def _fake_circuit_check(calls):
    """Deterministic check that never needs ngspice: LED pinned at 12 mA."""
    def check(netlist):
        calls["n"] += 1
        return {"findings": [], "netlist": [{"type": "led", "current": 0.012}], "power": {}}
    return check


def _cap_events(events):
    return [e for e in events if e.get("capped")]


def test_tuner_eval_cap_stops_circuit_search(monkeypatch):
    monkeypatch.setenv("NETPROOF_TUNER_MAX_EVALS", "3")
    calls = {"n": 0}
    netlist = {"components": [
        {"id": "LED1", "type": "led", "nodes": ["n1", "gnd"]},
        {"id": "R1", "type": "resistor", "nodes": ["n1", "vcc"], "value": 1000},
    ]}
    events = []
    out = tuner.tune_circuit(netlist, {"objective": "balanced"}, _fake_circuit_check(calls), events.append)
    assert out["capped"] is True and out["evals"] == 4  # 1 start + 2 candidates + mandatory final check
    assert calls["n"] == 4  # start check + 2 candidates + mandatory final check
    assert out["design"] and out["report"]  # still returns the best design so far
    assert _cap_events(events) and "evaluation cap (3 evaluations)" in _cap_events(events)[0]["message"]


def test_tuner_eval_cap_stops_fiber_search(monkeypatch):
    monkeypatch.setenv("NETPROOF_TUNER_MAX_EVALS", "2")
    events = []
    out = tuner.tune_fiber(_load("fiber_overload"), {"objective": "balanced"}, events.append)
    assert out["capped"] is True and out["evals"] == 2
    assert out["design"] and out["report"]
    assert _cap_events(events) and "evaluation cap" in _cap_events(events)[0]["message"]


def test_tuner_time_cap_stops_optics_search():
    events = []
    out = tuner.tune_optics(_load("optics_good"), {"objective": "max_power", "max_seconds": 0}, events.append)
    assert out["capped"] is True
    assert out["design"] and out["report"]
    assert _cap_events(events) and "time cap (0 s)" in _cap_events(events)[0]["message"]


def test_tuner_autofix_path_respects_eval_cap():
    events = []
    out = tuner.tune_optics(_load("optics_stray"), {"objective": "max_power", "max_evals": 1}, events.append)
    assert out["capped"] is True  # cap hit inside autofix, before any dump/trace loop
    assert out["design"] and out["report"]
    assert _cap_events(events)


def test_tuner_default_run_is_not_capped():
    out = tuner.tune_fiber(_load("fiber_overload"), {"objective": "balanced"}, lambda e: None)
    assert out["capped"] is False
    assert out["evals"] == 12  # 1 start check + 11 attenuation candidates


def test_tuner_prefs_override_env_cap(monkeypatch):
    monkeypatch.setenv("NETPROOF_TUNER_MAX_EVALS", "2")
    out = tuner.tune_fiber(_load("fiber_overload"), {"objective": "balanced", "max_evals": 500}, lambda e: None)
    assert out["capped"] is False and out["evals"] == 12


def test_tuner_invalid_env_cap_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("NETPROOF_TUNER_MAX_EVALS", "banana")
    monkeypatch.setenv("NETPROOF_TUNER_MAX_SECONDS", "not-a-time")
    out = tuner.tune_fiber(_load("fiber_overload"), {"objective": "balanced"}, lambda e: None)
    assert out["capped"] is False and out["evals"] == 12
