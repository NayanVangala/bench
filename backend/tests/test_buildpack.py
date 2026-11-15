"""Build pack: BOM prices/links, breadboard steps, PDF endpoint, STOP refusal."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netproof import buildpack
import netproof.server as server

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def test_line_items_and_steps_for_good_led():
    netlist = _load("good_led")
    items = {i["name"]: i for i in buildpack.line_items(netlist)}
    assert items["Resistor"]["spec"].startswith("470 Ω") and items["Resistor"]["refs"] == ["R1"]
    assert items["LED"]["spec"] == "5 mm red"
    assert {"9 V battery", "9 V battery snap"} <= items.keys()
    assert all(i["total_usd"] == round(i["unit_usd"] * i["qty"], 2) for i in items.values())
    assert all(len(i["links"]) == 3 for i in items.values())

    report = {"node_voltages": {"vcc": 9.0, "led": 2.0}, "branch_currents": {"v1": -0.0148}}
    steps = " ".join(buildpack.build_steps(netlist, report))
    # vcc is the battery + -> + rail; led node -> a row; ground -> - rail.
    assert "R1 (470 Ω resistor) from the + rail to row 10" in steps
    assert "long leg (anode, +) into row 10" in steps and "into the − rail" in steps
    assert steps.index("Last: connect V1") > steps.index("Place D1")
    tests = " ".join(buildpack.test_steps(netlist, report))
    assert "expect about 2.00 V" in tests and "expect about 14.8 mA" in tests


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_build_pack_endpoint():
    client = TestClient(server.app)
    ok = client.post("/build-pack", json=_load("good_led"))
    assert ok.status_code == 200 and ok.headers["content-type"] == "application/pdf"
    assert ok.content.startswith(b"%PDF") and len(ok.content) > 5000
    stop = client.post("/build-pack", json=_load("led_overcurrent"))
    assert stop.status_code == 409 and stop.json()["error"] == "not_buildable"


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_check_reports_led_current_on_pass():
    """A PASS has no LED finding to read the current from: the LED row must carry it."""
    rows = TestClient(server.app).post("/check", json=_load("good_led")).json()["netlist"]
    d1 = next(r for r in rows if r["id"] == "D1")
    assert d1["current"] == pytest.approx(0.0149, abs=5e-4)
