"""Shared pytest fixtures for the Netproof backend tests.

No network, no LLM key, no ngspice. Everything here is local:
fixture JSON files under ``backend/tests/fixtures/`` are loaded as
:class:`netproof.ir.Netlist` objects, the LLM client is a stub, and the
sim result is a canned dict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make ``import netproof`` work even when pytest is run without the
# ``pythonpath = ["backend"]`` setting in pyproject.toml (e.g. from a
# different working directory). conftest lives at backend/tests/, so
# parents[1] is backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netproof.ir import Netlist  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_netlist(name: str) -> Netlist:
    """Load ``backend/tests/fixtures/<name>.json`` as a Netlist."""
    path = FIXTURES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return Netlist.model_validate(data)


@pytest.fixture
def good_led() -> Netlist:
    """9 V source, 470 ohm resistor, red LED: the passing circuit."""
    return _load_netlist("good_led")


@pytest.fixture
def led_overcurrent() -> Netlist:
    """Same as good_led but with a 100 ohm resistor: LED over 30 mA."""
    return _load_netlist("led_overcurrent")


@pytest.fixture
def no_ground() -> Netlist:
    """Circuit with no ground node ``"0"`` anywhere."""
    return _load_netlist("no_ground")


@pytest.fixture
def floating_node() -> Netlist:
    """Good LED circuit plus a capacitor leg ending at a floating node."""
    return _load_netlist("floating_node")


@pytest.fixture
def shorted_source() -> Netlist:
    """Voltage source shorted across itself (nodes ``["0", "0"]``)."""
    return _load_netlist("shorted_source")


@pytest.fixture
def shorted_component() -> Netlist:
    """Resistor shorted across itself (nodes ``["vcc", "vcc"]``)."""
    return _load_netlist("shorted_component")


@pytest.fixture
def no_source() -> Netlist:
    """Resistor + LED with no power source at all."""
    return _load_netlist("no_source")


@pytest.fixture
def led_undercurrent() -> Netlist:
    """100k resistor: LED current below the 1 mA useful minimum."""
    return _load_netlist("led_undercurrent")


@pytest.fixture
def resistor_warn() -> Netlist:
    """270 ohm resistor: resistor dissipation near its 0.25 W rating."""
    return _load_netlist("resistor_warn")


@pytest.fixture
def reversed_led() -> Netlist:
    """LED wired cathode-to-vcc (anode/cathode swapped)."""
    return _load_netlist("reversed_led")


@pytest.fixture
def source_overcurrent() -> Netlist:
    """1 ohm resistor straight across the 9 V source."""
    return _load_netlist("source_overcurrent")


class FakeLLMClient:
    """Anthropic-shaped LLM stub. No network, no API key.

    ``client.messages.create(...)`` returns a response whose first
    content block holds JSON text. The canned payload defaults to the
    ``good_led`` netlist JSON; override ``canned_text`` (or pass
    ``canned_text=...``) to return something else. Every call is
    recorded in ``calls`` as its kwargs dict.
    """

    def __init__(self, canned_text: str | None = None) -> None:
        if canned_text is None:
            canned_text = (
                FIXTURES_DIR / "good_led.json"
            ).read_text(encoding="utf-8")
        self.canned_text = canned_text
        self.calls: list[dict] = []
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, client: "FakeLLMClient") -> None:
            self._client = client

        def create(self, **kwargs) -> SimpleNamespace:
            self._client.calls.append(dict(kwargs))
            return SimpleNamespace(
                content=[SimpleNamespace(text=self._client.canned_text)],
                model=kwargs.get("model", "fake-model"),
                stop_reason="end_turn",
            )


@pytest.fixture
def fake_llm_client() -> FakeLLMClient:
    """Stub LLM client; ``messages.create`` returns canned JSON text."""
    return FakeLLMClient()


@pytest.fixture
def fake_sim_result() -> dict:
    """Canned ngspice-style result for the good_led circuit.

    Plain dict (no SimResult model exists yet) with the same keys a
    :class:`netproof.ir.Report` carries: ``node_voltages`` (V),
    ``branch_currents`` (A), ``power`` (W). Values are the hand
    calculation for (9 V - 2 V) / 470 ohm = 14.9 mA.
    """
    current = (9.0 - 2.0) / 470.0  # ~0.0149 A
    return {
        "node_voltages": {"vcc": 9.0, "led": 2.0, "0": 0.0},
        "branch_currents": {"V1": -current, "R1": current, "D1": current},
        "power": {
            "R1": current**2 * 470.0,
            "D1": 2.0 * current,
            "V1": -9.0 * current,
        },
    }
