"""Server tests for the Netproof MVP (FastAPI TestClient).

File owner: backend/tests/test_server.py ONLY. Does not touch server code.

STATUS AT WRITE TIME (2026-11-15): ``backend/netproof/server.py`` does
NOT exist yet (package only has ``ir.py`` / ``parts.py``, ``web/`` is
empty, ``backend/tests/fixtures/`` is empty). These tests are therefore
written against the spec routes below and will SKIP (not fail) until the
server lands. The three fixture-validity tests at the bottom need only
``ir.py`` and run today.

UPDATE (2026-11-15, later the same day): ``server.py`` has landed, and
its FastAPI startup event raises ``RuntimeError`` when the ``ngspice``
binary is absent from PATH -- refusing to start without ngspice is the
server's spec behaviour and is NOT bypassed here. The ``client``
fixture therefore catches that ngspice ``RuntimeError`` at TestClient
startup and skips with "ngspice not installed, server refuses to
start", so the server tests SKIP (not error) on a machine without
ngspice. Any other startup error still propagates as a real failure.
The three fixture-validity tests at the bottom need only ``ir.py``
and run today.

Assumed routes (from README.md + the MVP spec; adjust the *_PATH
constants if the real server differs -- the tests, not the server, are
what this file owns):
  * GET  /health            -> 200, JSON with "status": "ok"
  * POST /check             -> accepts a Netlist JSON body (the
                               ``netproof.ir.Netlist`` shape: {"title",
                               "components": [...]}), returns a Report
                               JSON body with "verdict" and "findings"
                               (each finding has "rule_id").
  * GET  /                 -> 200, serves web/index.html (text/html).
  * POST /download/netlist -> accepts a Netlist JSON body, returns a
                               SPICE netlist file (``netlist.cir``)
                               containing a ``.op`` directive.
  * POST /download/bom     -> accepts a Netlist JSON body, returns
                               ``bom.csv`` whose first line is exactly
                               ``id,type,value,rating,qty``.

Simulation gating: POST /check verdicts need a real ngspice run, so
those tests skip when the ``ngspice`` binary is absent from PATH
(verified absent on this machine, 2026-11-15), unless the server
exposes a fake-simulator injection hook (``set_simulator``). No hook
exists yet, so the check tests currently skip for both reasons. No
network and no LLM key are used anywhere: the re-check test deletes
``ANTHROPIC_API_KEY`` from the environment to prove the verdict is
produced without the model.

Fixtures (9 V LED circuit, per the README demo):
  * good:        V1 9 V, R1 470 ohm, D1 red LED (Vf 2.0 V).
                 I ~= (9 - 2) / 470 ~= 14.9 mA -> under the 20 mA warn
                 and 30 mA max limits, over the 1 mA min -> PASS.
  * overcurrent: same circuit with R1 = 100 ohm.
                 I ~= (9 - 2) / 100 = 70 mA -> over the 30 mA LED
                 max -> STOP (LED_OVERCURRENT; R1 also exceeds its
                 0.25 W rating at ~0.49 W).
  * edited:      the overcurrent payload with R1 edited 100 -> 470.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from netproof.ir import Component, Netlist

# ---------------------------------------------------------------------------
# Route assumptions (see module docstring). Change here, not in the tests.
# ---------------------------------------------------------------------------
HEALTH_PATH = "/health"
CHECK_PATH = "/check"
INDEX_PATH = "/"
DOWNLOAD_NETLIST_PATH = "/download/netlist"
DOWNLOAD_BOM_PATH = "/download/bom"

BOM_HEADER = "id,type,value,rating,qty"

# ---------------------------------------------------------------------------
# Server import + simulator gating.
# ---------------------------------------------------------------------------
try:
    from netproof import server as _server_module
    from netproof.server import app
except ImportError as exc:  # server.py not written yet
    app = None
    _server_module = None
    _SERVER_IMPORT_ERROR = exc
else:
    _SERVER_IMPORT_ERROR = None

#: True when the server exposes a fake-simulator injection hook we know
#: how to use. Today this is False (server.py exists but exposes no
#: ``set_simulator`` hook).
_HAS_SIMULATOR_HOOK = _server_module is not None and hasattr(
    _server_module, "set_simulator"
)

#: Skip every test that needs the app until server.py exists.
needs_server = pytest.mark.skipif(
    app is None,
    reason=(
        "netproof.server not importable yet "
        f"(server.py missing: {_SERVER_IMPORT_ERROR})"
    ),
)

#: Skip every test that needs a real simulation when ngspice is absent
#: and no fake-simulator hook is available.
needs_simulation = pytest.mark.skipif(
    shutil.which("ngspice") is None and not _HAS_SIMULATOR_HOOK,
    reason=(
        "ngspice binary not found on PATH (install with "
        "`brew install ngspice`) and server exposes no set_simulator "
        "hook for fake-simulator injection"
    ),
)


class _AnalyticSeriesSimulator:
    """Best-effort fake simulator for a series source-resistor-LED loop.

    Only installed if the server exposes ``set_simulator``; its ``run``
    must return whatever the server's simulator interface returns for a
    netlist. Adapt this class to the real hook/interface when server.py
    lands -- until then it is never instantiated.
    """

    def run(self, netlist: Netlist) -> dict:
        comps = {c.id: c for c in netlist.components}
        source = next(
            c for c in netlist.components if c.type == "voltage_source"
        )
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
    """Install the analytic fake if (and only if) the hook exists."""
    if _server_module is not None and _HAS_SIMULATOR_HOOK:
        _server_module.set_simulator(_AnalyticSeriesSimulator())
        yield
        _server_module.set_simulator(None)
    else:
        yield


@pytest.fixture(scope="module")
def client():
    """TestClient for the app; skips the module's server tests if the
    app does not exist yet, or if the app refuses to start because
    ngspice is not installed (the FastAPI startup event raises
    RuntimeError). Refusing without ngspice is the server's spec
    behaviour, so the tests skip instead of erroring; any other
    startup error still propagates as a real failure."""
    if app is None:
        pytest.skip(f"netproof.server not importable: {_SERVER_IMPORT_ERROR}")
    # Enter the TestClient context explicitly (instead of ``with``) so
    # only a startup failure is caught here: a RuntimeError raised by
    # a test itself must never be turned into a skip.
    test_client_ctx = TestClient(app)
    try:
        test_client = test_client_ctx.__enter__()
    except RuntimeError as exc:
        if "ngspice" in str(exc).lower():
            pytest.skip("ngspice not installed, server refuses to start")
        raise
    yield test_client
    test_client_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Fixtures: 9 V LED circuit payloads (Netlist JSON, per ir.py).
# ---------------------------------------------------------------------------
def _component(
    comp_id: str,
    comp_type: str,
    value: float,
    nodes: list[str],
    **extra,
) -> dict:
    payload = {"id": comp_id, "type": comp_type, "value": value, "nodes": nodes}
    payload.update(extra)
    return payload


def good_payload() -> dict:
    """9 V source, 470 ohm series resistor, red LED -> expect PASS."""
    return {
        "title": "9V LED circuit (good)",
        "components": [
            _component("V1", "voltage_source", 9.0, ["n1", "0"]),
            _component("R1", "resistor", 470.0, ["n1", "n2"],
                       rating_watts=0.25),
            _component("D1", "led", 2.0, ["n2", "0"], color="red"),
        ],
    }


def overcurrent_payload() -> dict:
    """Same circuit with R1 = 100 ohm -> ~70 mA, expect STOP."""
    payload = good_payload()
    payload["title"] = "9V LED circuit (overcurrent)"
    payload["components"] = [
        {**c, "value": 100.0} if c["id"] == "R1" else c
        for c in payload["components"]
    ]
    return payload


def edited_payload() -> dict:
    """Overcurrent circuit with R1 edited 100 -> 470 ohm -> expect PASS."""
    payload = overcurrent_payload()
    payload["title"] = "9V LED circuit (R1 edited 100 -> 470)"
    payload["components"] = [
        {**c, "value": 470.0} if c["id"] == "R1" else c
        for c in payload["components"]
    ]
    return payload


def _rule_ids(report: dict) -> list[str]:
    return [f["rule_id"] for f in report.get("findings", [])]


# ---------------------------------------------------------------------------
# Health + index (need the server, but no simulation).
# ---------------------------------------------------------------------------
@needs_server
def test_health_returns_ok(client: TestClient):
    resp = client.get(HEALTH_PATH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@needs_server
def test_index_serves_html(client: TestClient):
    resp = client.get(INDEX_PATH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<html" in resp.text.lower()


@needs_server
def test_3d_main_script_served_as_javascript(client: TestClient):
    """web/index.html dynamically imports ./3d/main3d.js, so the server
    must serve it same-origin (static mount of web/, after the API
    routes) instead of 404ing."""
    resp = client.get("/3d/main3d.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"].lower()
    assert resp.text.strip(), "main3d.js must not be empty"
    assert "init3D" in resp.text


# ---------------------------------------------------------------------------
# POST /check verdicts (need the server AND a simulation).
# ---------------------------------------------------------------------------
@needs_server
@needs_simulation
def test_check_good_circuit_passes(client: TestClient):
    resp = client.post(CHECK_PATH, json=good_payload())
    assert resp.status_code == 200
    report = resp.json()
    assert report["verdict"] == "PASS"
    assert report["findings"] == []


@needs_server
@needs_simulation
def test_check_minimal_canonical_share_form_passes(client: TestClient):
    """P5.1: the minimal canonical form a share link posts — no title,
    no rating_watts / color (the server re-applies those defaults) —
    must still PASS. This guards the exact shape decodeSharePayload
    posts to /check (web/index.html share links carry the bare
    Netlist in this stripped form)."""
    payload = {
        "components": [
            _component("V1", "voltage_source", 9.0, ["vcc", "0"]),
            _component("R1", "resistor", 470.0, ["vcc", "led"]),
            _component("D1", "led", 2.0, ["led", "0"]),
        ]
    }
    resp = client.post(CHECK_PATH, json=payload)
    assert resp.status_code == 200
    report = resp.json()
    assert report["verdict"] == "PASS"
    assert report["findings"] == []


@needs_server
@needs_simulation
def test_check_overcurrent_circuit_stops(client: TestClient):
    resp = client.post(CHECK_PATH, json=overcurrent_payload())
    assert resp.status_code == 200
    report = resp.json()
    assert report["verdict"] == "STOP"
    assert report["findings"], "STOP verdict must name at least one rule"


@needs_server
@needs_simulation
def test_check_overcurrent_names_led_overcurrent(client: TestClient):
    resp = client.post(CHECK_PATH, json=overcurrent_payload())
    assert resp.status_code == 200
    assert "LED_OVERCURRENT" in _rule_ids(resp.json())


@needs_server
@needs_simulation
def test_edit_r1_and_recheck_passes_without_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """STOP on R1=100, then edit R1 100 -> 470 and re-check -> PASS,
    with no LLM key in the environment, proving the verdict comes from
    re-simulation, not from the model."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bad = client.post(CHECK_PATH, json=overcurrent_payload())
    assert bad.status_code == 200
    assert bad.json()["verdict"] == "STOP"
    fixed = client.post(CHECK_PATH, json=edited_payload())
    assert fixed.status_code == 200
    report = fixed.json()
    assert report["verdict"] == "PASS"
    assert report["findings"] == []


@needs_server
def test_check_rejects_invalid_component(client: TestClient):
    """A negative resistor value is invalid IR -> 422, no simulation."""
    payload = good_payload()
    payload["components"] = [
        {**c, "value": -5.0} if c["id"] == "R1" else c
        for c in payload["components"]
    ]
    resp = client.post(CHECK_PATH, json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Downloads (need the server; netlist/BOM generation, no simulation).
# ---------------------------------------------------------------------------
@needs_server
def test_download_netlist_contains_op_directive(client: TestClient):
    resp = client.post(DOWNLOAD_NETLIST_PATH, json=good_payload())
    assert resp.status_code == 200
    assert "netlist.cir" in resp.headers.get("content-disposition", "")
    text = resp.text
    assert ".op" in text.lower()
    assert "R1" in text and "V1" in text and "D1" in text


@needs_server
def test_download_bom_has_header_and_rows(client: TestClient):
    resp = client.post(DOWNLOAD_BOM_PATH, json=good_payload())
    assert resp.status_code == 200
    assert "bom.csv" in resp.headers.get("content-disposition", "").lower()
    lines = resp.text.strip().splitlines()
    assert lines[0] == BOM_HEADER
    assert any(line.startswith("R1,") for line in lines[1:])
    assert any(line.startswith("D1,") for line in lines[1:])
    assert any(line.startswith("V1,") for line in lines[1:])


# ---------------------------------------------------------------------------
# GET /parts (P5.3 parts picker; need the server, no simulation).
# ---------------------------------------------------------------------------
PARTS_PATH = "/parts"


@needs_server
def test_parts_returns_full_catalog(client: TestClient):
    resp = client.get(PARTS_PATH)
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    assert "max-age=86400" in resp.headers.get("cache-control", "")
    catalog = resp.json()
    assert catalog["version"] == 1
    assert set(catalog) == {
        "version", "resistor", "capacitor", "led",
        "diode", "voltage_source", "current_source",
    }
    resistor = catalog["resistor"]
    assert resistor["unit"] == "ohm"
    assert resistor["series"] == "E12"
    assert len(resistor["values"]) == 84
    assert resistor["ratings_w"] == [0.125, 0.25, 0.5, 1, 2, 5]
    assert resistor["default_rating_w"] == 0.25
    rvalues = [e["value"] for e in resistor["values"]]
    assert 470 in rvalues
    assert 1000 in rvalues
    rlabels = {e["value"]: e["label"] for e in resistor["values"]}
    assert rlabels[470] == "470 \u03a9"
    assert rlabels[1000] == "1 k\u03a9"

    capacitor = catalog["capacitor"]
    assert capacitor["unit"] == "farad"
    assert capacitor["series"] == "E12"
    assert len(capacitor["values"]) == 120
    cvalues = [e["value"] for e in capacitor["values"]]
    assert 1e-6 in cvalues
    clabels = {e["value"]: e["label"] for e in capacitor["values"]}
    assert clabels[1e-6] == "1 \u00b5F"
    assert clabels[100e-9] == "100 nF"

    colors = catalog["led"]["colors"]
    assert [c["color"] for c in colors] == [
        "red", "green", "blue", "white", "yellow",
    ]
    red = colors[0]
    assert red["vf_v"] == 2.0
    assert red["if_warn_a"] == 0.02
    assert red["if_max_a"] == 0.03
    assert red["if_min_a"] == 0.001
    assert red["model"] == "DRED"

    assert catalog["diode"]["values"] == [
        {"value": 0.7, "label": "0.7 V (Si)"},
        {"value": 0.3, "label": "0.3 V (Schottky)"},
    ]
    assert [e["value"] for e in catalog["voltage_source"]["values"]] == [
        1.5, 3.3, 5, 9, 12, 15, 24,
    ]
    assert [e["value"] for e in catalog["current_source"]["values"]] == [
        0.001, 0.01, 0.02, 0.1, 0.5, 1,
    ]


@needs_server
def test_parts_type_filter_resistor(client: TestClient):
    resp = client.get(PARTS_PATH, params={"type": "resistor"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"version", "resistor"}
    assert data["version"] == 1
    assert len(data["resistor"]["values"]) == 84


@needs_server
def test_parts_type_filter_led(client: TestClient):
    resp = client.get(PARTS_PATH, params={"type": "led"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"version", "led"}
    assert [c["color"] for c in data["led"]["colors"]] == [
        "red", "green", "blue", "white", "yellow",
    ]


@needs_server
def test_parts_type_filter_capacitor(client: TestClient):
    resp = client.get(PARTS_PATH, params={"type": "capacitor"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"version", "capacitor"}
    assert len(data["capacitor"]["values"]) == 120


@needs_server
def test_parts_unknown_type_is_422(client: TestClient):
    resp = client.get(PARTS_PATH, params={"type": "bogus"})
    assert resp.status_code == 422


@needs_server
def test_parts_values_match_parts_module(client: TestClient):
    """Every endpoint value is a number present in parts.py (no
    JS-side table to drift, no string values)."""
    from netproof import parts as parts_mod

    catalog = client.get(PARTS_PATH).json()
    assert {e["value"] for e in catalog["resistor"]["values"]} == set(
        parts_mod.standard_resistor_values()
    )
    assert {e["value"] for e in catalog["capacitor"]["values"]} == set(
        parts_mod.standard_capacitor_values()
    )


# ---------------------------------------------------------------------------
# Fixture validity (run today: need only ir.py, no server, no ngspice).
# ---------------------------------------------------------------------------
def test_good_fixture_is_valid_netlist():
    netlist = Netlist.model_validate(good_payload())
    assert netlist.has_ground()
    assert netlist.get("R1") is not None
    assert netlist.get("R1").value == 470.0
    assert netlist.get("D1").type == "led"


def test_overcurrent_fixture_is_valid_netlist():
    netlist = Netlist.model_validate(overcurrent_payload())
    assert netlist.get("R1").value == 100.0
    # Same topology as the good circuit, only R1 differs.
    assert netlist.get("R1").nodes == Netlist.model_validate(
        good_payload()
    ).get("R1").nodes


def test_edited_fixture_changes_r1_100_to_470():
    before = Netlist.model_validate(overcurrent_payload())
    after = Netlist.model_validate(edited_payload())
    assert before.get("R1").value == 100.0
    assert after.get("R1").value == 470.0
    assert before.get("V1") == after.get("V1")
    assert before.get("D1") == after.get("D1")
