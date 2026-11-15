"""Tests for the SPICE layer: ``backend/netproof/spice.py``.

Status at time of writing (2026-11-15, agent 16/50)
---------------------------------------------------
``backend/netproof/spice.py`` was ABSENT when this file was first
written, then landed at 16:56 (589 lines, parallel agent) while the
first pytest run was in progress. The tests below were re-checked
against the real implementation and updated where the spec guess
differed (see "Verified API"). The ``ngspice`` binary is still NOT
installed in this environment, so the two ``run_ngspice`` tests skip
with a clear reason until ngspice is installed.

* The pure-Python tests (``netlist_to_spice`` rendering and
  ``parse_op_output`` on literal sample strings) need no binary and run
  now.
* Every test that actually executes ngspice is gated on
  ``shutil.which("ngspice")`` (``pytest.mark.skipif``), per the task.

Verified API (``backend/netproof/spice.py``, read 2026-11-15)
-------------------------------------------------------------
* ``netlist_to_spice(netlist: Netlist) -> str``: title line (``* <title>``),
  ``.model`` lines, one component line each, ``.op``, a ``.control``
  block (``op`` + ``print all``), ``.endc``, ``.end``. Values use SPICE
  engineering suffixes (470 -> ``470``, 1000 -> ``1k``, 1e-6 -> ``1u``,
  0.01 -> ``10m``). LED model lines come from
  ``netproof.parts.diode_model_line`` (``.model DRED D(...)`` for red).
* ``parse_op_output(text: str) -> (node_voltages, branch_currents)``:
  a TUPLE of two dicts. Node keys are bare names (``"vcc"``); ground
  node ``"0"`` is deliberately OMITTED (it is 0 by definition).
  Current keys are source refs (``"v1"`` from ``i(v1) = ...``).
  Unparseable / empty text yields ``({}, {})``. Needs no binary.
* ``run_ngspice(spice_text: str) -> SimResult``: takes a SPICE DECK
  STRING (not a Netlist), runs ``ngspice -b`` (30 s timeout), returns
  a ``SimResult`` dataclass with ``.node_voltages``,
  ``.branch_currents``, ``.raw_output`` and ``.success``. Raises
  ``RuntimeError`` if the ngspice binary is missing or the run times
  out; a failed simulation returns ``success=False`` instead of raising.
* ``current_through_resistor(v_plus, v_minus, resistance)`` /
  ``resistor_power(...)``: pure Ohm-law helpers, used here only as a
  cross-check on the derived LED current.

Physics note on the good-LED expectations
-----------------------------------------
The canonical circuit (``backend/tests/fixtures/good_led.json``) is a
9 V source, a 470 ohm series resistor, and a red LED (``led_vf("red")``
is 2.0 V, ``parts.py``). The task expects ``v(led)`` ~2 V and LED
current ~15 mA. The diode model in ``parts.diode_model_line`` is now
tuned per color (agent 56, 2026-11-15: red IS=1e-18, N=2.0, RS=5)
so the diode equation gives Vf = 2.00 V at 15 mA; the 9 V / 470 ohm
circuit therefore simulates at v(led) = 2.00 V, I = 14.9 mA (Python
evaluation of the diode equation; live ngspice confirmation still
pending on a Mac). The run tests accordingly assert ``v(led)`` at
2.0 V +/- 0.3 V and the current at 15 mA +/- 5 mA. (The pre-tuning
model, IS=1e-14 / N=1.5, gave only ~1.1 V at ~17 mA, which is why
the bound used to be the loose 1.0-2.5 V range.)

No network, no LLM key: everything here is local and deterministic.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

# --- spice module: skip the whole file while it does not exist yet -------
try:
    import netproof.spice as spice  # noqa: F401  (imported for its side effect: proving it exists)
except ImportError as _exc:  # backend/netproof/spice.py not implemented yet
    pytest.skip(
        f"backend/netproof/spice.py not implemented yet ({_exc}); "
        "netlist_to_spice / run_ngspice / parse_op_output cannot be tested",
        allow_module_level=True,
    )

# Module exists at this point: missing names must fail loudly, not skip.
from netproof.spice import (
    current_through_resistor,
    netlist_to_spice,
    parse_op_output,
    run_ngspice,
)

from netproof.ir import Component, ComponentType, Netlist
from netproof.parts import diode_model_line, led_vf, model_name

FIXTURES_DIR = Path(__file__).parent / "fixtures"

requires_ngspice = pytest.mark.skipif(
    shutil.which("ngspice") is None,
    reason="ngspice binary not installed (brew install ngspice)",
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
def load_fixture_netlist(name: str) -> Netlist:
    """Load a fixture JSON as a Netlist (fixtures are Netlist-shaped)."""
    path = FIXTURES_DIR / name
    with path.open() as f:
        return Netlist.model_validate(json.load(f))


def good_led_inline() -> Netlist:
    """Inline copy of fixtures/good_led.json (fallback if the file moves)."""
    return Netlist(
        title="good LED 9V",
        components=[
            Component(
                id="V1",
                type=ComponentType.VOLTAGE_SOURCE,
                value=9.0,
                nodes=["vcc", "0"],
            ),
            Component(
                id="R1",
                type=ComponentType.RESISTOR,
                value=470.0,
                nodes=["vcc", "led"],
                rating_watts=0.25,
            ),
            Component(
                id="D1",
                type=ComponentType.LED,
                value=2.0,
                nodes=["led", "0"],
                color="red",
            ),
        ],
    )


@pytest.fixture
def good_led_netlist() -> Netlist:
    """The canonical good LED circuit: 9 V, 470 ohm, red LED.

    Prefers fixtures/good_led.json; falls back to the inline copy.
    """
    try:
        return load_fixture_netlist("good_led.json")
    except FileNotFoundError:
        return good_led_inline()


@pytest.fixture
def good_led_spice(good_led_netlist: Netlist) -> str:
    return netlist_to_spice(good_led_netlist)


def _norm_key(key: str) -> str:
    """Normalise a result key: lowercase, strip v(...)/i(...) wrappers."""
    k = key.strip().lower()
    k = re.sub(r"^[vi]\((.*)\)$", r"\1", k)  # v(vcc) -> vcc, i(v1) -> v1
    k = k.replace("@", "").replace("[i]", "").strip()
    return k


def _as_mapping(result, *attr_names: str) -> dict[str, float]:
    """Pull a {name: float} mapping out of a result, whatever its shape.

    Accepts a dict (either ``{"node_voltages": {...}}`` or a bare
    ``{node: value}`` dict) or an object with one of the named attrs.
    """
    if isinstance(result, dict):
        for name in attr_names:
            if name in result and isinstance(result[name], dict):
                return {_norm_key(k): float(v) for k, v in result[name].items()}
        if result and all(isinstance(v, (int, float)) for v in result.values()):
            return {_norm_key(k): float(v) for k, v in result.items()}
        return {}
    for name in attr_names:
        mapping = getattr(result, name, None)
        if isinstance(mapping, dict):
            return {_norm_key(k): float(v) for k, v in mapping.items()}
    return {}


def node_voltages(result) -> dict[str, float]:
    # parse_op_output returns a (voltages, currents) tuple.
    if isinstance(result, tuple) and len(result) == 2:
        return {_norm_key(k): float(v) for k, v in result[0].items()}
    return _as_mapping(result, "node_voltages", "voltages", "node_voltage")


def branch_currents(result) -> dict[str, float]:
    # parse_op_output returns a (voltages, currents) tuple.
    if isinstance(result, tuple) and len(result) == 2:
        return {_norm_key(k): float(v) for k, v in result[1].items()}
    return _as_mapping(result, "branch_currents", "currents", "branch_current")


def _find_current(currents: dict[str, float], *ids: str) -> float | None:
    """First available branch current for any of ``ids`` (by component id)."""
    for cid in ids:
        key = _norm_key(cid)
        if key in currents:
            return currents[key]
    return None


# Literal ngspice operating-point output (no binary needed to parse it).
# Format mirrors ngspice batch ``.op`` output: header lines plus
# ``v(<node>) = <value>`` / ``i(<source>) = <value>`` lines in scientific
# notation. Values are the good-LED circuit at ~15 mA:
#   v(led) = 1.945 V, i(v1) = -14.98 mA (negative: current leaves vcc).
SAMPLE_OP_OUTPUT = """\
Circuit: good LED 9V
No. of Data Rows : 1
Operating point:
  v(vcc) = 9.000000e+00
  v(led) = 1.945123e+00
  v(0) = 0.000000e+00
  i(v1) = -1.497800e-02
"""


# --------------------------------------------------------------------------
# netlist_to_spice: deck structure (no ngspice binary needed)
# --------------------------------------------------------------------------
def test_netlist_to_spice_has_op_and_end(good_led_spice: str) -> None:
    assert isinstance(good_led_spice, str)
    assert good_led_spice.strip()
    lines = [ln.strip().lower() for ln in good_led_spice.splitlines()]
    assert ".op" in lines, f"deck has no .op line:\n{good_led_spice}"
    assert ".end" in lines, f"deck has no .end line:\n{good_led_spice}"
    assert lines.index(".op") < lines.index(".end")


def test_netlist_to_spice_has_led_model_line(good_led_spice: str) -> None:
    # Exact line from netproof.parts, the single source of LED models.
    assert led_vf("red") == pytest.approx(2.0)
    assert model_name("red") == "DRED"
    assert diode_model_line("red") in good_led_spice
    assert ".model DRED" in good_led_spice


def test_netlist_to_spice_component_lines(good_led_spice: str) -> None:
    # One SPICE line per component, in IR order, with the right nodes.
    # Value spellings are matched loosely (470 / 470.0 / 470ohm all pass).
    assert re.search(r"(?m)^V1\s+vcc\s+0\s+(?:DC\s+)?9", good_led_spice), (
        f"no V1 9V source line in:\n{good_led_spice}"
    )
    assert re.search(r"(?m)^R1\s+vcc\s+led\s+470", good_led_spice), (
        f"no R1 470 ohm line in:\n{good_led_spice}"
    )
    assert re.search(r"(?m)^D1\s+led\s+0\s+DRED\b", good_led_spice), (
        f"no D1 LED line using model DRED in:\n{good_led_spice}"
    )


def test_netlist_to_spice_title_present(
    good_led_netlist: Netlist, good_led_spice: str
) -> None:
    assert good_led_netlist.title == "good LED 9V"
    assert "good LED 9V" in good_led_spice


def test_netlist_to_spice_no_led_means_no_led_model() -> None:
    netlist = Netlist(
        title="source and resistor only",
        components=[
            Component(
                id="V1",
                type=ComponentType.VOLTAGE_SOURCE,
                value=9.0,
                nodes=["vcc", "0"],
            ),
            Component(
                id="R1",
                type=ComponentType.RESISTOR,
                value=470.0,
                nodes=["vcc", "0"],
            ),
        ],
    )
    text = netlist_to_spice(netlist)
    assert ".op" in text.lower() and ".end" in text.lower()
    assert ".model DRED" not in text
    assert re.search(r"(?m)^V1\s+vcc\s+0\s+(?:DC\s+)?9", text)
    assert re.search(r"(?m)^R1\s+vcc\s+0\s+470", text)


def test_netlist_to_spice_capacitor_and_current_source_lines() -> None:
    netlist = Netlist(
        title="cap and current source",
        components=[
            Component(
                id="V1",
                type=ComponentType.VOLTAGE_SOURCE,
                value=9.0,
                nodes=["vcc", "0"],
            ),
            Component(
                id="R1",
                type=ComponentType.RESISTOR,
                value=1000.0,
                nodes=["vcc", "out"],
            ),
            Component(
                id="C1",
                type=ComponentType.CAPACITOR,
                value=1e-6,
                nodes=["out", "0"],
            ),
            Component(
                id="I1",
                type=ComponentType.CURRENT_SOURCE,
                value=0.01,
                nodes=["out", "0"],
            ),
        ],
    )
    text = netlist_to_spice(netlist)
    assert re.search(r"(?m)^C1\s+out\s+0\s+\S+", text), f"no C1 line in:\n{text}"
    assert re.search(r"(?m)^I1\s+out\s+0\s+\S+", text), f"no I1 line in:\n{text}"


# --------------------------------------------------------------------------
# parse_op_output: literal sample text (no ngspice binary needed)
# --------------------------------------------------------------------------
def test_parse_op_output_sample() -> None:
    result = parse_op_output(SAMPLE_OP_OUTPUT)
    voltages = node_voltages(result)
    assert voltages["vcc"] == pytest.approx(9.0, abs=0.01)
    assert voltages["led"] == pytest.approx(1.945123, abs=0.001)
    # Ground node "0" is deliberately omitted by the parser (0 by definition).
    assert "0" not in voltages
    assert voltages.get("0", 0.0) == pytest.approx(0.0, abs=1e-9)
    currents = branch_currents(result)
    # Current through V1 is reported negative (leaving vcc); magnitude ~15 mA.
    i_v1 = _find_current(currents, "v1")
    assert i_v1 is not None, f"no V1 current parsed from {currents!r}"
    assert abs(i_v1) == pytest.approx(0.014978, abs=0.0005)


def test_parse_op_output_compact_format() -> None:
    # Tolerates missing spaces around "="; bare node names without v(...)
    # are NOT a supported form, so every voltage uses v(<node>).
    result = parse_op_output("v(vcc)=9.0\nv(led) = 2.0\nv(0)=0.0\ni(v1)=-0.0149\n")
    voltages = node_voltages(result)
    assert voltages["vcc"] == pytest.approx(9.0, abs=0.01)
    assert voltages["led"] == pytest.approx(2.0, abs=0.01)
    assert "0" not in voltages
    currents = branch_currents(result)
    i_v1 = _find_current(currents, "v1")
    assert i_v1 is not None
    assert abs(i_v1) == pytest.approx(0.0149, abs=0.0005)


def test_parse_op_output_tabular_and_branch_forms() -> None:
    # Tabular "node value" rows inside an operating-point section, plus
    # the v1#branch current form.
    text = (
        "Operating point\n"
        "  vcc   9.000000e+00\n"
        "  led   1.945000e+00\n"
        "  v1#branch = -1.497800e-02\n"
    )
    voltages, currents = parse_op_output(text)
    assert voltages["vcc"] == pytest.approx(9.0, abs=0.01)
    assert voltages["led"] == pytest.approx(1.945, abs=0.001)
    assert abs(currents["v1"]) == pytest.approx(0.014978, abs=0.0005)


def test_parse_op_output_empty_returns_empty() -> None:
    assert parse_op_output("") == ({}, {})
    assert parse_op_output("nothing useful here\n") == ({}, {})


# --------------------------------------------------------------------------
# run_ngspice: real simulation (skipped unless the binary is installed)
# --------------------------------------------------------------------------
@requires_ngspice
def test_run_ngspice_good_led_voltages(good_led_spice: str) -> None:
    # run_ngspice takes a SPICE deck string, not a Netlist.
    result = run_ngspice(good_led_spice)
    assert result.success, f"ngspice run failed:\n{result.raw_output}"
    voltages = node_voltages(result)
    assert voltages["vcc"] == pytest.approx(9.0, abs=0.1)
    # Forward-biased red LED, model tuned to led_vf (parts.py, agent 56):
    # predicted v(led) = 2.00 V at 14.9 mA in the 9 V / 470 ohm circuit.
    assert 1.7 <= voltages["led"] <= 2.3, f"v(led)={voltages['led']}"
    if "0" in voltages:
        assert voltages["0"] == pytest.approx(0.0, abs=1e-9)


@requires_ngspice
def test_run_ngspice_good_led_current_derived(good_led_spice: str) -> None:
    result = run_ngspice(good_led_spice)
    assert result.success, f"ngspice run failed:\n{result.raw_output}"
    voltages = node_voltages(result)
    # LED current derived from the simulated node voltages and R1 = 470 ohm:
    # I = (9 - Vled) / 470 ~ 14.9 mA with the tuned model (Vled ~ 2.0 V).
    derived = (voltages["vcc"] - voltages["led"]) / 470.0
    assert derived == pytest.approx(0.015, abs=0.005)
    # Same value from the module's own Ohm-law helper.
    assert derived == pytest.approx(
        current_through_resistor(voltages["vcc"], voltages["led"], 470.0),
        abs=1e-12,
    )
    # If the simulator also reports a branch current, it must agree.
    currents = branch_currents(result)
    reported = _find_current(currents, "v1", "r1", "d1")
    if reported is not None:
        assert abs(reported) == pytest.approx(0.015, abs=0.005)
        assert abs(reported) == pytest.approx(derived, abs=0.002)


# --------------------------------------------------------------------------
# parse_op_output: tabular fallback is section-gated (spice.py fix 1)
# --------------------------------------------------------------------------
def test_parse_op_output_tabular_outside_op_section_rejected() -> None:
    # Two-token "word number" lines outside any operating-point section
    # are prose / warnings, not node voltages: nothing may be invented.
    text = (
        "Warning: tolerance 1e-3 exceeded\n"
        "vcc   9.000000e+00\n"
        "something 2.5\n"
        "error 3.3\n"
    )
    assert parse_op_output(text) == ({}, {})


def test_parse_op_output_tabular_only_after_op_header() -> None:
    # A tabular row before the header is dropped; the same row after it
    # is a real operating-point value.
    text = "vcc 9.0\nOperating point\nled 1.945\n"
    voltages, currents = parse_op_output(text)
    assert voltages == {"led": pytest.approx(1.945)}
    assert "vcc" not in voltages
    assert currents == {}


def test_parse_op_output_tabular_after_node_voltage_header() -> None:
    # Real ngspice .op tables head their columns "Node ... Voltage"
    # (wide spacing); that header opens the section for tabular rows.
    text = "Node   Voltage\nvcc   9.000000e+00\nled   1.945000e+00\n"
    voltages, _ = parse_op_output(text)
    assert voltages["vcc"] == pytest.approx(9.0, abs=0.01)
    assert voltages["led"] == pytest.approx(1.945, abs=0.001)


def test_parse_op_output_tabular_section_ends_after_table() -> None:
    # Latent false-positive: once the section opened it never closed,
    # so a tabular-shaped prose/warning line right after the table
    # was invented as a voltage. "tolerance 1e-3" is prose, not a node.
    text = "Operating point\nvcc 9.0\ntolerance 1e-3\n"
    voltages, currents = parse_op_output(text)
    assert voltages == {"vcc": pytest.approx(9.0)}
    assert "tolerance" not in voltages
    assert currents == {}


def test_parse_op_output_tabular_section_ends_at_blank_line() -> None:
    # A blank line closes the operating-point section: a tabular row
    # after it is prose until a fresh header re-opens the section.
    text = "Operating point\nvcc 9.0\n\nled 1.945\n"
    voltages, _ = parse_op_output(text)
    assert voltages == {"vcc": pytest.approx(9.0)}
    assert "led" not in voltages


def test_parse_op_output_tabular_section_reopens_after_new_header() -> None:
    # Closing a section is not permanent: a new operating-point header
    # re-opens it, including after a blank line closed the first one.
    text = "Operating point\nvcc 9.0\n\nOperating point\nled 1.945\n"
    voltages, _ = parse_op_output(text)
    assert voltages["vcc"] == pytest.approx(9.0)
    assert voltages["led"] == pytest.approx(1.945)


# --------------------------------------------------------------------------
# netlist_to_spice: model_line allowlist (spice.py fix 2)
#
# Dict-shaped components are used here: ir.Component has no
# model / model_line fields, and netlist_to_spice accepts dicts.
# --------------------------------------------------------------------------
def _diode_with_model_line(model_line) -> list:
    return [
        {
            "ref": "D1",
            "kind": "D",
            "nodes": ["a", "0"],
            "model_line": model_line,
        }
    ]


def test_netlist_to_spice_single_token_model_line_raises() -> None:
    # Was an IndexError from str.split()[1]; must be a ValueError now.
    with pytest.raises(ValueError):
        netlist_to_spice(_diode_with_model_line(".model"))
    with pytest.raises(ValueError):
        netlist_to_spice(_diode_with_model_line("DGEN"))
    with pytest.raises(ValueError):
        netlist_to_spice(_diode_with_model_line(".model DGEN"))


@pytest.mark.parametrize(
    "bad_line",
    [
        # Multi-line: smuggled extra component line.
        ".model DGEN D(IS=1e-14)\nV99 0 0 DC 999",
        # Multi-line: smuggled control block.
        ".control\nop\n.endc",
        # Control statements, one line each.
        ".end",
        ".include /etc/passwd",
        ".lib /tmp/evil.lib",
        ".subckt BAD 1 2",
        # Unsafe characters in the parameter list.
        ".model DGEN D(IS=1e-14); rm -rf /",
        ".model DGEN D(IS=1e-14) extra",
        # Model name must be a safe identifier.
        ".model D-GEN D(IS=1e-14)",
        ".model 9GEN D(IS=1e-14)",
    ],
)
def test_netlist_to_spice_model_line_injection_rejected(bad_line: str) -> None:
    with pytest.raises(ValueError):
        netlist_to_spice(_diode_with_model_line(bad_line))


def test_netlist_to_spice_valid_model_line_accepted() -> None:
    line = ".model DGEN D(IS=1e-14 N=1.0 RS=0.1 BV=100 IBV=1e-3)"
    text = netlist_to_spice(_diode_with_model_line(line))
    assert line in text
    assert text.count(line) == 1
    assert re.search(r"(?m)^D1\s+a\s+0\s+DGEN\b", text), f"no D1/DGEN line in:\n{text}"


def test_netlist_to_spice_model_name_injection_rejected() -> None:
    # A model name is pasted onto the component line, so it must be a
    # single safe identifier too: no spaces, newlines, or punctuation.
    for bad_model in ["D GEN", "DGEN\nV99 0 0 DC 9", "DGEN;op"]:
        with pytest.raises(ValueError):
            netlist_to_spice(
                [{"ref": "D1", "kind": "D", "nodes": ["a", "0"], "model": bad_model}]
            )


# --------------------------------------------------------------------------
# netlist_to_spice: sanitised-node collision (spice.py fix 3)
# --------------------------------------------------------------------------
def test_netlist_to_spice_node_collision_raises() -> None:
    # "a-b" and "a_b" both sanitise to "a_b": silently merging them
    # would short two different nets, so this must raise.
    netlist = [
        {"ref": "R1", "kind": "R", "value": 100, "nodes": ["a-b", "0"]},
        {"ref": "R2", "kind": "R", "value": 100, "nodes": ["a_b", "0"]},
    ]
    with pytest.raises(ValueError, match="collide"):
        netlist_to_spice(netlist)


def test_netlist_to_spice_node_collision_same_spelling_ok() -> None:
    # The same node used by two components is fine, as are case-only
    # differences (SPICE node names are case-insensitive) and the
    # various ground spellings, which intentionally collapse to "0".
    netlist = [
        {"ref": "V1", "kind": "V", "value": 9, "nodes": ["VCC", "GND"]},
        {"ref": "R1", "kind": "R", "value": 100, "nodes": ["vcc", "a-b"]},
        {"ref": "R2", "kind": "R", "value": 100, "nodes": ["a-b", "gnd"]},
        {"ref": "R3", "kind": "R", "value": 100, "nodes": ["a-b", "0"]},
    ]
    text = netlist_to_spice(netlist)
    assert re.search(r"(?m)^V1\s+VCC\s+0\s+DC\s+9", text)
    assert re.search(r"(?m)^R1\s+vcc\s+a_b\s+100", text)
