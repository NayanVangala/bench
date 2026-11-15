"""Rule tests for Netproof.

One test per rule in the rules engine, plus a good-circuit PASS test
(12 tests total). Aligned to the real ``backend/netproof/rules.py``
(which imports ``Finding`` / ``Report`` / ``Severity`` / ``Verdict``
from ``netproof.ir`` and the limits from ``netproof.parts``) and to
the fixture JSONs in ``backend/tests/fixtures/`` (loaded by
``conftest.py`` as ``ir.Netlist`` objects).

Real API used here (no import guard, no skips: ``rules.py`` exists
and imports cleanly):

* ``from netproof.rules import check_rules, verify``
* ``from netproof.spice import SimResult`` -- faked operating points
  (``node_voltages`` / ``branch_currents``, ``success=True``); no
  ngspice is called. Branch-current sign follows ngspice: a voltage
  source delivering power reports a *negative* current. Rules use
  magnitudes for overcurrent checks.
* ``check_rules(netlist, sim=None) -> list[ir.Finding]`` and
  ``verify(netlist, sim=None) -> ir.Report`` (findings sorted
  STOP > WARN > INFO; verdict STOP if any STOP, else WARN if any
  WARN, else PASS -- INFO alone still gives PASS).
* ``ir.Finding`` fields: ``rule_id`` / ``severity`` (a
  ``Severity`` enum) / ``component_ids`` / single-line ``message``
  stating the measured number and the limit.

Real severities / thresholds (``rules.py`` + ``parts.py``):

* NO_GROUND STOP, FLOATING_NODE STOP (node with one connection),
  SHORTED_SOURCE STOP (source, both terminals on one node),
  SHORTED_COMPONENT WARN (non-source, both terminals on one node),
  NO_SOURCE INFO.
* LED_OVERCURRENT: WARN at >= 20 mA, STOP at > 30 mA.
* LED_UNDERCURRENT: INFO below 1 mA (not WARN).
* REVERSED_LED: INFO when anode voltage < cathode voltage (not STOP).
* RESISTOR_POWER: WARN when P > 70% of rating, STOP when P > 100%
  of rating (not the old assumed <2x / >=2x split). Rating defaults
  to 0.25 W; every fixture here states 0.25 W or omits it.
* SOURCE_OVERCURRENT: WARN when a source supplies > 1 A.

Co-firing findings are asserted where the fixture genuinely trips
two rules (see the fixture README): a test asserts its rule is
present with the right severity, and only the good-circuit test
asserts zero findings. Pre-sim-only tests (ground / floating /
shorts / no source) pass no sim result: a shorted source must not
be simulated, and those rules need no operating point.
"""

from __future__ import annotations

from netproof.ir import (
    Component,
    ComponentType,
    Netlist,
    Report,
    Severity,
    Verdict,
)
from netproof.rules import check_rules, verify
from netproof.spice import SimResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sim(node_voltages, branch_currents) -> SimResult:
    """Fake a successful ngspice operating point as a real SimResult."""
    return SimResult(
        node_voltages=dict(node_voltages),
        branch_currents=dict(branch_currents),
        success=True,
    )


def _for_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def _assert_finding(findings, rule_id, severity, component_id=None):
    """Assert a rule fired with the real severity (and component)."""
    hits = _for_rule(findings, rule_id)
    assert hits, f"expected {rule_id} to fire, got {[f.rule_id for f in findings]}"
    assert any(f.severity is severity for f in hits), (
        f"expected {rule_id} severity {severity}, "
        f"got {[(f.severity, f.component_ids) for f in hits]}"
    )
    if component_id is not None:
        assert any(component_id in f.component_ids for f in hits), (
            f"expected {component_id} in {rule_id} component_ids, "
            f"got {[f.component_ids for f in hits]}"
        )
    assert all(f.message.strip() and "\n" not in f.message for f in hits)


# ---------------------------------------------------------------------------
# Structural rules (pre-sim only, no SimResult needed)
# ---------------------------------------------------------------------------


def test_no_ground(no_ground):
    """NO_GROUND STOP: no component touches node "0"."""
    assert not no_ground.has_ground()
    findings = check_rules(no_ground)
    _assert_finding(findings, "NO_GROUND", Severity.STOP)
    assert verify(no_ground).verdict is Verdict.STOP


def test_floating_node(floating_node):
    """FLOATING_NODE STOP: capacitor leg ends at node "float1"."""
    findings = check_rules(floating_node)
    _assert_finding(findings, "FLOATING_NODE", Severity.STOP)
    assert any("float1" in f.message for f in _for_rule(findings, "FLOATING_NODE"))
    assert verify(floating_node).verdict is Verdict.STOP


def test_no_source(no_source):
    """NO_SOURCE INFO: resistor + LED to ground, no source at all.

    INFO alone does not fail the circuit, so the verdict is PASS.
    """
    findings = check_rules(no_source)
    _assert_finding(findings, "NO_SOURCE", Severity.INFO)
    report = verify(no_source)
    assert report.verdict is Verdict.PASS
    assert report.findings == findings


def test_shorted_source(shorted_source):
    """SHORTED_SOURCE STOP: V1 has both terminals on node "0".

    Pre-sim only: an ideal 9 V source shorted across itself has
    undefined current, so it must not be simulated.
    """
    findings = check_rules(shorted_source)
    _assert_finding(findings, "SHORTED_SOURCE", Severity.STOP, "V1")
    assert verify(shorted_source).verdict is Verdict.STOP


def test_shorted_component(shorted_component):
    """SHORTED_COMPONENT WARN: R1 has both ends on node "vcc".

    WARN, not STOP. The fixture also carries R2 (vcc to ground), so
    ground has two connections and no FLOATING_NODE fires: the
    verdict is WARN, not STOP.
    """
    findings = check_rules(shorted_component)
    _assert_finding(findings, "SHORTED_COMPONENT", Severity.WARN, "R1")
    assert not _for_rule(findings, "FLOATING_NODE")
    assert verify(shorted_component).verdict is Verdict.WARN


# ---------------------------------------------------------------------------
# LED current rules (faked SimResult, no ngspice)
# ---------------------------------------------------------------------------


def test_led_overcurrent(led_overcurrent):
    """LED_OVERCURRENT STOP: 70 mA through a red LED (limit 30 mA).

    9 V source, 100 ohm series resistor, red LED Vf 2.0 V:
    (9 - 2) / 100 = 0.070 A. Resistor power 7^2 / 100 = 0.49 W is
    also over its 0.25 W rating, so RESISTOR_POWER STOP co-fires.
    """
    sim = _sim(
        node_voltages={"vcc": 9.0, "led": 2.0, "0": 0.0},
        branch_currents={"V1": -0.07, "R1": 0.07, "D1": 0.07},
    )
    report = verify(led_overcurrent, sim)
    _assert_finding(report.findings, "LED_OVERCURRENT", Severity.STOP, "D1")
    _assert_finding(report.findings, "RESISTOR_POWER", Severity.STOP, "R1")
    assert not _for_rule(report.findings, "LED_UNDERCURRENT")
    assert report.verdict is Verdict.STOP


def test_led_overcurrent_exactly_30ma_is_warn_not_stop(good_led):
    """LED_OVERCURRENT boundary: exactly 30.0 mA is WARN, not STOP.

    The STOP check is strict ``i > 0.030`` and WARN is ``i >= 0.020``,
    so 0.030 A lands in the [20 mA, 30 mA] WARN band. Resistor power
    7^2 / 470 = 0.104 W stays under the 70% warn level, so the only
    finding is LED_OVERCURRENT WARN and the verdict is WARN.
    """
    sim = _sim(
        node_voltages={"vcc": 9.0, "led": 2.0, "0": 0.0},
        branch_currents={"V1": -0.030, "R1": 0.030, "D1": 0.030},
    )
    report = verify(good_led, sim)
    _assert_finding(report.findings, "LED_OVERCURRENT", Severity.WARN, "D1")
    assert not any(
        f.severity is Severity.STOP
        for f in _for_rule(report.findings, "LED_OVERCURRENT")
    )
    assert report.verdict is Verdict.WARN


def test_led_undercurrent(led_undercurrent):
    """LED_UNDERCURRENT INFO: 70 uA through a red LED (min 1 mA).

    9 V source, 100 k series resistor, red LED Vf 2.0 V:
    (9 - 2) / 100_000 = 0.00007 A -- far too dim to light usefully.
    INFO, not WARN, so the verdict stays PASS.
    """
    sim = _sim(
        node_voltages={"vcc": 9.0, "led": 2.0, "0": 0.0},
        branch_currents={"V1": -0.00007, "R1": 0.00007, "D1": 0.00007},
    )
    report = verify(led_undercurrent, sim)
    _assert_finding(report.findings, "LED_UNDERCURRENT", Severity.INFO, "D1")
    assert not _for_rule(report.findings, "LED_OVERCURRENT")
    assert report.verdict is Verdict.PASS


def test_reversed_led(reversed_led):
    """REVERSED_LED INFO: LED anode to ground, cathode to the LED node.

    Anode 0 V < cathode ~9 V (no drop across R1: the LED does not
    conduct, so the LED node sits near 9 V) and the LED branch
    current is 0 A. REVERSED_LED is INFO, not STOP, and the 0 A
    current also trips LED_UNDERCURRENT INFO; verdict stays PASS.
    """
    sim = _sim(
        node_voltages={"vcc": 9.0, "led": 9.0, "0": 0.0},
        branch_currents={"V1": 0.0, "R1": 0.0, "D1": 0.0},
    )
    report = verify(reversed_led, sim)
    _assert_finding(report.findings, "REVERSED_LED", Severity.INFO, "D1")
    _assert_finding(report.findings, "LED_UNDERCURRENT", Severity.INFO, "D1")
    assert report.verdict is Verdict.PASS


# ---------------------------------------------------------------------------
# Power rules
# ---------------------------------------------------------------------------


def test_resistor_power_warn(resistor_warn):
    """RESISTOR_POWER WARN: 0.181 W in a 0.25 W resistor (72.6%).

    9 V source, 270 ohm series resistor, red LED Vf 2.0 V:
    I = (9 - 2) / 270 = 0.0259 A, P = 7^2 / 270 = 0.181 W -- over
    70% of the 0.25 W rating but under 100%, so WARN, not STOP.
    The 25.9 mA LED current is in the 20-30 mA band, so
    LED_OVERCURRENT WARN co-fires; with no STOP the verdict is WARN.
    """
    current = 7.0 / 270.0
    sim = _sim(
        node_voltages={"vcc": 9.0, "led": 2.0, "0": 0.0},
        branch_currents={"V1": -current, "R1": current, "D1": current},
    )
    report = verify(resistor_warn, sim)
    _assert_finding(report.findings, "RESISTOR_POWER", Severity.WARN, "R1")
    _assert_finding(report.findings, "LED_OVERCURRENT", Severity.WARN, "D1")
    assert not any(
        f.severity is Severity.STOP
        for f in _for_rule(report.findings, "RESISTOR_POWER")
    )
    assert report.verdict is Verdict.WARN


def test_resistor_power_stop():
    """RESISTOR_POWER STOP: 1.0 W in a 0.25 W resistor (4x rating).

    No fixture covers the STOP side in isolation (the fixtures that
    trip it also trip another rule), so this circuit is built inline:
    10 V across 100 ohms gives I = 0.1 A, P = 1.0 W -- over 100%
    of the 0.25 W default rating. Source current 0.1 A is under the
    1 A limit, so RESISTOR_POWER STOP is the only finding.
    """
    netlist = Netlist(components=[
        Component(
            id="V1", type=ComponentType.VOLTAGE_SOURCE, value=10.0,
            nodes=["vcc", "0"],
        ),
        Component(
            id="R1", type=ComponentType.RESISTOR, value=100.0,
            nodes=["vcc", "0"], rating_watts=0.25,
        ),
    ])
    sim = _sim(
        node_voltages={"vcc": 10.0, "0": 0.0},
        branch_currents={"V1": -0.1, "R1": 0.1},
    )
    report = verify(netlist, sim)
    _assert_finding(report.findings, "RESISTOR_POWER", Severity.STOP, "R1")
    assert report.findings == _for_rule(report.findings, "RESISTOR_POWER")
    assert report.verdict is Verdict.STOP


def test_source_overcurrent(source_overcurrent):
    """SOURCE_OVERCURRENT WARN: 9 A out of a 9 V source (limit 1 A).

    9 V source across a 1 ohm resistor: I = 9 A. WARN, not STOP.
    Resistor power 9^2 / 1 = 81 W is far over its 0.25 W rating,
    so RESISTOR_POWER STOP co-fires and sets the verdict to STOP.
    """
    sim = _sim(
        node_voltages={"vcc": 9.0, "0": 0.0},
        branch_currents={"V1": -9.0, "R1": 9.0},
    )
    report = verify(source_overcurrent, sim)
    _assert_finding(report.findings, "SOURCE_OVERCURRENT", Severity.WARN, "V1")
    _assert_finding(report.findings, "RESISTOR_POWER", Severity.STOP, "R1")
    assert report.verdict is Verdict.STOP


# ---------------------------------------------------------------------------
# Good circuit
# ---------------------------------------------------------------------------


def test_good_circuit_passes(good_led):
    """Good circuit: PASS with zero findings.

    9 V source, 470 ohm series resistor, red LED Vf 2.0 V:
    I = (9 - 2) / 470 = 0.0149 A -- above the 1 mA LED minimum, below
    the 20 mA LED warn level. Resistor power 7^2 / 470 = 0.104 W is
    42% of its 0.25 W rating (under the 70% warn level). Source
    supplies 14.9 mA, under the 1 A limit.
    """
    current = 7.0 / 470.0
    sim = _sim(
        node_voltages={"vcc": 9.0, "led": 2.0, "0": 0.0},
        branch_currents={"V1": -current, "R1": current, "D1": current},
    )
    report = verify(good_led, sim)
    assert isinstance(report, Report)
    assert report.verdict is Verdict.PASS
    assert report.findings == []
    assert check_rules(good_led, sim) == []
