"""Endpoint-drift regression tests (audit R1-R9, 2026-11-15).

File owner: backend/tests/test_endpoint_drift.py ONLY.

Covers the backend/frontend contract fixes, not the 3D layer:
  R1  GET /download/netlist and /download/bom are gone (POST only);
      a bodyless GET used to 400 on the missing JSON body.
  R2  GET /download/netlist.cir and /download/bom.csv serve the last
      checked netlist: 404 before any check, working after one, and
      the delivered BOM filename is lowercase "bom.csv" on both routes.
  R3  The terminal SSE `result` event carries `done: true` (mock and
      real); stream close stays a terminator too.
  R4  No progress event carries a netlist (the old spice_render one
      did, and the UI rendered it mid-stream, before any verdict).
  R5  (UI-side, pinned by string checks below) only result-bearing
      events become the stream's "last" event; error events never do.
  R6  Errors share one shape: {"error", "message", ...}; request
      validation is {"error": "validation_error", ...}, never
      FastAPI's default {"detail": [...]}.
  R7  /repair verification failure is HTTP 200 with verified:false,
      report:null, verdict:null (repaired rows preserved); success
      has reasons == changes and netlist as table rows, not a Netlist.
  R8  Mock reports carry exactly the real Report keys (no "spice" /
      "summary" extras).
  R9  Mock SSE stages match the real stream's stages (goal has no
      "generate" stage); mock timing is instant by design.
  (1) Case-insensitive lookups: Report keys are lowercase ("v1",
      "n1") while row ids / nodes may be upper case ("V1", "N1").

No network, no LLM key, no ngspice: a fake simulator is injected
via server.set_simulator, and llm.* is stubbed with monkeypatch
where a real stream / repair needs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from netproof import llm as llm_module
from netproof import server as server_module
from netproof.ir import Netlist

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


class _AnalyticSeriesSimulator:
    """Fake simulator for the 9 V source / resistor / LED loop.

    Returns lowercase keys, exactly like spice.parse_op_output does
    (ngspice lowercases node names and branch-current keys).
    """

    def run(self, netlist: Netlist) -> dict:
        comps = {c.id: c for c in netlist.components}
        source = next(c for c in netlist.components if c.type == "voltage_source")
        resistor = next(c for c in netlist.components if c.type == "resistor")
        led = next(c for c in netlist.components if c.type == "led")
        current = (source.value - led.value) / resistor.value
        return {
            "node_voltages": {"n1": 9.0, "n2": 2.0, "0": 0.0},
            "branch_currents": {
                source.id.lower(): -current,
                resistor.id.lower(): current,
                led.id.lower(): current,
            },
            "power": {
                resistor.id.lower(): current**2 * resistor.value,
                led.id.lower(): current * led.value,
                source.id.lower(): -9.0 * current,
            },
            "components": comps,
        }


def good_payload() -> dict:
    return {
        "title": "9V LED circuit (good)",
        "components": [
            {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["n1", "0"]},
            {"id": "R1", "type": "resistor", "value": 470.0, "nodes": ["n1", "n2"], "rating_watts": 0.25},
            {"id": "D1", "type": "led", "value": 2.0, "nodes": ["n2", "0"], "color": "red"},
        ],
    }


@pytest.fixture
def client():
    server_module.set_simulator(_AnalyticSeriesSimulator())
    with TestClient(server_module.app) as test_client:
        yield test_client
    server_module.set_simulator(None)


def _sse_frames(text: str) -> list[dict]:
    """Parse SSE text into [{"event": ..., "data": {...}}, ...]."""
    frames: list[dict] = []
    event = "message"
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            frames.append({"event": event, "data": json.loads(line[len("data:"):].strip())})
            event = "message"
    return frames


# ---------------------------------------------------------------------------
# R1: no bodyless GET downloads.
# ---------------------------------------------------------------------------
def test_get_download_netlist_and_bom_are_gone(client: TestClient):
    """GET /download/netlist and GET /download/bom are not routes
    anymore (they 400'd on the missing body). The request now falls
    through to the static mount and 404s; it must never again be the
    old 400 {"error": "invalid_json"}."""
    for path in ("/download/netlist", "/download/bom"):
        resp = client.get(path)
        assert resp.status_code == 404
        assert resp.json().get("error") != "invalid_json"


def test_post_download_netlist_still_works(client: TestClient):
    resp = client.post("/download/netlist", json=good_payload())
    assert resp.status_code == 200
    assert "netlist.cir" in resp.headers.get("content-disposition", "")
    assert ".op" in resp.text.lower()


def test_post_download_bom_still_works(client: TestClient):
    resp = client.post("/download/bom", json=good_payload())
    assert resp.status_code == 200
    assert "bom.csv" in resp.headers.get("content-disposition", "").lower()
    assert resp.text.splitlines()[0] == "id,type,value,rating,qty"


# ---------------------------------------------------------------------------
# R2: .cir / .csv serve the last checked netlist (404 before any check).
# ---------------------------------------------------------------------------
def test_download_cir_csv_404_before_any_check_then_work(client: TestClient):
    server_module._LAST_NETLIST = None
    missing = client.get("/download/netlist.cir")
    assert missing.status_code == 404
    assert missing.json()["error"] == "no_netlist"
    assert client.get("/download/bom.csv").status_code == 404

    checked = client.post("/check", json=good_payload())
    assert checked.status_code == 200

    cir = client.get("/download/netlist.cir")
    assert cir.status_code == 200
    assert ".op" in cir.text.lower()
    bom = client.get("/download/bom.csv")
    assert bom.status_code == 200
    # Lowercase filename on BOTH routes (GET used to say "BOM.csv").
    assert 'filename="bom.csv"' in bom.headers.get("content-disposition", "")
    assert bom.text.splitlines()[0] == "id,type,value,rating,qty"


# ---------------------------------------------------------------------------
# R3/R4/R9: SSE terminator, stages, and no netlist in progress events.
# ---------------------------------------------------------------------------
def test_mock_goal_stages_match_real_and_final_has_done(client: TestClient):
    frames = _sse_frames(client.post("/run-goal?mock=1", json={"goal": "led"}).text)
    progress = [f["data"] for f in frames if f["event"] == "progress"]
    # Goal mock stages == real goal stages: no invented "generate".
    assert [p["stage"] for p in progress] == [
        "started", "plan", "spice_render", "simulate", "rules_check",
    ]
    assert all("netlist" not in p for p in progress)
    result = [f["data"] for f in frames if f["event"] == "result"]
    assert len(result) == 1
    assert result[0]["done"] is True
    assert result[0]["mock"] is True
    assert result[0]["verdict"] == "PASS"


def test_mock_image_stages_and_final_has_done(client: TestClient):
    frames = _sse_frames(
        client.post(
            "/run-image?mock=1",
            files={"image": ("schematic.png", b"\x89PNG fake", "image/png")},
        ).text
    )
    progress = [f["data"] for f in frames if f["event"] == "progress"]
    assert [p["stage"] for p in progress] == [
        "started", "vision_extract", "spice_render", "simulate", "rules_check",
    ]
    result = [f["data"] for f in frames if f["event"] == "result"]
    assert len(result) == 1 and result[0]["done"] is True


def test_real_sse_final_has_done_and_progress_carries_no_netlist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Real /run-goal (llm.design stubbed): final result has done:true
    and no progress event carries a netlist (old spice_render did)."""
    monkeypatch.setattr(
        llm_module, "design",
        lambda goal, client=None: Netlist.model_validate(good_payload()),
    )
    frames = _sse_frames(client.post("/run-goal", json={"goal": "led"}).text)
    progress = [f["data"] for f in frames if f["event"] == "progress"]
    assert progress, "expected progress events before the result"
    assert all("netlist" not in p for p in progress)
    assert all("verdict" not in p and "findings" not in p for p in progress)
    result = [f["data"] for f in frames if f["event"] == "result"]
    assert len(result) == 1
    assert result[0]["done"] is True
    assert result[0]["verdict"] == "PASS"
    assert result[0]["mock"] is False


# ---------------------------------------------------------------------------
# R8: mock reports have exactly the real Report keys.
# ---------------------------------------------------------------------------
def test_mock_check_has_no_spice_or_summary_fields(client: TestClient):
    resp = client.post("/check?mock=1", json=good_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["mock"] is True
    assert "spice" not in body
    assert "summary" not in body
    real = client.post("/check", json=good_payload()).json()
    assert set(body) == set(real)


# ---------------------------------------------------------------------------
# R6: one error shape for validation failures.
# ---------------------------------------------------------------------------
def test_validation_error_shape(client: TestClient):
    resp = client.post("/check", json={"components": "not-a-list"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["detail"], list)

    # The pre-existing invalid-component case still 422s, now with
    # the same shape instead of FastAPI's {"detail": [...]}.
    bad = good_payload()
    bad["components"] = [
        {**c, "value": -5.0} if c["id"] == "R1" else c for c in bad["components"]
    ]
    resp = client.post("/check", json=bad)
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


# ---------------------------------------------------------------------------
# R7: repair verification failure is 200 + verified:false (rows kept);
# success has reasons == changes and netlist as table rows.
# ---------------------------------------------------------------------------
def _repair_body() -> dict:
    return {
        "netlist": good_payload(),
        "findings": [
            {
                "severity": "STOP",
                "rule_id": "LED_OVERCURRENT",
                "component_ids": ["D1"],
                "message": "LED D1 current exceeds the limit.",
            }
        ],
        "round": 1,
    }


def test_repair_unverified_returns_200_with_verified_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        llm_module, "repair",
        lambda netlist, findings, client=None: (
            Netlist.model_validate(good_payload()), ["changed R1 100 -> 470"],
        ),
    )

    def _boom(netlist):
        raise HTTPException(
            status_code=500,
            detail={"error": "simulation_error", "message": "ngspice down"},
        )

    monkeypatch.setattr(server_module, "_verify_netlist", _boom)
    resp = client.post("/repair", json=_repair_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False
    assert body["report"] is None
    assert body["verdict"] is None
    assert body["findings"] == []
    assert body["verification_error"]
    # The repaired rows survive for manual inspection.
    assert isinstance(body["netlist"], list)
    assert [row["id"] for row in body["netlist"]] == ["V1", "R1", "D1"]


def test_repair_success_reasons_equal_changes_and_netlist_is_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        llm_module, "repair",
        lambda netlist, findings, client=None: (
            Netlist.model_validate(good_payload()), ["changed R1 100 -> 470"],
        ),
    )
    resp = client.post("/repair", json=_repair_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["verdict"] == "PASS"
    assert body["reasons"] == body["changes"] == ["changed R1 100 -> 470"]
    # netlist is editable table rows, NOT an ir.Netlist object.
    assert isinstance(body["netlist"], list)
    row = body["netlist"][0]
    assert set(row) >= {"id", "type", "value", "nodes", "current", "power"}
    assert row["id"] == "V1" and row["current"] is not None


# ---------------------------------------------------------------------------
# Health status is not always "ok".
# ---------------------------------------------------------------------------
def test_health_ok_when_healthy(client: TestClient):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["simulator_override"] is True


def test_health_degraded_when_a_module_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        server_module, "_sibling_status",
        lambda: {name: True for name in server_module._SIBLINGS} | {"llm": False},
    )
    assert client.get("/health").json()["status"] == "degraded"


# ---------------------------------------------------------------------------
# (1) Case-insensitive lookups: uppercase ids / nodes vs lowercase
# Report keys from spice.parse_op_output.
# ---------------------------------------------------------------------------
def test_check_rows_case_insensitive_for_uppercase_ids_and_nodes(
    client: TestClient,
):
    payload = good_payload()
    for comp in payload["components"]:
        comp["nodes"] = [n.upper() for n in comp["nodes"]]
    resp = client.post("/check", json=payload)
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()["netlist"]}
    # Report keys are "v1"/"n1"; row ids/nodes are "V1"/"N1".
    assert rows["V1"]["current"] is not None
    assert rows["V1"]["node_voltage"] == pytest.approx(9.0)
    assert rows["R1"]["current"] == pytest.approx((9.0 - 2.0) / 470.0)


# ---------------------------------------------------------------------------
# UI-side guards (string checks, following backend/tests/test_3d.py):
# the JS is not executed here, so these pin the exact contract lines.
# ---------------------------------------------------------------------------
def _html() -> str:
    assert INDEX_HTML.is_file(), "web/index.html missing"
    return INDEX_HTML.read_text(encoding="utf-8")


def test_ui_report_numbers_lookup_voltage_case_insensitively():
    html = _html()
    assert "lookupCI(voltages, firstNode)" in html
    assert "lookupCI(currents, row.id)" in html
    assert "lookupCI(powers, row.id)" in html


def test_ui_repair_checks_verified_before_applying():
    html = _html()
    assert "data.verified === false" in html
    assert "Repair not verified" in html
    assert "UNVERIFIED" in html


def test_ui_stream_only_keeps_result_bearing_events_as_last():
    html = _html()
    assert "isResultEvent(parsed)" in html
    assert "if (parsed) last = parsed" not in html
    assert "if (tailParsed) last = tailParsed" not in html


def test_ui_progress_events_never_apply_a_bare_netlist():
    html = _html()
    assert "if (data.verdict || data.findings || data.report) applyResult(data);" in html


def test_ui_bom_filename_is_lowercase_everywhere():
    html = _html()
    assert "'BOM.csv'" not in html
    assert "bom.csv" in html
