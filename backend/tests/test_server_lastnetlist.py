"""Tests for bench server fixes (d) + (e), in server.py only.

(d) ``_load_sibling`` must NOT cache a failed import: one transient
failure must not 501 every AI route until the process restarts. Only
successes are cached, so the next call retries the import.

(e) ``_LAST_NETLIST`` is scoped per owner (``user:<id>`` for a signed
session cookie, else ``ip:<client ip>``): one user's check must never
be served to another user by GET /download/netlist.cir or
GET /download/bom.csv, and another user's check must never overwrite
the first user's download. Downloads still serve the owning user's
own last netlist.

No real Anthropic/Gemini calls: /check runs against the analytic fake
simulator injected via ``set_simulator`` (same shape as the one in
test_server.py), and the sibling-import tests monkeypatch importlib.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from netproof import accounts as _accounts
from netproof import server as _server
from netproof.server import app

# ---------------------------------------------------------------------------
# Fake simulator (analytic series source-resistor-LED loop), copied from
# test_server.py so this file stands alone.
# ---------------------------------------------------------------------------


class _AnalyticSeriesSimulator:
    def run(self, netlist):
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


def _component(comp_id, comp_type, value, nodes, **extra):
    payload = {"id": comp_id, "type": comp_type, "value": value, "nodes": nodes}
    payload.update(extra)
    return payload


def _payload(resistor_ohms: float, title: str) -> dict:
    """9 V source + series resistor + red LED; only R1 differs."""
    return {
        "title": title,
        "components": [
            _component("V1", "voltage_source", 9.0, ["n1", "0"]),
            _component(
                "R1", "resistor", resistor_ohms, ["n1", "n2"],
                rating_watts=0.25,
            ),
            _component("D1", "led", 2.0, ["n2", "0"], color="red"),
        ],
    }


@pytest.fixture
def client():
    """TestClient with the fake simulator installed (so the lifespan
    ngspice check passes without ngspice on PATH)."""
    _server.set_simulator(_AnalyticSeriesSimulator())
    ctx = TestClient(app)
    test_client = ctx.__enter__()
    yield test_client
    ctx.__exit__(None, None, None)
    _server.set_simulator(None)


@pytest.fixture(autouse=True)
def _isolate_last_netlist():
    """Each test starts with an empty last-netlist store and restores
    whatever was there, so tests cannot leak downloads into each other
    (or into later test modules)."""
    saved = _server._LAST_NETLIST
    _server._LAST_NETLIST = {}
    yield
    _server._LAST_NETLIST = saved


def _session_headers(user_id: int) -> dict:
    """Cookie header for a signed-in user; no DB row needed: _owner_key
    only verifies the HMAC signature via accounts.read_session. Sent
    as a header (not TestClient's per-request cookies=) to avoid the
    deprecated per-request cookie jar behaviour."""
    return {"cookie": f"{_accounts.COOKIE}={_accounts.sign_session(user_id)}"}


# ---------------------------------------------------------------------------
# (d) _load_sibling retries after a transient failure
# ---------------------------------------------------------------------------


def test_load_sibling_retries_after_transient_failure(monkeypatch):
    """A first call that fails on every attempt returns None and caches
    NOTHING; the next call retries and succeeds. Before the fix the
    None was cached, so every later call 501'd until restart."""
    import importlib

    real_import = importlib.import_module
    real_spec = importlib.util.spec_from_file_location
    calls = {"import": 0, "spec": 0}

    def flaky_import(name, *args, **kwargs):
        calls["import"] += 1
        if calls["import"] <= 2:  # attempts 0 and 1 of the first call
            raise ImportError("transient boom")
        return real_import(name, *args, **kwargs)

    def flaky_spec(*args, **kwargs):
        calls["spec"] += 1
        if calls["spec"] == 1:  # attempt 2 of the first call
            raise ImportError("transient boom")
        return real_spec(*args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", flaky_import)
    monkeypatch.setattr(
        importlib.util, "spec_from_file_location", flaky_spec
    )
    _server._module_cache.pop("rules", None)
    _server._module_error.pop("rules", None)

    assert _server._load_sibling("rules") is None
    assert "rules" not in _server._module_cache
    assert "rules" in _server._module_error

    module = _server._load_sibling("rules")
    assert module is not None
    assert module is _server._module_cache["rules"]
    assert "rules" not in _server._module_error


def test_load_sibling_caches_successes(monkeypatch):
    """A successful import is cached: the second call performs no
    import at all."""
    import importlib

    real_import = importlib.import_module
    calls = {"n": 0}

    def counting_import(name, *args, **kwargs):
        calls["n"] += 1
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", counting_import)
    _server._module_cache.pop("rules", None)

    first = _server._load_sibling("rules")
    assert first is not None
    n_after_first = calls["n"]
    assert n_after_first >= 1

    second = _server._load_sibling("rules")
    assert second is first
    assert calls["n"] == n_after_first


def test_load_sibling_missing_module_returns_none_without_caching():
    """A genuinely absent module returns None on every call, records
    the error for /health, and still does not poison the cache."""
    assert _server._load_sibling("no_such_sibling_xyz") is None
    assert "no_such_sibling_xyz" not in _server._module_cache
    assert _server._module_error.get("no_such_sibling_xyz")
    assert _server._load_sibling("no_such_sibling_xyz") is None


# ---------------------------------------------------------------------------
# (e) _LAST_NETLIST is scoped per owner
# ---------------------------------------------------------------------------


def test_last_netlist_isolated_between_signed_in_users(client):
    """User A checks a 470 ohm circuit; user B must NOT get A's deck
    (404 before B checks), and after B checks a 100 ohm circuit, A's
    download must still be the 470 ohm deck, not B's."""
    alice = _session_headers(1)
    bob = _session_headers(2)

    checked = client.post(
        "/check", json=_payload(470.0, "alice circuit"), headers=alice
    )
    assert checked.status_code == 200

    # The leak, pre-fix: B would have been served A's netlist here.
    assert client.get("/download/netlist.cir", headers=bob).status_code == 404
    assert client.get("/download/bom.csv", headers=bob).status_code == 404

    checked_bob = client.post(
        "/check", json=_payload(100.0, "bob circuit"), headers=bob
    )
    assert checked_bob.status_code == 200

    cir_alice = client.get("/download/netlist.cir", headers=alice)
    assert cir_alice.status_code == 200
    assert "470" in cir_alice.text
    assert "100" not in cir_alice.text
    bom_alice = client.get("/download/bom.csv", headers=alice)
    assert bom_alice.status_code == 200
    assert "470" in bom_alice.text

    cir_bob = client.get("/download/netlist.cir", headers=bob)
    assert cir_bob.status_code == 200
    assert "100" in cir_bob.text
    assert "470" not in cir_bob.text
    bom_bob = client.get("/download/bom.csv", headers=bob)
    assert bom_bob.status_code == 200
    assert "100" in bom_bob.text


def test_last_netlist_isolated_between_anonymous_ips(client):
    """Without a session cookie the owner key falls back to client IP,
    so two different IPs still cannot see each other's netlists."""
    ip_a = {"x-forwarded-for": "10.0.0.1"}
    ip_b = {"x-forwarded-for": "10.0.0.2"}

    assert (
        client.post("/check", json=_payload(470.0, "a"), headers=ip_a).status_code
        == 200
    )
    assert client.get("/download/netlist.cir", headers=ip_b).status_code == 404

    assert (
        client.post("/check", json=_payload(100.0, "b"), headers=ip_b).status_code
        == 200
    )
    cir_a = client.get("/download/netlist.cir", headers=ip_a)
    assert cir_a.status_code == 200
    assert "470" in cir_a.text and "100" not in cir_a.text
    cir_b = client.get("/download/netlist.cir", headers=ip_b)
    assert cir_b.status_code == 200
    assert "100" in cir_b.text and "470" not in cir_b.text


def test_same_user_second_check_overwrites_own_download(client):
    """Scoping is per user, not per check: the same user's second
    check replaces their own download (the old same-tab behaviour)."""
    alice = _session_headers(1)
    assert (
        client.post("/check", json=_payload(470.0, "v1"), headers=alice).status_code
        == 200
    )
    assert (
        client.post("/check", json=_payload(100.0, "v2"), headers=alice).status_code
        == 200
    )
    cir = client.get("/download/netlist.cir", headers=alice)
    assert cir.status_code == 200
    assert "100" in cir.text
    assert "470" not in cir.text


def test_mock_check_records_per_owner(client):
    """A mock /check also records under the caller's owner only."""
    alice = _session_headers(1)
    bob = _session_headers(2)
    assert (
        client.post(
            "/check?mock=1", json=_payload(470.0, "mock"), headers=alice
        ).status_code
        == 200
    )
    assert client.get("/download/netlist.cir", headers=bob).status_code == 404
    cir = client.get("/download/netlist.cir", headers=alice)
    assert cir.status_code == 200
    assert "470" in cir.text


def test_legacy_none_assignment_still_clears_store(client):
    """Older clearing code (``server._LAST_NETLIST = None``, as in
    test_endpoint_drift.py) must still read as an empty store, not
    crash the download routes."""
    assert (
        client.post("/check", json=_payload(470.0, "x")).status_code == 200
    )
    assert client.get("/download/netlist.cir").status_code == 200
    _server._LAST_NETLIST = None
    assert client.get("/download/netlist.cir").status_code == 404
    assert client.get("/download/bom.csv").status_code == 404


def test_record_and_lookup_helpers_are_per_owner():
    """Unit level: recording under two owners keeps both, and an
    unknown owner reads None (never another owner's netlist)."""
    _server._record_last_netlist("user:1", "netlist-one")
    _server._record_last_netlist("user:2", "netlist-two")
    assert _server._last_netlist_for("user:1") == "netlist-one"
    assert _server._last_netlist_for("user:2") == "netlist-two"
    assert _server._last_netlist_for("user:3") is None
    _server._LAST_NETLIST = None
    assert _server._last_netlist_for("user:1") is None
