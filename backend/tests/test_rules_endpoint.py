"""Tests for GET /rules (Netproof P5.2).

The 12-row rule inventory, built live from ``rules.py`` constants.
No ngspice is needed for these tests, but the TestClient startup
still requires ngspice on PATH or an injected simulator, so this
module installs its own analytic fake with ``set_simulator`` BEFORE
creating the TestClient (the module-scoped fake in test_server.py
does not carry over to this module).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from netproof.ir import Netlist

try:
    from netproof import server as _server_module
    from netproof.server import app
except ImportError as exc:  # pragma: no cover - server.py is present
    app = None
    _server_module = None
    _SERVER_IMPORT_ERROR = exc
else:
    _SERVER_IMPORT_ERROR = None

needs_server = pytest.mark.skipif(
    app is None,
    reason=f"netproof.server not importable: {_SERVER_IMPORT_ERROR}",
)


class _AnalyticSeriesSimulator:
    """Same series source-resistor-LED fake as test_server.py."""

    def run(self, netlist: Netlist) -> dict:
        comps = {c.id: c for c in netlist.components}
        source = next(c for c in netlist.components if c.type == "voltage_source")
        resistor = next(c for c in netlist.components if c.type == "resistor")
        led = next(c for c in netlist.components if c.type == "led")
        current = (source.value - led.value) / resistor.value
        return {
            "node_voltages": {"0": 0.0},
            "branch_currents": {
                source.id: current,
                resistor.id: current,
                led.id: current,
            },
            "power": {
                resistor.id: current * current * resistor.value,
                led.id: current * led.value,
            },
            "components": comps,
        }


@pytest.fixture(scope="module", autouse=True)
def _install_fake_simulator():
    if _server_module is not None:
        _server_module.set_simulator(_AnalyticSeriesSimulator())
        yield
        _server_module.set_simulator(None)
    else:
        yield


@pytest.fixture(scope="module")
def client():
    if app is None:
        pytest.skip(f"netproof.server not importable: {_SERVER_IMPORT_ERROR}")
    ctx = TestClient(app)
    try:
        test_client = ctx.__enter__()
    except RuntimeError as exc:
        if "ngspice" in str(exc).lower():
            pytest.skip("ngspice not installed, server refuses to start")
        raise
    yield test_client
    ctx.__exit__(None, None, None)


@pytest.fixture(scope="module")
def rules_payload(client: TestClient) -> dict:
    resp = client.get("/rules")
    assert resp.status_code == 200
    return resp.json()


# Full expected row order: pre-sim in pre_sim_checks order, then
# post-sim in post_sim_checks order, STOP tier before WARN within a
# shared rule_id (matches rules.py check order and SEVERITY_RANK).
EXPECTED_ORDER = [
    ("NO_GROUND", "STOP", None),
    ("FLOATING_NODE", "STOP", None),
    ("SHORTED_SOURCE", "STOP", None),
    ("SHORTED_COMPONENT", "WARN", None),
    ("NO_SOURCE", "INFO", None),
    ("REVERSED_LED", "INFO", None),
    ("LED_OVERCURRENT", "STOP", "stop"),
    ("LED_OVERCURRENT", "WARN", "warn"),
    ("LED_UNDERCURRENT", "INFO", None),
    ("RESISTOR_POWER", "STOP", "stop"),
    ("RESISTOR_POWER", "WARN", "warn"),
    ("SOURCE_OVERCURRENT", "WARN", None),
]


@needs_server
def test_rules_returns_12_rows_in_order(rules_payload: dict):
    rows = rules_payload["rules"]
    assert len(rows) == 12
    assert [(r["rule_id"], r["severity"], r["band"]) for r in rows] == EXPECTED_ORDER


@needs_server
def test_rules_has_10_distinct_rule_ids(rules_payload: dict):
    rows = rules_payload["rules"]
    assert len({r["rule_id"] for r in rows}) == 10
    assert len({(r["rule_id"], r["band"]) for r in rows}) == 12


@needs_server
def test_rules_led_rows(rules_payload: dict):
    rows = [r for r in rules_payload["rules"] if r["rule_id"] == "LED_OVERCURRENT"]
    assert len(rows) == 2
    stop = next(r for r in rows if r["band"] == "stop")
    warn = next(r for r in rows if r["band"] == "warn")
    assert (stop["severity"], stop["comparator"], stop["threshold"]) == ("STOP", ">", 0.03)
    assert (warn["severity"], warn["comparator"], warn["threshold"]) == ("WARN", ">=", 0.02)
    assert stop["threshold_display"] == "30 mA"
    assert warn["threshold_display"] == "20 mA"
    assert stop["threshold_unit"] == warn["threshold_unit"] == "A"
    assert stop["quantity"] == warn["quantity"] == "current_a"


@needs_server
def test_rules_resistor_rows_are_fractions(rules_payload: dict):
    rows = [r for r in rules_payload["rules"] if r["rule_id"] == "RESISTOR_POWER"]
    assert len(rows) == 2
    stop = next(r for r in rows if r["band"] == "stop")
    warn = next(r for r in rows if r["band"] == "warn")
    assert (stop["severity"], stop["comparator"], stop["threshold"]) == ("STOP", ">", 1.0)
    assert (warn["severity"], warn["comparator"], warn["threshold"]) == ("WARN", ">", 0.7)
    assert stop["quantity"] == warn["quantity"] == "power_fraction_of_rating"
    assert stop["threshold_unit"] == warn["threshold_unit"] == "fraction"
    assert stop["threshold_display"] == "100% of rating"
    assert warn["threshold_display"] == "70% of rating"


@needs_server
def test_rules_reversed_led_row(rules_payload: dict):
    row = next(r for r in rules_payload["rules"] if r["rule_id"] == "REVERSED_LED")
    assert row["severity"] == "INFO"
    assert row["band"] is None
    assert row["quantity"] == "anode_minus_cathode_v"
    assert row["comparator"] == "<"
    assert row["threshold"] == -1e-9
    assert row["threshold_unit"] == "V"


@needs_server
def test_rules_structural_rows_have_no_threshold(rules_payload: dict):
    for rule_id in ("NO_GROUND", "FLOATING_NODE", "SHORTED_SOURCE",
                    "SHORTED_COMPONENT", "NO_SOURCE"):
        row = next(r for r in rules_payload["rules"] if r["rule_id"] == rule_id)
        assert row["threshold"] is None
        assert row["comparator"] is None
        assert row["band"] is None


@needs_server
def test_rules_undercurrent_and_source_rows(rules_payload: dict):
    under = next(r for r in rules_payload["rules"] if r["rule_id"] == "LED_UNDERCURRENT")
    assert (under["severity"], under["comparator"], under["threshold"]) == ("INFO", "<", 0.001)
    src = next(r for r in rules_payload["rules"] if r["rule_id"] == "SOURCE_OVERCURRENT")
    assert (src["severity"], src["comparator"], src["threshold"]) == ("WARN", ">", 1.0)


@needs_server
def test_rules_defaults_and_notes(rules_payload: dict):
    assert rules_payload["defaults"] == {"resistor_rating_w": 0.25}
    assert len(rules_payload["notes"]) == 2
    assert any("identical for all colours" in n for n in rules_payload["notes"])
    assert any("own rating_watts" in n for n in rules_payload["notes"])
    # Every row carries the full field contract.
    for row in rules_payload["rules"]:
        assert set(row) == {
            "rule_id", "severity", "band", "applies_to", "quantity",
            "comparator", "threshold", "threshold_unit",
            "threshold_display", "source",
        }
        assert row["source"]
