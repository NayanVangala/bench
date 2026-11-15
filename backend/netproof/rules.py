"""Netproof rules engine (MVP).

DC operating point only; transient/AC is later.

Pure functions over ``(Netlist, SimResult | None) -> Report``.
No ngspice, no LLM, no I/O, no hardcoded demo results.

Aligned to the real IR (``ir.py``): this module imports
``Finding`` / ``Report`` / ``Component`` / ``Netlist`` from ``ir.py``
(and the limits from ``parts.py``) and defines no data types of its
own. Components are read through their real fields only:
``comp.id`` / ``comp.type`` / ``comp.value`` / ``comp.nodes`` /
``comp.rating_watts`` / ``comp.color``.

Conventions (from ``ir.py`` / ``spice.py``):
* Ground is node ``"0"`` (``"GND"``, case-insensitive, is accepted as
  the same reference node for voltage lookups).
* ``Component.nodes`` is ``[n1, n2]``: LED ``[anode, cathode]``,
  source ``[+, -]``.
* ``SimResult.node_voltages`` is ``{node: volts}`` and
  ``SimResult.branch_currents`` is ``{refdes: amps}``; a plain dict
  with the same keys is also accepted (the test fixtures use dicts).
  Branch-current sign follows ngspice: for a voltage source, positive
  means current flows into the ``+`` terminal, so a source delivering
  power typically reports a negative current. Magnitudes are used for
  overcurrent checks, so a sign flip cannot hide a violation.
* A resistor's power rating is ``comp.rating_watts`` (``ir.py``
  defaults it to 0.25 W); ``parts.resistor_rating_w`` is the single
  source of truth for the fallback.
* LED/diode current, when not present in ``branch_currents``, is
  derived by KCL at the LED's anode node: if every other component
  incident to that node has a known current (resistor via Ohm, source
  via branch current / set value), the LED current is whatever must
  leave the node through the LED. If more than one incident current
  is unknown, the LED current is underdetermined and no
  LED-current finding is emitted (recorded in ``Report.uncertain``).
"""

from __future__ import annotations

from typing import Any

try:  # normal package import: backend/netproof/rules.py
    from .ir import (
        SEVERITY_RANK,
        Component,
        ComponentType,
        Finding,
        Netlist,
        Report,
        Severity,
        Verdict,
    )
    from .parts import (
        RESISTOR_DEFAULT_WATTAGE,
        led_if_max,
        led_if_min,
        led_if_warn,
        resistor_rating_w,
    )
    from .spice import SimResult
except ImportError:  # pragma: no cover - standalone / flat-layout fallback
    from ir import (  # type: ignore
        SEVERITY_RANK,
        Component,
        ComponentType,
        Finding,
        Netlist,
        Report,
        Severity,
        Verdict,
    )
    from parts import (  # type: ignore
        RESISTOR_DEFAULT_WATTAGE,
        led_if_max,
        led_if_min,
        led_if_warn,
        resistor_rating_w,
    )
    from spice import SimResult  # type: ignore

__all__ = [
    "Finding",
    "Report",
    "LED_WARN_A",
    "LED_STOP_A",
    "LED_UNDER_A",
    "SOURCE_OVERCURRENT_A",
    "RESISTOR_WARN_FRACTION",
    "RESISTOR_STOP_FRACTION",
    "DEFAULT_RESISTOR_RATING_W",
    "led_current",
    "check_rules",
    "pre_sim_checks",
    "post_sim_checks",
    "verify",
]

# ---------------------------------------------------------------------------
# Thresholds (amps / watts / fractions of rating)
# ---------------------------------------------------------------------------

LED_WARN_A = 0.020           # 20 mA (parts.led_if_warn)
LED_STOP_A = 0.030           # 30 mA (parts.led_if_max)
LED_UNDER_A = 0.001          # 1 mA  (parts.led_if_min)
SOURCE_OVERCURRENT_A = 1.0   # 1 A
RESISTOR_WARN_FRACTION = 0.70
RESISTOR_STOP_FRACTION = 1.00
DEFAULT_RESISTOR_RATING_W = RESISTOR_DEFAULT_WATTAGE  # 0.25


# ---------------------------------------------------------------------------
# Input normalisation (ir.py types in, tolerant shapes accepted)
# ---------------------------------------------------------------------------

def _coerce_netlist(netlist: Netlist | dict | list | tuple) -> Netlist:
    """Accept a ``Netlist`` (the normal case), a netlist dict, or a bare
    component list, and return a ``Netlist``. Anything else raises
    ``TypeError``."""
    if isinstance(netlist, Netlist):
        return netlist
    if isinstance(netlist, dict):
        return Netlist.model_validate(netlist)
    if isinstance(netlist, (list, tuple)):
        return Netlist(
            components=[
                c if isinstance(c, Component) else Component.model_validate(c)
                for c in netlist
            ]
        )
    raise TypeError(
        "netlist must be a netproof.ir.Netlist, a netlist dict, or a "
        f"component list; got {type(netlist).__name__}"
    )


def _sim_maps(
    sim_result: SimResult | dict | Any | None,
) -> tuple[dict[str, float], dict[str, float], bool | None, str | None]:
    """Extract ``(node_voltages, branch_currents, success, error)`` from
    a ``spice.SimResult`` or a plain dict with the same keys (the test
    fixtures use dicts). ``None`` yields empty maps. Non-numeric values
    are dropped."""
    if sim_result is None:
        return {}, {}, None, None
    if isinstance(sim_result, dict):
        raw_voltages = (
            sim_result.get("node_voltages") or sim_result.get("voltages") or {}
        )
        raw_currents = (
            sim_result.get("branch_currents") or sim_result.get("currents") or {}
        )
        success = sim_result.get("success")
        error = sim_result.get("error")
    else:
        raw_voltages = getattr(sim_result, "node_voltages", None) or getattr(
            sim_result, "voltages", None
        ) or {}
        raw_currents = getattr(sim_result, "branch_currents", None) or getattr(
            sim_result, "currents", None
        ) or {}
        success = getattr(sim_result, "success", None)
        error = getattr(sim_result, "error", None)

    def _float_map(raw: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, bool):
                    continue
                try:
                    out[str(key)] = float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        return out

    return (
        _float_map(raw_voltages),
        _float_map(raw_currents),
        success if isinstance(success, bool) else None,
        str(error) if error is not None else None,
    )


def _is_ground(node: str) -> bool:
    return node == "0" or node.upper() == "GND"


def _is_resistor(c: Component) -> bool:
    return c.type is ComponentType.RESISTOR


def _is_led(c: Component) -> bool:
    return c.type is ComponentType.LED


def _is_voltage_source(c: Component) -> bool:
    return c.type is ComponentType.VOLTAGE_SOURCE


def _is_current_source(c: Component) -> bool:
    return c.type is ComponentType.CURRENT_SOURCE


def _is_source(c: Component) -> bool:
    return _is_voltage_source(c) or _is_current_source(c)


def _led_limits(c: Component) -> tuple[float, float, float]:
    """``(warn_A, max_A, min_A)`` for an LED from ``parts.py`` by colour,
    falling back to the module constants for an unknown colour."""
    color = c.color or "red"
    try:
        return led_if_warn(color), led_if_max(color), led_if_min(color)
    except (TypeError, ValueError):
        return LED_WARN_A, LED_STOP_A, LED_UNDER_A


def _branch_current_for(
    refdes: str, branch_currents: dict[str, float]
) -> float | None:
    """Look up a component's branch current under common key spellings."""
    if not refdes:
        return None
    candidates = (
        refdes,
        refdes.lower(),
        refdes.upper(),
        f"i({refdes})",
        f"I({refdes})",
        f"i({refdes.lower()})",
        f"{refdes}#branch",
        f"branch_{refdes}",
        f"branch_{refdes.lower()}",
    )
    for key in candidates:
        if key in branch_currents:
            return branch_currents[key]
    return None


def _voltage_at(node: str, node_voltages: dict[str, float]) -> float | None:
    if node in node_voltages:
        return node_voltages[node]
    # Ground aliases.
    if _is_ground(node):
        for alias in ("0", "GND", "gnd"):
            if alias in node_voltages:
                return node_voltages[alias]
        return 0.0
    return None


def _known_current(
    c: Component, node_voltages: dict[str, float], branch_currents: dict[str, float]
) -> float | None:
    """Current through ``c`` from its first node to its second, if it can
    be derived without knowing any LED/diode current. ``None`` otherwise."""
    if len(c.nodes) < 2:
        return None
    branch = _branch_current_for(c.id, branch_currents)
    if branch is not None:
        return branch
    if _is_resistor(c):
        v0 = _voltage_at(c.nodes[0], node_voltages)
        v1 = _voltage_at(c.nodes[1], node_voltages)
        if v0 is not None and v1 is not None:
            return (v0 - v1) / c.value
        return None
    if _is_current_source(c):
        return c.value
    return None


def led_current(
    led: Component,
    components: list[Component],
    node_voltages: dict[str, float],
    branch_currents: dict[str, float],
) -> float | None:
    """Public LED-current lookup (anode -> cathode): branch current
    first, else KCL at the LED's nodes. ``None`` if underdetermined.

    This is the same derivation ``verify`` uses for its findings, so
    a caller (e.g. the sweep endpoint) reading it off a ``Report``
    cannot disagree with the verdict. Real ngspice OP output never
    carries an LED branch current (only voltage sources get one), so
    the KCL path is the one that fires on real runs.
    """
    return _led_current(led, components, node_voltages, branch_currents)


def _led_current(
    led: Component,
    components: list[Component],
    node_voltages: dict[str, float],
    branch_currents: dict[str, float],
) -> float | None:
    """LED current (anode -> cathode). Branch current first, else KCL at
    its nodes. ``None`` if underdetermined."""
    branch = _branch_current_for(led.id, branch_currents)
    if branch is not None:
        return branch
    if len(led.nodes) < 2:
        return None
    # KCL at a node: current leaving the node through the LED equals
    # minus the sum of currents leaving the node through known components.
    for node, sign in ((led.nodes[0], 1.0), (led.nodes[1], -1.0)):
        # sign: +1 at anode (LED current leaves the anode node),
        # -1 at cathode (LED current enters the cathode node, so the
        # current leaving through the LED is -I_led).
        leaving_known = 0.0
        underdetermined = False
        for other in components:
            if other is led or len(other.nodes) < 2:
                continue
            if node not in other.nodes:
                continue
            i_other = _known_current(other, node_voltages, branch_currents)
            if i_other is None:
                underdetermined = True
                break
            # Current leaving `node` through `other`: +i if `node` is its
            # first node, -i if it is its second.
            leaving_known += i_other if other.nodes[0] == node else -i_other
        if underdetermined:
            continue
        # Sum of all currents leaving the node is zero.
        # At the anode: I_led leaves the node, so I_led = -leaving_known.
        # At the cathode: I_led enters the node (leaving = -I_led), so
        # -I_led + leaving_known = 0, i.e. I_led = leaving_known.
        return -leaving_known if sign == 1.0 else leaving_known
    return None


# ---------------------------------------------------------------------------
# Pre-simulation ERC
# ---------------------------------------------------------------------------

def pre_sim_checks(netlist: Netlist | dict | list | tuple) -> list[Finding]:
    """Static checks that need no simulation result."""
    netlist = _coerce_netlist(netlist)
    components = list(netlist.components)
    findings: list[Finding] = []

    all_nodes: list[str] = []
    for c in components:
        all_nodes.extend(c.nodes)

    # NO_GROUND STOP if "0" absent.
    if not netlist.has_ground() and not any(_is_ground(n) for n in all_nodes):
        findings.append(
            Finding(
                severity=Severity.STOP,
                rule_id="NO_GROUND",
                message=(
                    "No ground node '0' in the netlist; every circuit "
                    "needs a reference node."
                ),
            )
        )

    # FLOATING_NODE STOP: node with only one connection.
    counts: dict[str, int] = {}
    for n in all_nodes:
        counts[n] = counts.get(n, 0) + 1
    for node in sorted(counts):
        if counts[node] == 1:
            findings.append(
                Finding(
                    severity=Severity.STOP,
                    rule_id="FLOATING_NODE",
                    message=(
                        f"Node '{node}' has only 1 connection and is floating."
                    ),
                )
            )

    # SHORTED_SOURCE STOP / SHORTED_COMPONENT WARN: both nodes equal.
    for c in components:
        if len(c.nodes) >= 2 and c.nodes[0] == c.nodes[1]:
            if _is_source(c):
                findings.append(
                    Finding(
                        severity=Severity.STOP,
                        rule_id="SHORTED_SOURCE",
                        component_ids=[c.id],
                        message=(
                            f"Source {c.id} has its + and - terminals on the "
                            f"same node '{c.nodes[0]}'."
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        severity=Severity.WARN,
                        rule_id="SHORTED_COMPONENT",
                        component_ids=[c.id],
                        message=(
                            f"Component {c.id} has both terminals on node "
                            f"'{c.nodes[0]}' and is shorted out."
                        ),
                    )
                )

    # NO_SOURCE INFO if no voltage/current source.
    if not any(_is_source(c) for c in components):
        findings.append(
            Finding(
                severity=Severity.INFO,
                rule_id="NO_SOURCE",
                message=(
                    "No voltage or current source in the netlist; nothing "
                    "drives the circuit."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Post-simulation checks (only when a SimResult is present)
# ---------------------------------------------------------------------------

def post_sim_checks(
    netlist: Netlist | dict | list | tuple,
    sim_result: SimResult | dict | Any | None,
) -> list[Finding]:
    """Operating-point checks. Returns [] when ``sim_result`` is None."""
    if sim_result is None:
        return []
    netlist = _coerce_netlist(netlist)
    components = list(netlist.components)
    node_voltages, branch_currents, _, _ = _sim_maps(sim_result)
    findings: list[Finding] = []

    for c in components:
        if len(c.nodes) < 2:
            continue
        v0 = _voltage_at(c.nodes[0], node_voltages)
        v1 = _voltage_at(c.nodes[1], node_voltages)

        if _is_led(c):
            warn_a, stop_a, under_a = _led_limits(c)
            # REVERSED_LED INFO: anode voltage below cathode voltage.
            if v0 is not None and v1 is not None and v0 < v1 - 1e-9:
                findings.append(
                    Finding(
                        severity=Severity.INFO,
                        rule_id="REVERSED_LED",
                        component_ids=[c.id],
                        message=(
                            f"LED {c.id} is reverse-biased "
                            f"(anode {v0:.3f} V < cathode {v1:.3f} V)."
                        ),
                    )
                )
            # LED current: branch current, else KCL at its nodes.
            i_led = _led_current(c, components, node_voltages, branch_currents)
            if i_led is not None:
                i_mag = abs(i_led)
                if i_mag > stop_a:
                    findings.append(
                        Finding(
                            severity=Severity.STOP,
                            rule_id="LED_OVERCURRENT",
                            component_ids=[c.id],
                            message=(
                                f"LED {c.id} current {i_mag * 1e3:.1f} mA "
                                f"exceeds the {stop_a * 1e3:.0f} mA limit."
                            ),
                        )
                    )
                elif i_mag >= warn_a:
                    findings.append(
                        Finding(
                            severity=Severity.WARN,
                            rule_id="LED_OVERCURRENT",
                            component_ids=[c.id],
                            message=(
                                f"LED {c.id} current {i_mag * 1e3:.1f} mA "
                                f"is in the {warn_a * 1e3:.0f}-"
                                f"{stop_a * 1e3:.0f} mA warning range."
                            ),
                        )
                    )
                elif i_mag < under_a:
                    findings.append(
                        Finding(
                            severity=Severity.INFO,
                            rule_id="LED_UNDERCURRENT",
                            component_ids=[c.id],
                            message=(
                                f"LED {c.id} current {i_mag * 1e3:.3f} mA "
                                f"is below {under_a * 1e3:.0f} mA; the LED "
                                f"is likely off or starved."
                            ),
                        )
                    )

        if _is_resistor(c):
            # RESISTOR_POWER: P = V^2/R from node voltages, else I^2*R.
            power: float | None = None
            if v0 is not None and v1 is not None:
                power = (v0 - v1) ** 2 / c.value
            else:
                branch = _branch_current_for(c.id, branch_currents)
                if branch is not None:
                    power = branch**2 * c.value
            if power is not None:
                rating = resistor_rating_w(c)
                if power > RESISTOR_STOP_FRACTION * rating:
                    findings.append(
                        Finding(
                            severity=Severity.STOP,
                            rule_id="RESISTOR_POWER",
                            component_ids=[c.id],
                            message=(
                                f"Resistor {c.id} dissipates {power:.3f} W, "
                                f"over its {rating:g} W rating."
                            ),
                        )
                    )
                elif power > RESISTOR_WARN_FRACTION * rating:
                    findings.append(
                        Finding(
                            severity=Severity.WARN,
                            rule_id="RESISTOR_POWER",
                            component_ids=[c.id],
                            message=(
                                f"Resistor {c.id} dissipates {power:.3f} W, "
                                f"over 70% of its {rating:g} W rating."
                            ),
                        )
                    )

        if _is_source(c):
            # SOURCE_OVERCURRENT WARN: source supplies > 1 A.
            i_src: float | None = None
            if _is_voltage_source(c):
                i_src = _branch_current_for(c.id, branch_currents)
            elif _is_current_source(c):
                i_src = _branch_current_for(c.id, branch_currents)
                if i_src is None:
                    i_src = c.value
            if i_src is not None and abs(i_src) > SOURCE_OVERCURRENT_A:
                findings.append(
                    Finding(
                        severity=Severity.WARN,
                        rule_id="SOURCE_OVERCURRENT",
                        component_ids=[c.id],
                        message=(
                            f"Source {c.id} supplies {abs(i_src):.2f} A, "
                            f"over the {SOURCE_OVERCURRENT_A:g} A limit."
                        ),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Combined entry point and report
# ---------------------------------------------------------------------------

def check_rules(
    netlist: Netlist | dict | list | tuple,
    sim_result: SimResult | dict | Any | None = None,
) -> list[Finding]:
    """All findings for ``netlist``: pre-sim ERC always, post-sim checks
    only when ``sim_result`` is given. Findings are in rule order
    (use :func:`verify` for severity sorting)."""
    findings = pre_sim_checks(netlist)
    if sim_result is not None:
        findings.extend(post_sim_checks(netlist, sim_result))
    return findings


def _component_power(
    components: list[Component],
    node_voltages: dict[str, float],
    branch_currents: dict[str, float],
) -> dict[str, float]:
    """Per-component dissipated power in watts, where derivable.
    Resistor: V^2/R (else I^2*R). LED: |V_anode - V_cathode| * |I_led|.
    Source: |V+ - V-| * |I| (power delivered, reported positive)."""
    power: dict[str, float] = {}
    for c in components:
        if len(c.nodes) < 2:
            continue
        v0 = _voltage_at(c.nodes[0], node_voltages)
        v1 = _voltage_at(c.nodes[1], node_voltages)
        if _is_resistor(c):
            if v0 is not None and v1 is not None:
                power[c.id] = (v0 - v1) ** 2 / c.value
            else:
                branch = _branch_current_for(c.id, branch_currents)
                if branch is not None:
                    power[c.id] = branch**2 * c.value
        elif _is_led(c):
            i_led = _led_current(c, components, node_voltages, branch_currents)
            if i_led is not None and v0 is not None and v1 is not None:
                power[c.id] = abs(v0 - v1) * abs(i_led)
        elif _is_source(c):
            i_src = _branch_current_for(c.id, branch_currents)
            if i_src is None and _is_current_source(c):
                i_src = c.value
            if i_src is not None and v0 is not None and v1 is not None:
                power[c.id] = abs(v0 - v1) * abs(i_src)
    return power


def _uncertain(
    netlist: Netlist,
    sim_result: SimResult | dict | Any | None,
    components: list[Component],
) -> list[str]:
    """Human-readable notes for checks that could not be run, so a
    PASS verdict is never mistaken for a fully verified circuit."""
    uncertain: list[str] = []
    if sim_result is None:
        return uncertain
    node_voltages, branch_currents, success, error = _sim_maps(sim_result)
    if success is False:
        uncertain.append(
            f"Simulation did not succeed: {error or 'no error reported'}."
        )

    for c in components:
        if _is_led(c) and len(c.nodes) >= 2:
            if _led_current(c, components, node_voltages, branch_currents) is None:
                uncertain.append(
                    f"{c.id}: LED current could not be derived "
                    f"(no branch current and KCL underdetermined at its nodes), "
                    f"so LED over/undercurrent was not checked."
                )
        if _is_resistor(c):
            if c.rating_watts is None:
                # Only possible for a Netlist built with model_construct
                # (validation bypassed): a validated resistor always
                # carries its 0.25 W ir.py default.
                uncertain.append(
                    f"{c.id}: power rating not specified, assumed "
                    f"{DEFAULT_RESISTOR_RATING_W:g} W for the power check."
                )
            v0 = _voltage_at(c.nodes[0], node_voltages) if len(c.nodes) >= 2 else None
            v1 = _voltage_at(c.nodes[1], node_voltages) if len(c.nodes) >= 2 else None
            if v0 is None or v1 is None:
                if _branch_current_for(c.id, branch_currents) is None:
                    uncertain.append(
                        f"{c.id}: neither node voltages nor a branch "
                        f"current were available, so resistor power was not checked."
                    )
        if _is_voltage_source(c):
            if _branch_current_for(c.id, branch_currents) is None:
                uncertain.append(
                    f"{c.id}: source current not in branch_currents, "
                    f"so source overcurrent was not checked."
                )
    return uncertain


def verify(
    netlist: Netlist | dict | list | tuple,
    sim_result: SimResult | dict | Any | None = None,
) -> Report:
    """Run all rules and summarise as an ``ir.Report``.

    Verdict is derived from the findings by ``ir.Report`` itself
    (any STOP -> STOP, else any WARN -> WARN, else PASS) and the
    findings are stored sorted STOP > WARN > INFO.
    """
    netlist = _coerce_netlist(netlist)
    findings = check_rules(netlist, sim_result)
    findings = sorted(findings, key=lambda f: SEVERITY_RANK[f.severity])

    if any(f.severity is Severity.STOP for f in findings):
        verdict = Verdict.STOP
    elif any(f.severity is Severity.WARN for f in findings):
        verdict = Verdict.WARN
    else:
        verdict = Verdict.PASS

    components = list(netlist.components)
    node_voltages, branch_currents, _, _ = _sim_maps(sim_result)

    return Report(
        verdict=verdict,
        findings=findings,
        node_voltages=node_voltages,
        branch_currents=branch_currents,
        power=_component_power(components, node_voltages, branch_currents)
        if sim_result is not None
        else {},
        netlist_used=netlist,
        uncertain=_uncertain(netlist, sim_result, components),
    )
