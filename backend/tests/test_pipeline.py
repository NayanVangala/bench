"""Pipeline tests for Netproof MVP.

File owner: backend/tests/test_pipeline.py ONLY.

Status of the modules under test (checked 2026-11-15)
------------------------------------------------------
``backend/netproof/pipeline.py``, ``rules.py``, ``spice.py`` and ``llm.py``
do NOT exist yet. Only ``ir.py`` and ``parts.py`` exist, and this file
relies on those two exactly as written. The tests below are therefore
written against the contract in ``README.md`` plus the task brief, and
they will fail at collection (``ModuleNotFoundError: netproof.pipeline``)
until the pipeline owner lands the module. That failure is the correct
signal, not a bug in this file. ``py_compile`` passes.

Assumed pipeline API (the pipeline owner must implement this)
------------------------------------------------------------
``from netproof.pipeline import analyze, design, repair_loop, run_pipeline``

* ``analyze(netlist, *, simulator, llm=None) -> Report``
  Simulate once, apply the rules, return the ``ir.Report``. The verdict
  comes from the simulator + rules, never from the LLM.
* ``repair_loop(netlist, *, simulator, llm, max_rounds=3) -> Report``
  (or an object with a ``.report`` attribute, e.g. a RepairResult).
  Check once; while the verdict is not PASS and rounds remain, ask the
  LLM for a repair, re-simulate, and count one round per
  repair-proposal + re-simulation. The initial check is not a round.
  Returns PASS as soon as a repair re-simulates clean, and stops after
  ``max_rounds`` rounds still STOP if no repair works.
* ``design(prompt, *, llm, simulator, n=3) -> (netlist, report)``
  (or an object with ``.netlist`` and ``.report``). Ask the LLM for ``n``
  candidate netlists, simulate every candidate, return the one with the
  best verdict (PASS beats WARN beats STOP). Never just return the first
  candidate.
* ``run_pipeline(netlist, *, simulator, llm=None) -> Iterator[event]``
  Yield progress events as the pipeline runs. Every event must expose
  ``stage``, ``agent``, ``message`` and ``elapsed_s`` (attributes or
  dict keys). ``elapsed_s`` is seconds since the run started, numeric
  and non-decreasing. ``stage``/``agent``/``message`` are non-empty
  strings.

Assumed LLM seam (the pipeline calls the injected LLM this way)
----------------------------------------------------------------
* ``llm.propose_repair(netlist, report) -> Netlist``
* ``llm.design(prompt, n=3) -> list[Netlist]``
* Any LLM failure must propagate as a visible exception whose message
  names the failure (e.g. ``PipelineError("LLM call failed: ...")`` or
  the original error). It must never be swallowed into a PASS verdict.

Assumed simulator seam
-----------------------
* ``simulator.simulate(netlist) -> Report`` (a plain callable returning
  a Report works too). The injected simulator stands in for
  spice + rules, so the fake below computes the verdict from the
  circuit itself using the real ``parts.py`` limits.

Fake-simulator physics (matches README demo + parts.py)
--------------------------------------------------------
9 V source, red LED (Vf 2.0 V, If_max 0.030 A, If_min 0.001 A), one
series resistor R1. LED current ``I = (9 - 2) / R1``.
``R1 = 100`` -> 70 mA -> STOP ``LED_OVERCURRENT``.
``R1 = 470`` -> ~14.9 mA -> PASS (below the 20 mA warn level too).
``R1 = 47``  -> ~149 mA -> STOP ``LED_OVERCURRENT``.

No network, no API key, no ngspice binary is needed for any test in
this file except the one explicitly gated test at the bottom, which
skips unless both ``netproof.spice`` and the ``ngspice`` binary exist.
"""

from __future__ import annotations

import inspect
import json
import shutil

import pytest

from netproof.ir import (
    Component,
    ComponentType,
    Finding,
    Netlist,
    Report,
    Severity,
    Verdict,
)
from netproof.llm import read_schematic
from netproof.parts import led_if_max, led_if_min
from netproof.pipeline import (
    analyze,
    design,
    repair_loop,
    run_image,
    run_pipeline,
)

# ---------------------------------------------------------------------------
# Circuit fixtures: the README demo circuit, good and bad.
# V1 9 V between "vcc" and ground "0"; R1 "vcc" -> "n1";
# D1 red LED, anode "n1", cathode "0".
# ---------------------------------------------------------------------------

SOURCE_V = 9.0
LED_VF = 2.0
GOOD_R = 470.0
BAD_R = 100.0
WORST_R = 47.0


def led_circuit(r_ohms: float) -> Netlist:
    """9 V red-LED circuit with series resistor ``r_ohms``."""
    return Netlist(
        title="9V red LED",
        components=[
            Component(
                id="V1",
                type=ComponentType.VOLTAGE_SOURCE,
                value=SOURCE_V,
                nodes=["vcc", "0"],
            ),
            Component(
                id="R1",
                type=ComponentType.RESISTOR,
                value=r_ohms,
                nodes=["vcc", "n1"],
            ),
            Component(
                id="D1",
                type=ComponentType.LED,
                value=LED_VF,
                nodes=["n1", "0"],
                color="red",
            ),
        ],
    )


@pytest.fixture
def good() -> Netlist:
    return led_circuit(GOOD_R)


@pytest.fixture
def bad() -> Netlist:
    return led_circuit(BAD_R)


# ---------------------------------------------------------------------------
# Fakes: injected in place of the real LLM and the real ngspice simulator.
# ---------------------------------------------------------------------------


class FakeSimulator:
    """Deterministic stand-in for spice + rules.

    Returns an ``ir.Report`` whose verdict is computed from the netlist
    itself (see module docstring). Records every simulated netlist in
    ``calls`` so tests can prove how many simulations a loop ran.
    """

    def __init__(self) -> None:
        self.calls: list[Netlist] = []

    def simulate(self, netlist: Netlist) -> Report:
        self.calls.append(netlist)
        findings: list[Finding] = []
        if not netlist.has_ground():
            findings.append(
                Finding(
                    severity=Severity.STOP,
                    rule_id="NO_GROUND",
                    component_ids=[],
                    message="No ground node '0' in netlist",
                )
            )
        sources = [
            c
            for c in netlist.components
            if c.type in (ComponentType.VOLTAGE_SOURCE, ComponentType.CURRENT_SOURCE)
        ]
        if not sources:
            findings.append(
                Finding(
                    severity=Severity.STOP,
                    rule_id="NO_SOURCE",
                    component_ids=[],
                    message="No power source in netlist",
                )
            )
        current = self._led_current(netlist)
        voltages: dict[str, float] = {"0": 0.0}
        currents: dict[str, float] = {}
        power: dict[str, float] = {}
        if current is not None:
            led = next(
                c for c in netlist.components if c.type is ComponentType.LED
            )
            resistor = next(
                c for c in netlist.components if c.type is ComponentType.RESISTOR
            )
            limit = led_if_max(led.color or "red")
            minimum = led_if_min(led.color or "red")
            voltages = {"0": 0.0, "vcc": SOURCE_V, "n1": LED_VF}
            currents = {led.id: current, resistor.id: current}
            power = {
                resistor.id: current * current * resistor.value,
                led.id: current * LED_VF,
            }
            if current > limit:
                findings.append(
                    Finding(
                        severity=Severity.STOP,
                        rule_id="LED_OVERCURRENT",
                        component_ids=[led.id],
                        message=(
                            f"{led.id} current {current:.3f} A exceeds "
                            f"{limit:.3f} A limit"
                        ),
                    )
                )
            elif current < minimum:
                findings.append(
                    Finding(
                        severity=Severity.STOP,
                        rule_id="LED_UNDERCURRENT",
                        component_ids=[led.id],
                        message=(
                            f"{led.id} current {current:.3f} A is below "
                            f"{minimum:.3f} A minimum"
                        ),
                    )
                )
        return Report(
            findings=findings,
            node_voltages=voltages,
            branch_currents=currents,
            power=power,
            netlist_used=netlist,
        )

    def _led_current(self, netlist: Netlist) -> float | None:
        sources = [
            c
            for c in netlist.components
            if c.type is ComponentType.VOLTAGE_SOURCE
        ]
        leds = [c for c in netlist.components if c.type is ComponentType.LED]
        resistors = [
            c for c in netlist.components if c.type is ComponentType.RESISTOR
        ]
        if not sources or not leds or not resistors:
            return None
        return (sources[0].value - leds[0].value) / resistors[0].value

    # The pipeline may call the simulator as a plain callable.
    __call__ = simulate
    run = simulate


@pytest.fixture
def sim() -> FakeSimulator:
    return FakeSimulator()


def _with_resistor(netlist: Netlist, value: float) -> Netlist:
    """Return a copy of ``netlist`` with every resistor set to ``value``."""
    return Netlist(
        title=netlist.title,
        components=[
            comp.model_copy(update={"value": value})
            if comp.type is ComponentType.RESISTOR
            else comp
            for comp in netlist.components
        ],
    )


class FakeLLM:
    """Fake LLM: repair always sets R1 to 470 ohms; design returns a
    fixed spread of candidates with the good one in the middle, so a
    pipeline that just picks the first candidate will fail the tests.
    """

    def __init__(self) -> None:
        self.repair_calls: list[tuple[Netlist, Report | None]] = []
        self.design_calls: list[tuple[str, int]] = []

    def propose_repair(
        self, netlist: Netlist, report: Report | None = None
    ) -> Netlist:
        self.repair_calls.append((netlist, report))
        return _with_resistor(netlist, GOOD_R)

    # Aliases for the same repair seam, whichever name the pipeline uses.
    repair = propose_repair
    suggest_repair = propose_repair

    def design(self, prompt: str, n: int = 3) -> list[Netlist]:
        self.design_calls.append((prompt, n))
        return [led_circuit(BAD_R), led_circuit(GOOD_R), led_circuit(WORST_R)]

    # Aliases for the same design seam.
    generate = design
    generate_candidates = design


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


class FailingLLM:
    """LLM that fails on every call, so the pipeline must surface the
    error instead of swallowing it into a verdict."""

    def _fail(self, *args, **kwargs):
        raise RuntimeError("LLM unavailable: simulated API failure")

    propose_repair = _fail
    repair = _fail
    suggest_repair = _fail
    design = _fail
    generate = _fail
    generate_candidates = _fail
    __call__ = _fail


class StubbornLLM(FakeLLM):
    """LLM whose repair changes nothing, so the loop must exhaust its
    round budget and still return STOP."""

    def propose_repair(
        self, netlist: Netlist, report: Report | None = None
    ) -> Netlist:
        self.repair_calls.append((netlist, report))
        return netlist


class AllBadLLM(FakeLLM):
    """LLM whose design candidates are all STOP, so best-of-3 must
    still return a (STOP) best instead of crashing or inventing a PASS."""

    def design(self, prompt: str, n: int = 3) -> list[Netlist]:
        self.design_calls.append((prompt, n))
        return [led_circuit(BAD_R), led_circuit(WORST_R), led_circuit(220.0)]


# ---------------------------------------------------------------------------
# Small adapters: tolerate the documented return-shape options without
# weakening the behavioural assertions.
# ---------------------------------------------------------------------------


def _call(fn, *args, **wanted):
    """Call ``fn`` with only the keyword args its signature accepts.

    Lets the tests pass ``simulator``/``llm``/``max_rounds``/``n`` without
    hard-coding whether a given entry point takes all of them. Genuine
    errors raised inside the function still propagate.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **wanted)
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return fn(*args, **wanted)
    accepted = {p.name for p in params}
    return fn(*args, **{k: v for k, v in wanted.items() if k in accepted})


def _unwrap_report(result) -> Report:
    """Accept a Report directly, an object with ``.report``, or a tuple
    containing a Report."""
    if isinstance(result, Report):
        return result
    report = getattr(result, "report", None)
    if isinstance(report, Report):
        return report
    if isinstance(result, tuple):
        for item in result:
            if isinstance(item, Report):
                return item
    raise AssertionError(f"expected a Report (or .report), got {result!r}")


def _unwrap_design(result) -> tuple[Netlist, Report]:
    """Accept ``(netlist, report)`` in either order, or an object with
    ``.netlist``/``.report`` (netlist also accepted as ``.netlist_used``)."""
    if isinstance(result, tuple):
        nets = [i for i in result if isinstance(i, Netlist)]
        reps = [i for i in result if isinstance(i, Report)]
        assert nets and reps, f"expected (Netlist, Report), got {result!r}"
        return nets[0], reps[0]
    netlist = getattr(result, "netlist", None) or getattr(
        result, "netlist_used", None
    )
    report = getattr(result, "report", None)
    assert isinstance(netlist, Netlist), f"expected .netlist, got {result!r}"
    assert isinstance(report, Report), f"expected .report, got {result!r}"
    return netlist, report


def _field(event, name: str):
    """Read ``name`` off an event object or a dict event."""
    if isinstance(event, dict):
        assert name in event, f"event missing key {name!r}: {event!r}"
        return event[name]
    assert hasattr(event, name), f"event missing attribute {name!r}: {event!r}"
    return getattr(event, name)


def _assert_events_well_formed(events) -> list:
    """Every event must carry stage/agent/message/elapsed_s, with
    elapsed_s numeric, >= 0 and non-decreasing across the run."""
    events = list(events)
    assert events, "pipeline yielded no events"
    previous = 0.0
    for event in events:
        stage = _field(event, "stage")
        agent = _field(event, "agent")
        message = _field(event, "message")
        elapsed = _field(event, "elapsed_s")
        assert isinstance(stage, str) and stage.strip(), f"bad stage: {event!r}"
        assert isinstance(agent, str) and agent.strip(), f"bad agent: {event!r}"
        assert isinstance(message, str) and message.strip(), (
            f"bad message: {event!r}"
        )
        assert isinstance(elapsed, (int, float)) and not isinstance(
            elapsed, bool
        ), f"elapsed_s must be numeric, got {elapsed!r}"
        assert elapsed >= 0, f"elapsed_s must be >= 0, got {elapsed!r}"
        assert elapsed >= previous, (
            f"elapsed_s went backwards: {previous} -> {elapsed}"
        )
        previous = elapsed
    return events


def _resistor_value(netlist: Netlist, component_id: str = "R1") -> float:
    comp = netlist.get(component_id)
    assert comp is not None, f"{component_id} missing from {netlist!r}"
    return comp.value


# ---------------------------------------------------------------------------
# Unmodeled parts: ICs / transistors / crystals / connectors the circuit
# model cannot simulate must be listed, not silently dropped.
# ---------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _ReadFakeClient:
    """Fake Anthropic client: every create() returns the same reply
    text (thread-safe: no shared mutable state, so the 3 parallel
    reads in run_image can all use it)."""

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply_text


def _schematic_reply(unmodeled: list[dict] | None) -> str:
    """A good 9 V LED circuit as a schematic-read reply, plus the
    given unmodeled parts (``None`` means the key is omitted)."""
    obj = led_circuit(GOOD_R).model_dump(mode="json")
    if unmodeled is not None:
        obj["unmodeled"] = unmodeled
    return json.dumps(obj)


UNMODELED_PARTS = [
    {"id": "U1", "type": "microcontroller", "note": "ATmega328P; no SPICE model"},
    {"id": "Q1", "type": "transistor", "note": "2N2222 NPN; not modelable"},
    {"id": "Y1", "type": "crystal", "note": "16 MHz crystal; not modelable"},
    {"id": "J1", "type": "connector", "note": "pin header; not modelable"},
]


def _drive_image(gen):
    """Drive a run_image generator to completion; return
    ``(events, PipelineResult)``."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as done:
        return events, done.value


def test_read_schematic_returns_unmodeled_parts():
    client = _ReadFakeClient(_schematic_reply(UNMODELED_PARTS))
    result = read_schematic(PNG_BYTES, client=client)

    assert isinstance(result, Netlist)
    assert [c.id for c in result.components] == ["V1", "R1", "D1"]
    assert [p.model_dump() for p in result.unmodeled] == UNMODELED_PARTS
    assert len(client.calls) == 1


def test_read_schematic_defaults_unmodeled_to_empty():
    client = _ReadFakeClient(_schematic_reply(None))
    result = read_schematic(PNG_BYTES, client=client)

    assert isinstance(result, Netlist)
    assert result.unmodeled == []
    assert len(client.calls) == 1


def test_run_image_returns_unmodeled_parts(sim):
    client = _ReadFakeClient(_schematic_reply(UNMODELED_PARTS))
    events, result = _drive_image(
        run_image(PNG_BYTES, client=client, simulator=sim)
    )

    # All 3 parallel reads ran against the fake client (no real API).
    assert len(client.calls) == 3
    # Unmodeled parts survive consensus (sorted by id) and are NOT
    # in the simulated netlist.
    assert result.unmodeled == sorted(UNMODELED_PARTS, key=lambda p: p["id"])
    assert {c.id for c in result.netlist.components} == {"V1", "R1", "D1"}
    assert "U1" not in {c.id for c in result.netlist.components}
    assert result.report.verdict is Verdict.PASS
    # The terminal event carries the same list for the UI.
    done = [e for e in events if e.get("event") == "result"]
    assert len(done) == 1
    assert done[0]["data"]["unmodeled"] == result.unmodeled
    # ... and the consensus event names them.
    assert any(
        "U1" in e["message"] and "unmodeled" in e["message"]
        for e in events
        if e["stage"] == "consensus"
    )


def test_run_image_unmodeled_empty_when_everything_modeled(sim):
    client = _ReadFakeClient(_schematic_reply(None))
    _, result = _drive_image(
        run_image(PNG_BYTES, client=client, simulator=sim)
    )
    assert result.unmodeled == []
    assert result.report.verdict is Verdict.PASS


# ---------------------------------------------------------------------------
# Verdicts: good netlist PASS, undersized resistor STOP.
# ---------------------------------------------------------------------------


def test_good_netlist_passes(good, sim, llm):
    report = _unwrap_report(_call(analyze, good, simulator=sim, llm=llm))
    assert report.verdict is Verdict.PASS
    assert not [f for f in report.findings if f.severity is Severity.STOP]
    assert report.branch_currents["D1"] == pytest.approx((9.0 - 2.0) / 470.0)
    assert report.node_voltages["vcc"] == pytest.approx(9.0)
    assert len(sim.calls) == 1, "analyze must simulate exactly once"


def test_undersized_resistor_stops(bad, sim, llm):
    report = _unwrap_report(_call(analyze, bad, simulator=sim, llm=llm))
    assert report.verdict is Verdict.STOP
    overcurrent = [
        f for f in report.findings if f.rule_id == "LED_OVERCURRENT"
    ]
    assert overcurrent, f"expected LED_OVERCURRENT, got {report.findings!r}"
    finding = overcurrent[0]
    assert finding.severity is Severity.STOP
    assert "D1" in finding.component_ids
    # Finding must state the measured number and the limit (ir.py contract).
    assert "0.070" in finding.message
    assert "0.030" in finding.message


# ---------------------------------------------------------------------------
# Repair loop.
# ---------------------------------------------------------------------------


def test_repair_loop_fixes_within_three_rounds(bad, sim, llm):
    result = _call(repair_loop, bad, simulator=sim, llm=llm, max_rounds=3)
    report = _unwrap_report(result)
    assert report.verdict is Verdict.PASS
    assert len(llm.repair_calls) <= 3, (
        f"repair took {len(llm.repair_calls)} rounds, budget is 3"
    )
    assert len(llm.repair_calls) == 1, "fake repair fixes it first try"
    assert len(sim.calls) == 2, "initial check + one re-simulation"
    assert _resistor_value(sim.calls[-1]) == pytest.approx(GOOD_R)
    assert _resistor_value(report.netlist_used) == pytest.approx(GOOD_R)


def test_repair_loop_stops_early_on_pass(good, sim, llm):
    result = _call(repair_loop, good, simulator=sim, llm=llm, max_rounds=3)
    report = _unwrap_report(result)
    assert report.verdict is Verdict.PASS
    assert len(sim.calls) == 1, "already-PASS netlist must not re-simulate"
    assert llm.repair_calls == [], "already-PASS netlist must not ask LLM"


def test_repair_loop_exhausts_rounds_and_stays_stop(bad, sim):
    stubborn = StubbornLLM()
    result = _call(
        repair_loop, bad, simulator=sim, llm=stubborn, max_rounds=3
    )
    report = _unwrap_report(result)
    assert report.verdict is Verdict.STOP
    assert len(stubborn.repair_calls) == 3, "must use the full round budget"
    assert len(sim.calls) == 4, "initial check + 3 re-simulations"


# ---------------------------------------------------------------------------
# Design: best-of-3.
# ---------------------------------------------------------------------------


def test_design_best_of_three_picks_best_verdict(sim, llm):
    result = _call(
        design,
        "9 V red LED indicator",
        simulator=sim,
        llm=llm,
        n=3,
        num_candidates=3,
    )
    netlist, report = _unwrap_design(result)
    assert report.verdict is Verdict.PASS
    assert _resistor_value(netlist) == pytest.approx(GOOD_R)
    assert len(llm.design_calls) == 1, "design must ask the LLM once"
    assert len(sim.calls) == 3, "best-of-3 must simulate all 3 candidates"
    assert _resistor_value(sim.calls[0]) == pytest.approx(BAD_R), (
        "first candidate is the bad one, so PASS proves selection, "
        "not first-pick"
    )


def test_design_best_of_three_with_all_bad_returns_stop(sim):
    result = _call(
        design,
        "9 V red LED indicator",
        simulator=sim,
        llm=AllBadLLM(),
        n=3,
        num_candidates=3,
    )
    netlist, report = _unwrap_design(result)
    assert report.verdict is Verdict.STOP
    assert len(sim.calls) == 3
    assert _resistor_value(netlist) in (BAD_R, WORST_R, 220.0)


# ---------------------------------------------------------------------------
# LLM failure must be a visible error, never a swallowed PASS.
# ---------------------------------------------------------------------------


def test_llm_failure_in_repair_loop_raises_visible_error(bad, sim):
    with pytest.raises(Exception, match="LLM|unavailable|failed"):
        _call(repair_loop, bad, simulator=sim, llm=FailingLLM(), max_rounds=3)


def test_llm_failure_in_design_raises_visible_error(sim):
    with pytest.raises(Exception, match="LLM|unavailable|failed"):
        _call(
            design,
            "9 V red LED indicator",
            simulator=sim,
            llm=FailingLLM(),
            n=3,
            num_candidates=3,
        )


# ---------------------------------------------------------------------------
# Progress events.
# ---------------------------------------------------------------------------


def test_run_pipeline_events_have_required_fields(good, sim, llm):
    events = _assert_events_well_formed(
        _call(run_pipeline, good, simulator=sim, llm=llm)
    )
    assert len(events) >= 2, "expected at least a start and a done event"
    stages = [_field(e, "stage").lower() for e in events]
    assert any("simul" in s or "check" in s or "verif" in s for s in stages), (
        f"no simulate/check stage among {stages!r}"
    )


def test_run_pipeline_bad_netlist_still_yields_events(bad, sim, llm):
    events = _assert_events_well_formed(
        _call(run_pipeline, bad, simulator=sim, llm=llm)
    )
    text = " ".join(
        _field(e, "stage") + " " + _field(e, "message") for e in events
    )
    assert "LED_OVERCURRENT" in text or "STOP" in text, (
        "events must surface the STOP verdict/rule, got: " + text
    )


# ---------------------------------------------------------------------------
# Real ngspice, only when it is actually available. Skipped otherwise.
# ---------------------------------------------------------------------------


def test_real_ngspice_good_netlist_passes(good, bad, llm):
    pytest.importorskip("netproof.spice", reason="spice module not implemented yet")
    if shutil.which("ngspice") is None:
        pytest.skip("ngspice binary not installed")
    import netproof.spice as spice

    simulate = getattr(spice, "simulate", None)
    assert simulate is not None, "netproof.spice has no simulate() yet"
    good_report = _unwrap_report(
        _call(analyze, good, simulator=simulate, llm=llm)
    )
    assert good_report.verdict is Verdict.PASS
    bad_report = _unwrap_report(
        _call(analyze, bad, simulator=simulate, llm=llm)
    )
    assert bad_report.verdict is Verdict.STOP
    assert any(
        f.rule_id == "LED_OVERCURRENT" for f in bad_report.findings
    )
