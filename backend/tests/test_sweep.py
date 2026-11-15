"""Tests for POST /sweep (Netproof P5.2).

Reuses the analytic series fake from test_server.py. NOTE: that
module's fake does NOT carry over here, so this module installs its
own with ``set_simulator`` BEFORE creating the TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from netproof.ir import Netlist
from netproof import rules as rules_mod

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

SWEEP_PATH = "/sweep"


def _component(comp_id, comp_type, value, nodes, **extra):
    payload = {"id": comp_id, "type": comp_type, "value": value, "nodes": nodes}
    payload.update(extra)
    return payload


def good_payload() -> dict:
    """9 V source, 470 ohm series resistor, red LED -> PASS."""
    return {
        "title": "9V LED circuit (good)",
        "components": [
            _component("V1", "voltage_source", 9.0, ["n1", "0"]),
            _component("R1", "resistor", 470.0, ["n1", "n2"], rating_watts=0.25),
            _component("D1", "led", 2.0, ["n2", "0"], color="red"),
        ],
    }


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


class _KclOnlySimulator:
    """Real-ngspice-shaped fake: node voltages + source branch
    current only, NO LED (or resistor) branch current. LED current
    must come from the KCL derivation, as on the real path."""

    def run(self, netlist: Netlist) -> dict:
        source = next(c for c in netlist.components if c.type == "voltage_source")
        resistor = next(c for c in netlist.components if c.type == "resistor")
        led = next(c for c in netlist.components if c.type == "led")
        current = (source.value - led.value) / resistor.value
        return {
            "node_voltages": {"n1": 9.0, "n2": 2.0, "0": 0.0},
            "branch_currents": {"v1": -current},
            "power": {},
            "components": {c.id: c for c in netlist.components},
        }


class _Exact30mASimulator:
    """Every point reports exactly 0.030 A through the LED."""

    def run(self, netlist: Netlist) -> dict:
        return {
            "node_voltages": {"n1": 9.0, "n2": 2.0, "0": 0.0},
            "branch_currents": {"v1": -0.030, "r1": 0.030, "d1": 0.030},
            "power": {},
            "components": {c.id: c for c in netlist.components},
        }


class _FailAt270Simulator(_AnalyticSeriesSimulator):
    """Analytic fake, except the R=270 point fails to simulate."""

    def run(self, netlist: Netlist) -> dict:
        resistor = next(c for c in netlist.components if c.type == "resistor")
        if resistor.value == 270.0:
            return {
                "success": False,
                "error": "sim failed at 270",
                "node_voltages": {},
                "branch_currents": {},
                "power": {},
            }
        return super().run(netlist)


class _CountingSimulator(_AnalyticSeriesSimulator):
    """Analytic fake that counts how many sims ran."""

    def __init__(self):
        self.calls = 0

    def run(self, netlist: Netlist) -> dict:
        self.calls += 1
        return super().run(netlist)


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


@pytest.fixture
def _swap_simulator():
    """Swap the injected simulator for one test, then restore."""
    def _swap(sim):
        _server_module.set_simulator(sim)
        return sim
    yield _swap
    _server_module.set_simulator(_AnalyticSeriesSimulator())


def _sweep_body(**overrides):
    body = {"netlist": good_payload(), "resistor_id": "R1"}
    body.update(overrides)
    return body


def _rule_ids(point):
    return [f["rule_id"] for f in point.get("findings", [])]


# ---------------------------------------------------------------------------
# Default sweep
# ---------------------------------------------------------------------------
@needs_server
def test_sweep_default_ten_points(client: TestClient):
    resp = client.post(SWEEP_PATH, json=_sweep_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["resistor_id"] == "R1"
    assert data["led_id"] == "D1"
    assert data["rating_w"] == 0.25
    assert data["values"] == [100, 120, 180, 270, 390, 560, 820, 1200, 1800, 2200]
    assert data["mock"] is False
    assert len(data["points"]) == 10
    assert [p["r_ohms"] for p in data["points"]] == data["values"]
    # Threshold echo: default 0.25 W rating.
    assert data["thresholds"]["led_warn_a"] == 0.02
    assert data["thresholds"]["led_stop_a"] == 0.03
    assert data["thresholds"]["led_under_a"] == 0.001
    assert data["thresholds"]["resistor_warn_w"] == pytest.approx(0.175)
    assert data["thresholds"]["resistor_stop_w"] == pytest.approx(0.25)


@needs_server
def test_sweep_point_verdicts_match_rules(client: TestClient):
    resp = client.post(SWEEP_PATH, json=_sweep_body(values=[100, 270, 470]))
    assert resp.status_code == 200
    points = {p["r_ohms"]: p for p in resp.json()["points"]}
    # R=100: 70 mA -> STOP, LED_OVERCURRENT.
    assert points[100.0]["verdict"] == "STOP"
    assert "LED_OVERCURRENT" in _rule_ids(points[100.0])
    assert points[100.0]["led_current_a"] == pytest.approx(0.07)
    assert points[100.0]["resistor_power_w"] == pytest.approx(0.49)
    # R=270: 25.9 mA / 0.181 W -> WARN, matches rules.verify directly.
    assert points[270.0]["verdict"] == "WARN"
    netlist = Netlist.model_validate(good_payload())
    netlist_270 = netlist.model_copy(
        update={"components": [
            c.model_copy(update={"value": 270.0}) if c.id == "R1" else c
            for c in netlist.components
        ]}
    )
    sim = _AnalyticSeriesSimulator().run(netlist_270)
    assert rules_mod.verify(netlist_270, sim).verdict == points[270.0]["verdict"]
    # R=470: 14.9 mA -> PASS.
    assert points[470.0]["verdict"] == "PASS"
    assert points[470.0]["findings"] == []
    assert points[470.0]["led_current_a"] == pytest.approx(7.0 / 470.0)


@needs_server
def test_sweep_custom_rating_uses_own_rating(client: TestClient):
    payload = good_payload()
    payload["components"] = [
        {**c, "rating_watts": 0.5} if c["id"] == "R1" else c
        for c in payload["components"]
    ]
    resp = client.post(SWEEP_PATH, json={"netlist": payload, "resistor_id": "R1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["rating_w"] == 0.5
    assert data["thresholds"]["resistor_warn_w"] == pytest.approx(0.35)
    assert data["thresholds"]["resistor_stop_w"] == pytest.approx(0.5)


@needs_server
def test_sweep_include_findings_false(client: TestClient):
    resp = client.post(
        SWEEP_PATH, json=_sweep_body(values=[100], include_findings=False)
    )
    assert resp.status_code == 200
    point = resp.json()["points"][0]
    assert point["findings"] == []
    assert point["verdict"] == "STOP"


@needs_server
def test_sweep_mock(client: TestClient):
    resp = client.post(SWEEP_PATH + "?mock=1", json=_sweep_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["mock"] is True
    assert len(data["points"]) == 10
    assert all(p["led_current_a"] is None for p in data["points"])
    assert all("Mock mode" in u for p in data["points"] for u in p["uncertain"])


# ---------------------------------------------------------------------------
# 422 cases
# ---------------------------------------------------------------------------
@needs_server
@pytest.mark.parametrize(
    "body,code",
    [
        ({"resistor_id": "R9"}, "unknown_resistor"),
        ({"resistor_id": "D1"}, "invalid_resistor"),
        ({"led_id": "R9"}, "unknown_led"),
        ({"led_id": "R1"}, "invalid_led"),
        ({"values": []}, "invalid_values"),
        ({"values": [0]}, "invalid_values"),
        ({"values": [-100]}, "invalid_values"),
        ({"values": [100, 100]}, "invalid_values"),
        ({"values": list(range(1, 27))}, "invalid_values"),
    ],
)
def test_sweep_422_cases(client: TestClient, body: dict, code: str):
    resp = client.post(SWEEP_PATH, json=_sweep_body(**body))
    assert resp.status_code == 422
    assert resp.json()["error"] == code


@needs_server
def test_sweep_no_led_422(client: TestClient):
    payload = {
        "title": "no LED",
        "components": [
            _component("V1", "voltage_source", 9.0, ["n1", "0"]),
            _component("R1", "resistor", 470.0, ["n1", "0"], rating_watts=0.25),
        ],
    }
    resp = client.post(SWEEP_PATH, json={"netlist": payload, "resistor_id": "R1"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "no_led"


@needs_server
def test_sweep_erc_gate_runs_zero_sims(client: TestClient, _swap_simulator):
    counting = _CountingSimulator()
    _swap_simulator(counting)
    payload = {
        "title": "shorted source",
        "components": [
            _component("V1", "voltage_source", 9.0, ["0", "0"]),
            _component("R1", "resistor", 470.0, ["n1", "n2"], rating_watts=0.25),
            _component("D1", "led", 2.0, ["n2", "0"], color="red"),
        ],
    }
    resp = client.post(SWEEP_PATH, json={"netlist": payload, "resistor_id": "R1"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "erc_failed"
    assert resp.json()["findings"], "ERC findings must be returned"
    assert counting.calls == 0


# ---------------------------------------------------------------------------
# LED current read path (KCL-only fake guards the real-ngspice path)
# ---------------------------------------------------------------------------
@needs_server
def test_sweep_kcl_only_fake_still_reports_led_current(
    client: TestClient, _swap_simulator
):
    _swap_simulator(_KclOnlySimulator())
    resp = client.post(SWEEP_PATH, json=_sweep_body(values=[470]))
    assert resp.status_code == 200
    point = resp.json()["points"][0]
    # No LED branch current anywhere in the fake, yet KCL derives it.
    assert point["led_current_a"] == pytest.approx(7.0 / 470.0)
    assert point["resistor_power_w"] == pytest.approx(49.0 / 470.0)
    assert point["verdict"] == "PASS"


@needs_server
def test_sweep_exactly_30ma_is_warn_not_stop(
    client: TestClient, _swap_simulator
):
    _swap_simulator(_Exact30mASimulator())
    resp = client.post(SWEEP_PATH, json=_sweep_body(values=[470]))
    assert resp.status_code == 200
    point = resp.json()["points"][0]
    assert point["led_current_a"] == pytest.approx(0.030)
    assert point["verdict"] == "WARN"
    assert "LED_OVERCURRENT" in _rule_ids(point)
    assert not any(
        f["severity"] == "STOP" and f["rule_id"] == "LED_OVERCURRENT"
        for f in point["findings"]
    )


@needs_server
def test_sweep_per_point_sim_failure_is_a_gap_not_an_abort(
    client: TestClient, _swap_simulator
):
    _swap_simulator(_FailAt270Simulator())
    resp = client.post(SWEEP_PATH, json=_sweep_body(values=[100, 270, 470]))
    assert resp.status_code == 200
    points = {p["r_ohms"]: p for p in resp.json()["points"]}
    gap = points[270.0]
    assert gap["led_current_a"] is None
    assert gap["resistor_power_w"] is None
    assert any("Simulation did not succeed" in u for u in gap["uncertain"])
    # Neighbours still simulated normally.
    assert points[100.0]["verdict"] == "STOP"
    assert points[470.0]["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Downloads must survive a sweep (_LAST_NETLIST)
# ---------------------------------------------------------------------------
@needs_server
def test_sweep_does_not_corrupt_downloads(client: TestClient):
    check = client.post("/check", json=good_payload())
    assert check.status_code == 200
    assert check.json()["verdict"] == "PASS"
    sweep = client.post(SWEEP_PATH, json=_sweep_body())
    assert sweep.status_code == 200
    cir = client.get("/download/netlist.cir")
    assert cir.status_code == 200
    assert "470" in cir.text
    assert "2200" not in cir.text
    bom = client.get("/download/bom.csv")
    assert bom.status_code == 200
    assert bom.text.startswith("id,type,value,rating,qty")
