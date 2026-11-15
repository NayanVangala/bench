"""SPICE netlist generation and ngspice execution for Netproof.

Converts an intermediate-representation netlist into a SPICE deck, runs it
with ngspice in batch mode, and parses the operating-point results back
into plain Python dicts. No PySpice, no LLM calls, no hardcoded demo
results. Component currents / power are derived in ``rules.py``, not here
(apart from the small pure helpers at the bottom).

IR assumptions (``ir.py`` landed while this file was being written; the
interface below was verified against it, and the tolerant spellings are
kept so dict-shaped netlists still work):

* ``netlist`` is an ``ir.Netlist`` (``.components`` of ``ir.Component``:
  ``id``, ``type`` as ``ir.ComponentType``, ``value`` float in SI units,
  ``nodes`` ``[n1, n2]``, ``color`` for LEDs), or a list of components,
  a dict with a ``"components"`` (or ``"netlist"``) key, or an object
  with a ``.components`` attribute.
* Each component is a dict (or an object with attributes of the same
  names) with:
    - ``ref`` / ``name`` / ``id``: designator, e.g. ``"R1"``, ``"V1"``.
      Missing refs are auto-generated (``R1``, ``R2``, ... per type).
    - ``kind`` / ``type``: ``"R"`` / ``"resistor"``, ``"C"`` /
      ``"capacitor"``, ``"D"`` / ``"diode"``, ``"LED"``, ``"V"`` /
      ``"voltage_source"`` / ``"voltage source"``, ``"I"`` /
      ``"current_source"`` / ``"current source"`` (case insensitive;
      a leading type letter on the ref is used as a fallback).
    - ``nodes``: a 2-item list/tuple of node names, or the pairs
      ``n1``/``n2``, ``plus``/``minus``, ``anode``/``cathode``, ``p``/``n``.
      Node ``"GND"`` / ``"gnd"`` / ``"ground"`` / ``0`` is ground node ``0``.
    - ``value``: ohms / farads / volts / amps as a number or a string
      such as ``"1k"``, ``"10u"``, ``"9"``. Required for R/C/V/I.
    - ``color`` (LEDs only): e.g. ``"red"``; see ``parts.py``. An LED
      with no color defaults to red.
    - ``model`` / ``model_line`` (optional): explicit SPICE model name /
      full ``.model`` line, used verbatim instead of the built-in ones.
* LEDs/diodes use the model name on the component line
  (``D1 anode cathode DRED``) and a ``.model`` line from ``parts.py``
  (``diode_model_line`` / ``model_name``). ``parts.py`` has no generic
  (non-LED) diode model, so plain diodes share a local ``DGEN`` model
  defined here. If ``parts.py`` cannot be imported, equivalent fallback
  model lines are used so this module still works standalone.
* Values are emitted in SI units with SPICE engineering suffixes
  (``1k``, ``1m``, ``1u``; ``Meg`` for mega, since bare ``M`` is milli
  in SPICE).

The deck is: title line, ``.model`` lines, component lines, ``.op``,
a ``.control`` block (``op`` + ``print all``) so batch-mode ngspice
actually prints the operating point, and ``.end``.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

try:  # normal package import: backend/netproof/spice.py
    from .parts import diode_model_line as _parts_diode_model_line
    from .parts import model_name as _parts_model_name
except ImportError:  # pragma: no cover - standalone / flat-layout fallback
    try:
        from parts import diode_model_line as _parts_diode_model_line  # type: ignore
        from parts import model_name as _parts_model_name  # type: ignore
    except ImportError:

        def _parts_model_name(color: str) -> str:
            return "D" + str(color).strip().upper()

        def _parts_diode_model_line(color: str) -> str:
            return (
                f".model {_parts_model_name(color)} "
                "D(IS=1e-14 RS=1 N=1.5 BV=5 IBV=1e-3)"
            )


__all__ = [
    "SimResult",
    "netlist_to_spice",
    "parse_op_output",
    "run_ngspice",
    "simulate",
    "current_through_resistor",
    "resistor_power",
]

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SimResult:
    """Outcome of one ngspice run.

    ``node_voltages`` maps node name -> volts (ground node ``"0"`` is
    omitted: it is 0 by definition). ``branch_currents`` maps voltage /
    current-source ref (e.g. ``"V1"``) -> amps, using ngspice's sign
    convention: positive means current flows into the source's ``+``
    terminal (through the source from ``+`` to ``-``), so a source
    delivering power to the circuit typically reports a negative current.
    ``success`` is False when ngspice failed, errored, or produced no
    operating-point data; ``raw_output`` always holds the full text.
    """

    node_voltages: dict = field(default_factory=dict)
    branch_currents: dict = field(default_factory=dict)
    raw_output: str = ""
    success: bool = False


# ---------------------------------------------------------------------------
# Value handling (SI in, SPICE engineering notation out)
# ---------------------------------------------------------------------------

_SUFFIX_SCALE = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "meg": 1e6,
    "g": 1e9,
    "t": 1e12,
}

# SPICE suffix for a given decade exponent (note: "M" is milli in SPICE,
# so mega must be written "Meg").
_ENGINEERING_SUFFIX = {
    -12: "p",
    -9: "n",
    -6: "u",
    -3: "m",
    0: "",
    3: "k",
    6: "Meg",
    9: "G",
    12: "T",
}

_UNIT_TAILS = ("ohm", "ohms", "Ω", "f", "v", "a", "h", "hz")


def _parse_value(value) -> float:
    """Parse a component value (number, or string like "1k" / "10uF")."""
    if isinstance(value, bool):
        raise TypeError(f"component value must be a number or string, got {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("component value string is empty")
        match = re.match(
            r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*)$", text
        )
        if not match:
            raise ValueError(f"cannot parse component value: {value!r}")
        number = float(match.group(1))
        suffix = match.group(2).strip().lower().rstrip("Ω".lower())
        if suffix in ("",):
            return number
        # "meg" prefix first (mega), then a single scale letter, then an
        # optional unit tail ("f", "v", "a", "ohm", ...).
        rest = suffix
        if rest.startswith("meg"):
            scale, rest = 1e6, rest[3:]
        elif rest[:1] in _SUFFIX_SCALE:
            # Bare "M" is milli in SPICE; only "meg" means mega.
            scale, rest = _SUFFIX_SCALE[rest[:1]], rest[1:]
        else:
            raise ValueError(f"unknown value suffix in {value!r}")
        rest = rest.strip()
        if rest and rest not in _UNIT_TAILS and not rest.startswith("ohm"):
            raise ValueError(f"unknown value suffix in {value!r}")
        return number * scale
    raise TypeError(
        f"component value must be a number or string, got {type(value).__name__}"
    )


def _format_spice_value(value: float) -> str:
    """Format a float as a SPICE value, e.g. 1000 -> "1k", 1e-6 -> "1u"."""
    if value == 0:
        return "0"
    if not math.isfinite(value):
        raise ValueError(f"component value must be finite, got {value!r}")
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    exponent = int(math.floor(math.log10(magnitude)) // 3 * 3)
    exponent = max(-12, min(12, exponent))
    mantissa = magnitude / (10.0 ** exponent)
    if mantissa >= 999.5 and exponent < 12:  # rounding pushed past 999
        mantissa /= 1000.0
        exponent += 3
    text = f"{mantissa:.6g}"
    return f"{sign}{text}{_ENGINEERING_SUFFIX[exponent]}"


# ---------------------------------------------------------------------------
# Netlist normalisation
# ---------------------------------------------------------------------------

_KIND_ALIASES = {
    "r": "R",
    "resistor": "R",
    "c": "C",
    "capacitor": "C",
    "cap": "C",
    "d": "D",
    "diode": "D",
    "led": "LED",
    "light-emitting diode": "LED",
    "light emitting diode": "LED",
    "v": "V",
    "voltage": "V",
    "voltage source": "V",
    "voltage_source": "V",
    "voltage-source": "V",
    "vsource": "V",
    "dc": "V",
    "i": "I",
    "current": "I",
    "current source": "I",
    "current_source": "I",
    "current-source": "I",
    "isource": "I",
}

_GROUND_NAMES = {"0", "gnd", "ground", "gnd!"}


def _norm_node(node) -> str:
    """Normalise one node name; ground becomes "0"."""
    text = str(node).strip()
    if text.lower() in _GROUND_NAMES:
        return "0"
    # SPICE node names: letters, digits, underscore. Anything else
    # becomes "_" so a stray label can never break the deck.
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", text)
    if not cleaned:
        raise ValueError(f"empty node name in netlist (from {node!r})")
    return cleaned


def _get(component, *names, default=None):
    """Read a field from a dict-like or attribute-style component."""
    if isinstance(component, dict):
        for name in names:
            if name in component and component[name] is not None:
                return component[name]
        return default
    for name in names:
        if hasattr(component, name):
            value = getattr(component, name)
            if value is not None:
                return value
    return default


def _norm_kind(component) -> str:
    kind = _get(component, "kind", "type", "category")
    if kind is not None:
        # StrEnum (ir.ComponentType) str()s to its value, but read .value
        # explicitly so plain enums work too.
        key = str(getattr(kind, "value", kind)).strip().lower()
        if key in _KIND_ALIASES:
            return _KIND_ALIASES[key]
        raise ValueError(f"unknown component kind: {kind!r}")
    ref = _get(component, "ref", "name", "id", "designator", default="")
    if ref and str(ref)[:1].upper() in ("R", "C", "D", "V", "I"):
        return str(ref)[:1].upper()
    raise ValueError(
        f"component has no kind/type and its ref gives none: {component!r}"
    )


def _norm_nodes(component) -> tuple:
    nodes = _get(component, "nodes", "node", "terminals", "pins", "connections")
    if nodes is None:
        n1 = _get(component, "n1", "plus", "anode", "p", "node1", "a")
        n2 = _get(component, "n2", "minus", "cathode", "n", "node2", "k")
        if n1 is None or n2 is None:
            raise ValueError(f"component has no nodes: {component!r}")
        nodes = (n1, n2)
    nodes = tuple(nodes)
    if len(nodes) != 2:
        raise ValueError(
            f"component must have exactly 2 nodes, got {len(nodes)}: {component!r}"
        )
    return (_norm_node(nodes[0]), _norm_node(nodes[1]))


def _components_of(netlist) -> list:
    """Extract the component list from any accepted netlist shape."""
    if isinstance(netlist, (list, tuple)):
        return list(netlist)
    if isinstance(netlist, dict):
        for key in ("components", "netlist", "parts", "elements"):
            if key in netlist and netlist[key] is not None:
                return list(netlist[key])
        raise ValueError(
            "netlist dict has no 'components' key; "
            f"keys present: {sorted(netlist)}"
        )
    components = getattr(netlist, "components", None)
    if components is not None:
        return list(components)
    raise TypeError(
        "netlist must be a list of components, a dict with a "
        "'components' key, or an object with a .components attribute; "
        f"got {type(netlist).__name__}"
    )


# Plain (non-LED) diodes: parts.py has no generic diode model, so share
# one local model. 1N4148-style: fast silicon switching diode.
_GENERIC_DIODE_MODEL = "DGEN"
_GENERIC_DIODE_MODEL_LINE = (
    ".model DGEN D(IS=1e-14 N=1.0 RS=0.1 BV=100 IBV=1e-3)"
)


def netlist_to_spice(netlist) -> str:
    """Convert an IR netlist to a SPICE deck string.

    Output: title line, ``.model`` lines for LEDs/diodes, one component
    line per component (R/C/D/V/I, SI values), ``.op``, a ``.control``
    block so batch ngspice prints the operating point, and ``.end``.
    Ground is node ``0``. Example voltage source line: ``V1 n1 0 DC 9``.
    """
    components = _components_of(netlist)
    if not components:
        raise ValueError("netlist has no components")

    model_lines: list = []
    seen_models: set = set()
    element_lines: list = []
    counters: dict = {}

    def _emit_model(line: str) -> None:
        if line not in seen_models:
            seen_models.add(line)
            model_lines.append(line)

    for component in components:
        kind = _norm_kind(component)
        n1, n2 = _norm_nodes(component)
        ref = _get(component, "ref", "name", "id", "designator")
        letter = "D" if kind in ("D", "LED") else kind
        if ref is None or str(ref).strip() == "":
            counters[letter] = counters.get(letter, 0) + 1
            ref = f"{letter}{counters[letter]}"
        ref = str(ref).strip()
        if ref[:1].upper() != letter:
            # Keep the given designator recognisable but make the first
            # letter the SPICE element letter (e.g. "load1" -> "Rload1").
            ref = f"{letter}{ref}"

        if kind == "R":
            value = _get(component, "value", "val", "resistance")
            if value is None:
                raise ValueError(f"resistor {ref} has no value")
            element_lines.append(
                f"{ref} {n1} {n2} {_format_spice_value(_parse_value(value))}"
            )
        elif kind == "C":
            value = _get(component, "value", "val", "capacitance")
            if value is None:
                raise ValueError(f"capacitor {ref} has no value")
            element_lines.append(
                f"{ref} {n1} {n2} {_format_spice_value(_parse_value(value))}"
            )
        elif kind in ("D", "LED"):
            model = _get(component, "model", "model_name")
            model_line = _get(component, "model_line")
            if model_line:
                _emit_model(str(model_line).strip())
                model = model or str(model_line).split()[1]
            elif model:
                model = str(model).strip()
            elif kind == "LED":
                color = _get(component, "color", "colour") or "red"
                model = _parts_model_name(str(color))
                _emit_model(_parts_diode_model_line(str(color)))
            else:
                model = _GENERIC_DIODE_MODEL
                _emit_model(_GENERIC_DIODE_MODEL_LINE)
            element_lines.append(f"{ref} {n1} {n2} {model}")
        elif kind == "V":
            value = _get(component, "value", "val", "voltage", "dc")
            if value is None:
                raise ValueError(f"voltage source {ref} has no value")
            element_lines.append(
                f"{ref} {n1} {n2} DC "
                f"{_format_spice_value(_parse_value(value))}"
            )
        elif kind == "I":
            value = _get(component, "value", "val", "current", "dc")
            if value is None:
                raise ValueError(f"current source {ref} has no value")
            element_lines.append(
                f"{ref} {n1} {n2} DC "
                f"{_format_spice_value(_parse_value(value))}"
            )
        else:  # pragma: no cover - _norm_kind already rejects these
            raise ValueError(f"unsupported component kind: {kind!r}")

    lines = []
    title = _get(netlist, "title")
    if isinstance(title, str) and title.strip():
        # A "*" title must stay one line; drop any newline a caller smuggled in.
        lines.append("* " + " ".join(title.split()))
    else:
        lines.append("* Netproof")
    lines.extend(model_lines)
    lines.extend(element_lines)
    lines.append(".op")
    lines.append(".control")
    lines.append("op")
    lines.append("print all")
    lines.append(".endc")
    lines.append(".end")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Operating-point output parsing
# ---------------------------------------------------------------------------

_NUMBER = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"

# ngspice "print all" / .op forms (all matched case-insensitively, keys
# lowercased so "V(VCC)" and "v(vcc)" land on the same "vcc" key):
#   v(n1) = 9.000000e+00        v(vcc) 9.0        v(vcc): 9.0
#   i(v1) = -9.000000e-03        i(v1) -0.015
#   v1#branch = -9.000000e-03    v1 #branch -0.015
#   vcc = 9.0                    (bare "name = value" assignment)
# The separator between a name and its value is optional: real ngspice
# output uses "=", compact/sample output sometimes omits it entirely.
# Tabular rows ("vcc   9.000000e+00") are accepted anywhere, not only
# after an "Operating point" header: real ngspice .op tables head their
# columns "Node ... Voltage" (wide spacing, no literal "node voltage"
# substring), and some sample outputs carry no header at all.
_NUMBER = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
_SEP = r"\s*[:=]?\s*"
_VOLT_RE = re.compile(rf"^\s*v\(\s*([^)\s]+)\s*\){_SEP}{_NUMBER}", re.IGNORECASE)
_CURR_PAREN_RE = re.compile(
    rf"^\s*i\(\s*([^)\s]+)\s*\){_SEP}{_NUMBER}", re.IGNORECASE
)
_BRANCH_RE = re.compile(
    rf"^\s*([A-Za-z][A-Za-z0-9_]*)\s*#\s*branch{_SEP}{_NUMBER}",
    re.IGNORECASE,
)
# Bare assignment: "vcc = 9.0" / "led: 1.9" (optional trailing unit).
_BARE_RE = re.compile(
    rf"^\s*([A-Za-z][A-Za-z0-9_.\-]*)\s*[:=]\s*{_NUMBER}"
    r"(?:\s+[A-Za-z%]+)?\s*$",
    re.IGNORECASE,
)
# Tabular fallback: "vcc   9.000000e+00" (optional trailing unit).
_TABULAR_RE = re.compile(
    rf"^\s*([A-Za-z][A-Za-z0-9_.\-]*)\s+{_NUMBER}"
    r"(?:\s+[A-Za-z%]+)?\s*$"
)
_OP_SECTION_RE = re.compile(
    r"operating[ -]?point|node\s+voltage|dc\s+operating", re.IGNORECASE
)

# Words that are column headers / labels, never node names.
_HEADER_WORDS = frozenset(
    {"node", "voltage", "current", "branch", "name", "value", "circuit"}
)


def _is_ground(name: str) -> bool:
    """True for ground in any spelling (``"0"``, ``"gnd"``, ...)."""
    return name == "0" or name.lower() in _GROUND_NAMES


def parse_op_output(text):
    """Parse ngspice operating-point output.

    Returns ``(node_voltages, branch_currents)``: node name -> volts and
    source ref -> amps (ngspice sign: positive into the source's ``+``
    terminal). Keys are lowercased; ground (``"0"`` / ``"gnd"`` / ...) is
    omitted. Handles ``v(n1) = ...``, ``i(v1) = ...``,
    ``v1#branch = ...``, bare ``vcc = 9.0`` assignments, and tabular
    ``n1  9.0`` rows, in spaced or compact form, with ``=``, ``:``, or
    no separator at all. Unparseable / empty text yields ``({}, {})``.
    """
    node_voltages: dict = {}
    branch_currents: dict = {}
    if not text:
        return node_voltages, branch_currents

    in_op_section = False
    for line in str(text).splitlines():
        if _OP_SECTION_RE.search(line):
            in_op_section = True

        match = _VOLT_RE.match(line)
        if match:
            node = match.group(1).strip().lower()
            if not _is_ground(node):
                node_voltages[node] = float(match.group(2))
            continue

        match = _CURR_PAREN_RE.match(line)
        if match:
            branch_currents[match.group(1).strip().lower()] = float(
                match.group(2)
            )
            continue

        match = _BRANCH_RE.match(line)
        if match:
            branch_currents[match.group(1).strip().lower()] = float(
                match.group(2)
            )
            continue

        match = _BARE_RE.match(line)
        if match:
            name = match.group(1).strip().lower()
            if name not in _HEADER_WORDS and not _is_ground(name):
                node_voltages[name] = float(match.group(2))
            continue

        # Tabular "node value" rows. Accepted anywhere, not only inside
        # an operating-point section (see the regex comment above); the
        # two-token shape plus the header-word / ground exclusions keep
        # stray prose lines such as "nothing useful here" out.
        match = _TABULAR_RE.match(line)
        if match:
            name = match.group(1).strip().lower()
            if name not in _HEADER_WORDS and not _is_ground(name):
                node_voltages[name] = float(match.group(2))

    return node_voltages, branch_currents


# ---------------------------------------------------------------------------
# ngspice execution
# ---------------------------------------------------------------------------

_ERROR_MARKERS = ("error on line", "singular matrix", "no convergence")


def run_ngspice(spice_text: str) -> SimResult:
    """Run a SPICE deck with ngspice in batch mode (30 s timeout).

    Raises ``RuntimeError`` with a clear message if the ngspice binary is
    not on PATH or the run times out. A failed / erroring simulation does
    not raise: it returns a ``SimResult`` with ``success=False`` and the
    full ngspice output in ``raw_output``.
    """
    if not isinstance(spice_text, str) or not spice_text.strip():
        raise ValueError("spice_text must be a non-empty string")

    ngspice = shutil.which("ngspice")
    if ngspice is None:
        raise RuntimeError(
            "ngspice binary not found on PATH; install ngspice to run "
            "simulations (macOS: `brew install ngspice`; "
            "Debian/Ubuntu: `apt install ngspice`), then retry."
        )

    path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cir", prefix="netproof-", delete=False
        ) as handle:
            handle.write(spice_text)
            path = handle.name
        try:
            completed = subprocess.run(
                [ngspice, "-b", path],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "ngspice timed out after 30 s; the circuit may be "
                "degenerate (e.g. a floating node or a zero-resistance "
                "loop) or ngspice may be waiting on input."
            )
        raw_output = completed.stdout or ""
        if completed.stderr:
            raw_output = (raw_output + "\n" + completed.stderr).strip() + "\n"
        node_voltages, branch_currents = parse_op_output(raw_output)
        lowered = raw_output.lower()
        failed = completed.returncode != 0 or any(
            marker in lowered for marker in _ERROR_MARKERS
        )
        success = not failed and bool(node_voltages or branch_currents)
        return SimResult(
            node_voltages=node_voltages,
            branch_currents=branch_currents,
            raw_output=raw_output,
            success=success,
        )
    finally:
        if path is not None:
            try:
                import os

                os.unlink(path)
            except OSError:
                pass


def simulate(netlist) -> SimResult:
    """Simulate a netlist end to end: render the SPICE deck with
    :func:`netlist_to_spice` and run it with :func:`run_ngspice`.

    This is the one-call real-simulation path used by
    ``pipeline._simulate`` (and by ``test_pipeline.py``'s real-ngspice
    test, which passes it in as the injected simulator).
    """
    return run_ngspice(netlist_to_spice(netlist))


# ---------------------------------------------------------------------------
# Small pure helpers for rules.py (no simulation here)
# ---------------------------------------------------------------------------


def current_through_resistor(v_plus: float, v_minus: float, resistance: float) -> float:
    """Ohm's law: current (A) flowing from the ``+`` node to the ``-`` node.

    ``v_plus`` / ``v_minus`` are the node voltages on either side of the
    resistor and ``resistance`` is in ohms. Raises ``ValueError`` for a
    zero / negative resistance.
    """
    if resistance <= 0:
        raise ValueError(f"resistance must be positive, got {resistance!r}")
    return (float(v_plus) - float(v_minus)) / float(resistance)


def resistor_power(v_plus: float, v_minus: float, resistance: float) -> float:
    """Power (W) dissipated in a resistor from the voltage across it."""
    if resistance <= 0:
        raise ValueError(f"resistance must be positive, got {resistance!r}")
    drop = float(v_plus) - float(v_minus)
    return drop * drop / float(resistance)
