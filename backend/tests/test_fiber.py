"""Fiber link checker: power budget numbers, rules, endpoints, build pack."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import netproof.server as server
from netproof.fiber import check_link

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.mark.parametrize("name, verdict, rules", [
    ("fiber_good", "PASS", []),
    ("fiber_overload", "STOP", ["DETECTOR_OVERLOAD", "LASER_SAFETY"]),
    ("fiber_deep_split", "STOP", ["LINK_MARGIN"]),
    ("fiber_apc_mismatch", "STOP", ["CONNECTOR_POLISH_MISMATCH"]),
])
def test_fixture_verdicts(name, verdict, rules):
    r = check_link(_load(name))
    assert r["verdict"] == verdict
    assert [f["rule_id"] for f in r["findings"]] == rules


def test_power_budget_math():
    r = check_link(_load("fiber_good"))
    # 14 m SMF @1550 (0.2 dB/km) + 2 connectors (0.3) + 1x2 split (3.0103 + 0.5)
    assert r["total_loss_db"] == pytest.approx(14 * 0.0002 + 0.6 + 3.0103 + 0.5, abs=1e-3)
    assert r["received_dbm"] == pytest.approx(-r["total_loss_db"], abs=1e-3)
    assert [t["id"] for t in r["trace"]] == ["L1", "F1", "C1", "F2", "S1", "F3", "C2", "D1"]


def test_structure_rules():
    r = check_link({"elements": [{"id": "F1", "type": "fiber"}]})
    assert {f["rule_id"] for f in r["findings"]} == {"LINK_NO_SOURCE", "LINK_NO_DETECTOR"}


def test_endpoints():
    c = TestClient(server.app)
    assert c.post("/fiber/check", json=_load("fiber_good")).json()["verdict"] == "PASS"
    assert c.post("/fiber/check", json={"elements": []}).status_code == 422
    pack = c.post("/fiber/build-pack", json=_load("fiber_good"))
    assert pack.status_code == 200 and pack.content.startswith(b"%PDF")
    assert c.post("/fiber/build-pack", json=_load("fiber_overload")).status_code == 409
