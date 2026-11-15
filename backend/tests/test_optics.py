"""Optics bench: ray trace geometry, rules, endpoints, build pack."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import netproof.server as server
from netproof.optics import trace_bench

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.mark.parametrize("name, verdict, rules", [
    ("optics_good", "PASS", []),
    ("optics_stray", "STOP", ["STRAY_BEAM"]),
    ("optics_misaligned", "STOP", ["DETECTOR_NO_LIGHT", "STRAY_BEAM", "NOT_IN_BEAM"]),
    ("optics_overload", "STOP", ["DETECTOR_OVERLOAD", "LASER_SAFETY"]),
])
def test_fixture_verdicts(name, verdict, rules):
    r = trace_bench(_load(name))
    assert r["verdict"] == verdict
    assert [f["rule_id"] for f in r["findings"]] == rules


def test_mirror_geometry_and_power():
    r = trace_bench(_load("optics_good"))
    # L1 -> M1 (45 deg turn up) -> M2 (turn right) -> lens -> ND -> detector.
    assert [s["end"] for s in r["segments"]] == ["M1", "M2", "LN1", "ND1", "D1"]
    assert r["segments"][1]["from"] == [300, 50] and r["segments"][1]["to"] == [300, 250]
    assert r["received_mw"]["D1"] == pytest.approx(4.5 * 0.99 * 0.99 * 0.96 * 0.1, rel=1e-6)


def test_dump_fixes_stray_beam():
    bench = _load("optics_stray")
    bench["elements"].append({"id": "BD1", "type": "dump", "x_mm": 250, "y_mm": 270, "angle_deg": 90})
    assert trace_bench(bench)["verdict"] == "PASS"


def test_endpoints():
    c = TestClient(server.app)
    assert c.post("/optics/check", json=_load("optics_good")).json()["verdict"] == "PASS"
    pack = c.post("/optics/build-pack", json=_load("optics_good"))
    assert pack.status_code == 200 and pack.content.startswith(b"%PDF")
    assert c.post("/optics/build-pack", json=_load("optics_stray")).status_code == 409
