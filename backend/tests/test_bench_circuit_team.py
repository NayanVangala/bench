"""Bench backlog #8: circuit goal mode as a real team.

``pipeline.design_team`` / ``run_goal(team=True)`` give the circuit
designers strategies like ``design_ai.DESIGNERS`` (Faithful / Simple /
Safe / Efficient) and emit per-designer ``agent`` / ``state`` /
``score`` events like ``design_ai.propose_team``, so the dock shows
one lane per designer for circuits too.

Fake LLM + analytic simulator only: no real Anthropic/Gemini call,
no ngspice. The simulator returns the same dict shape as
``spice.parse_op_output`` (node voltages keyed to the netlist's own
node names, lowercase branch-current / power keys), so the real
``rules.verify`` decides every verdict.

Pinned behaviour:
* strategy names are single ``\\w+`` words (``Efficient``, not
  ``Low-power``): the dock's Judge regex is ``/Designer (\\w+)/``.
* events are exactly ``Designer <Name>`` (working, then done with a
  numeric ``score``, ``calls`` and ``physics``), ``Judge`` (done,
  ``"Winner: Designer <Name> ..."``) and ``Physics`` (done,
  ``"Verdict <V>: ..."``).
* the winner is picked by verdict rank, NOT by the tuner score:
  the regression case is a PASS-but-dim candidate (5 mA LED, tuner
  71.7) beating a WARN candidate whose LED sits exactly on the
  12 mA target (tuner 89.98). The tuner score is emitted, never the
  picker, and no tuning runs inside the pipeline.
* ``run_goal`` without ``team=True`` is unchanged (best-of-3, the
  aggregate lowercase ``designer`` event, no ``Designer`` lanes).
* the terminal frame stays dialect A: ``event == "result"`` with the
  flat report and ``done: true``.
"""
from __future__ import annotations

import re

import pytest

from netproof.ir import Component, ComponentType, Netlist
from netproof.pipeline import (
    CIRCUIT_DESIGNERS,
    circuit_score,
    design_team,
    run_goal,
)

SOURCE_V = 9.0
LED_VF = 2.0


def led_circuit(r_ohms: float, rating_watts: float = 0.25) -> Netlist:
    """9 V red-LED circuit with series resistor ``r_ohms``."""
    return Netlist(
        title=f"9V red LED R={r_ohms:g}",
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
                rating_watts=rating_watts,
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


# Resistor values and what the real rules do with them (I = (9-2)/R):
#   R=1400 -> 5.0 mA, PASS, tuner 95 - 40*|5-12|/12 = 71.67 (dim)
#   R=583  -> 12.0 mA on target, but 0.084 W > 70% of a 0.1 W rating
#             -> WARN RESISTOR_POWER, tuner 95 - 5 - 0.02 = 89.98 (on target)
#   R=470  -> 14.9 mA, PASS, tuner 95 - 40*|14.9-12|/12 = 85.36
#   R=100  -> 70 mA, STOP LED_OVERCURRENT, tuner strongly negative
DIM_R = 1400.0
TARGET_R = 583.0
GOOD_R = 470.0
BAD_R = 100.0


class AnalyticSim:
    """Fake simulator: analytic series-loop operating point, real rules."""

    def __init__(self) -> None:
        self.calls: list[Netlist] = []

    def simulate(self, netlist: Netlist) -> dict:
        self.calls.append(netlist)
        source = next(
            c for c in netlist.components if c.type == "voltage_source"
        )
        resistor = next(
            c for c in netlist.components if c.type == "resistor"
        )
        led = next(c for c in netlist.components if c.type == "led")
        current = (source.value - led.value) / resistor.value
        return {
            "node_voltages": {
                source.nodes[0]: source.value,
                source.nodes[1]: 0.0,
                led.nodes[0]: led.value,
                led.nodes[1]: 0.0,
            },
            "branch_currents": {
                source.id.lower(): -current,
                resistor.id.lower(): current,
                led.id.lower(): current,
            },
            "power": {
                resistor.id.lower(): current**2 * resistor.value,
                led.id.lower(): current * led.value,
                source.id.lower(): -source.value * current,
            },
        }

    __call__ = simulate
    run = simulate


class TeamFakeLLM:
    """Keyword-routing fake: one candidate per designer strategy.

    ``design(prompt, n=1)`` returns a single-element list keyed by the
    strategy text in the prompt (``faithfully`` / ``fewest parts`` /
    ``safety first`` / ``as little power``), like the ``SeqClient``
    in ``tests/test_design_ai.py``.
    """

    def __init__(self, by_keyword: dict, fail_keyword: str | None = None):
        self.by_keyword = by_keyword
        self.fail_keyword = fail_keyword
        self.design_calls: list[str] = []

    def design(self, prompt: str, n: int = 3) -> list[Netlist]:
        self.design_calls.append(prompt)
        if self.fail_keyword and self.fail_keyword in prompt:
            raise RuntimeError("LLM unavailable: simulated API failure")
        for key, netlist in self.by_keyword.items():
            if key in prompt:
                return [netlist]
        raise RuntimeError(f"no design for prompt: {prompt[:80]!r}")


class SingleNetlistLLM:
    """Real-seam fake: ``design(prompt)`` with no ``n``, one Netlist."""

    def __init__(self, by_keyword: dict):
        self.by_keyword = by_keyword
        self.design_calls: list[str] = []

    def design(self, prompt: str) -> Netlist:
        self.design_calls.append(prompt)
        for key, netlist in self.by_keyword.items():
            if key in prompt:
                return netlist
        raise RuntimeError(f"no design for prompt: {prompt[:80]!r}")


def _mismatch_team() -> TeamFakeLLM:
    """Faithful dim-PASS, Simple WARN-on-target, Safe good-PASS,
    Efficient STOP. The picker must take Faithful (verdict rank,
    earliest PASS tie) even though Simple's tuner score is highest."""
    return TeamFakeLLM(
        {
            "faithfully": led_circuit(DIM_R),
            "fewest parts": led_circuit(TARGET_R, rating_watts=0.1),
            "safety first": led_circuit(GOOD_R),
            "as little power": led_circuit(BAD_R),
        }
    )


def _designer_events(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("agent") == f"Designer {name}"]


def test_strategy_names_are_single_words():
    """Dock Judge regex is /Designer (\\w+)/: no hyphens or spaces."""
    assert [n for n, _ in CIRCUIT_DESIGNERS] == [
        "Faithful",
        "Simple",
        "Safe",
        "Efficient",
    ]
    assert all(re.fullmatch(r"\w+", n) for n, _ in CIRCUIT_DESIGNERS)
    assert "Low-power" not in [n for n, _ in CIRCUIT_DESIGNERS]


def test_team_picks_by_verdict_rank_not_tuner_score():
    """Regression for the picker/tuner mismatch: PASS-but-dim (tuner
    71.7) wins over WARN-on-target (tuner 89.98). The tuner score is
    emitted per designer, never the picker."""
    llm = _mismatch_team()
    sim = AnalyticSim()
    events: list[dict] = []
    result = design_team(
        "9V red LED circuit", llm=llm, simulator=sim, on_event=events.append
    )
    # Winner: Faithful, the earliest PASS candidate (R=1400, 5 mA).
    assert result.netlist.components[1].value == DIM_R
    assert result.report.verdict == "PASS"
    assert len(result.candidates) == 4
    assert len(sim.calls) == 4, "every candidate is simulated"

    # Leaderboard is in pick order, winner first; scores are the
    # tuner scores, and the WARN candidate outscores the winner.
    assert [row["name"] for row in result.leaderboard] == [
        "Faithful",
        "Safe",
        "Simple",
        "Efficient",
    ]
    by_name = {row["name"]: row for row in result.leaderboard}
    assert by_name["Faithful"]["score"] == pytest.approx(71.67, abs=0.01)
    assert by_name["Simple"]["score"] == pytest.approx(89.98, abs=0.01)
    assert by_name["Simple"]["score"] > by_name["Faithful"]["score"]
    assert by_name["Safe"]["verdict"] == "PASS"
    assert by_name["Simple"]["verdict"] == "WARN"
    assert by_name["Efficient"]["verdict"] == "STOP"

    # Each designer was asked once, with its own strategy text.
    assert len(llm.design_calls) == 4
    for keyword in ("faithfully", "fewest parts", "safety first", "as little power"):
        assert any(keyword in call for call in llm.design_calls), keyword


def test_team_events_match_propose_team_dialect():
    """Per-designer working/done + Judge + Physics, with state/score/
    calls/physics intact (the coordinator forwards them through
    ``_pipeline_progress_event``; the dock regexes need them)."""
    llm = _mismatch_team()
    events: list[dict] = []
    design_team(
        "9V red LED circuit",
        llm=llm,
        simulator=AnalyticSim(),
        on_event=events.append,
    )
    for name, _ in CIRCUIT_DESIGNERS:
        mine = _designer_events(events, name)
        working = [e for e in mine if e.get("state") == "working"]
        done = [e for e in mine if e.get("state") == "done"]
        assert len(working) == 1, f"Designer {name}: {mine!r}"
        assert working[0]["stage"] == "agent"
        assert len(done) == 1, f"Designer {name}: {mine!r}"
        assert done[0]["stage"] == "verify"
        assert isinstance(done[0]["score"], float)
        assert done[0]["calls"] == 1
        assert done[0]["physics"] == 1
        assert "score" in done[0]["message"]

    judge = [e for e in events if e.get("agent") == "Judge"]
    assert len(judge) == 1
    assert judge[0]["stage"] == "verify" and judge[0]["state"] == "done"
    assert judge[0]["score"] == pytest.approx(71.67, abs=0.01)
    # The dock's winner regex must find the winner in this message.
    match = re.search(r"Designer (\w+)", judge[0]["message"])
    assert match and match.group(1) == "Faithful"
    assert judge[0]["message"].startswith("Winner: Designer Faithful")

    physics = [e for e in events if e.get("agent") == "Physics"]
    assert {e.get("state") for e in physics} == {"working", "done"}
    done_physics = [e for e in physics if e.get("state") == "done"]
    assert len(done_physics) == 1
    assert done_physics[0]["message"].startswith("Verdict PASS")

    # No lowercase aggregate designer lane, no hyphenated name.
    assert not any(e.get("agent") == "designer" for e in events)
    assert not any("Low-power" in (e.get("agent") or "") for e in events)
    assert all(
        isinstance(e.get("elapsed_s"), (int, float)) for e in events
    )


def test_team_survives_a_failing_designer():
    """One designer's LLM failure is a failed event, not a lost run."""
    llm = TeamFakeLLM(
        {
            "faithfully": led_circuit(DIM_R),
            "fewest parts": led_circuit(TARGET_R, rating_watts=0.1),
            "safety first": led_circuit(GOOD_R),
            "as little power": led_circuit(BAD_R),
        },
        fail_keyword="as little power",
    )
    events: list[dict] = []
    result = design_team(
        "9V red LED circuit",
        llm=llm,
        simulator=AnalyticSim(),
        on_event=events.append,
    )
    assert result.report.verdict == "PASS"
    assert len(result.leaderboard) == 3
    failed = [
        e
        for e in events
        if e.get("agent") == "Designer Efficient"
        and e.get("state") == "failed"
    ]
    assert len(failed) == 1
    assert "LLM call failed" in failed[0]["message"]


def test_team_all_designers_failing_raises():
    llm = TeamFakeLLM({}, fail_keyword="Strategy")
    with pytest.raises(RuntimeError, match="LLM call failed"):
        design_team("9V red LED circuit", llm=llm, simulator=AnalyticSim())


def test_team_accepts_real_seam_single_netlist():
    """``llm.design(prompt)`` with no ``n`` (the real ``llm.py``
    seam) works: one Netlist per designer, no list to unwrap."""
    llm = SingleNetlistLLM(
        {
            "faithfully": led_circuit(DIM_R),
            "fewest parts": led_circuit(GOOD_R),
            "safety first": led_circuit(GOOD_R),
            "as little power": led_circuit(BAD_R),
        }
    )
    result = design_team(
        "9V red LED circuit", llm=llm, simulator=AnalyticSim()
    )
    assert result.report.verdict == "PASS"
    assert len(llm.design_calls) == 4


def test_team_designers_param_limits_the_team():
    llm = _mismatch_team()
    result = design_team(
        "9V red LED circuit",
        llm=llm,
        simulator=AnalyticSim(),
        designers=2,
    )
    assert len(result.candidates) == 2
    assert [row["name"] for row in result.leaderboard] == [
        "Faithful",
        "Simple",
    ]
    assert len(llm.design_calls) == 2


def test_circuit_score_is_the_tuner_score():
    """``circuit_score`` is ``tuner._circuit_score`` on the verified
    report: PASS-dim 71.67 < WARN-on-target 89.98, and a no-LED
    circuit scores 95 - 25*STOP - 5*WARN."""
    from netproof.pipeline import analyze

    sim = AnalyticSim()
    dim = analyze(led_circuit(DIM_R), simulator=sim)
    target = analyze(led_circuit(TARGET_R, rating_watts=0.1), simulator=sim)
    assert str(dim.verdict) == "PASS"
    assert str(target.verdict) == "WARN"
    assert circuit_score(dim, led_circuit(DIM_R)) == pytest.approx(71.67, abs=0.01)
    assert circuit_score(target, led_circuit(TARGET_R, rating_watts=0.1)) == pytest.approx(89.98, abs=0.01)


def _drive_goal(goal: str, **kwargs):
    """Drive ``run_goal`` to completion; return (events, result)."""
    gen = run_goal(goal, **kwargs)
    events: list[dict] = []
    result = None
    try:
        while True:
            events.append(next(gen))
    except StopIteration as done:
        result = done.value
    return events, result


def test_run_goal_team_streams_designer_lanes_and_flat_terminal():
    """``run_goal(team=True)``: Designer/Judge/Physics lanes in the
    stream, elapsed_s non-decreasing, terminal event still dialect A
    (``event: result`` + flat report). ``done: true`` is added to the
    terminal frame by the server's ``_run_stream`` from the returned
    ``PipelineResult``, not by the pipeline, so it is asserted on the
    server-built shape only in the docstring, not here."""
    events, result = _drive_goal(
        "9V red LED circuit",
        llm=_mismatch_team(),
        simulator=AnalyticSim(),
        team=True,
    )
    agents = [e.get("agent") for e in events]
    for name, _ in CIRCUIT_DESIGNERS:
        assert f"Designer {name}" in agents
    assert "Judge" in agents and "Physics" in agents
    # No aggregate lowercase designer/verifier events in team mode:
    # they would duplicate the per-designer / Physics lanes.
    assert "designer" not in agents
    assert "verifier" not in agents

    elapsed = [e["elapsed_s"] for e in events]
    assert elapsed == sorted(elapsed)
    assert all(isinstance(v, (int, float)) for v in elapsed)

    judge = [e for e in events if e.get("agent") == "Judge"][0]
    assert re.search(r"Designer (\w+)", judge["message"]).group(1) == "Faithful"

    terminal = events[-1]
    assert terminal.get("event") == "result"
    # ``done: true`` is added by server._run_stream when it rebuilds
    # this terminal event from the PipelineResult return value.
    assert terminal["data"]["verdict"] == "PASS"
    assert terminal["data"]["netlist"]["components"][1]["value"] == DIM_R
    assert [row["name"] for row in terminal["data"]["leaderboard"]] == [
        "Faithful",
        "Safe",
        "Simple",
        "Efficient",
    ]
    assert result is not None
    assert [row["name"] for row in result.leaderboard] == [
        "Faithful",
        "Safe",
        "Simple",
        "Efficient",
    ]
    assert result.report.verdict == "PASS"


def test_run_goal_default_is_unchanged_best_of_three():
    """Without ``team=True`` the goal path is exactly the old one:
    one ``design(prompt, n=3)`` call, aggregate ``designer`` event,
    no ``Designer`` lanes."""
    from netproof.pipeline import design as _design  # noqa: F401

    class OldFakeLLM:
        def __init__(self):
            self.design_calls: list[tuple[str, int]] = []

        def design(self, prompt: str, n: int = 3) -> list[Netlist]:
            self.design_calls.append((prompt, n))
            return [
                led_circuit(BAD_R),
                led_circuit(GOOD_R),
                led_circuit(DIM_R),
            ]

        def propose_repair(self, netlist, report=None):
            return netlist

    llm = OldFakeLLM()
    events, result = _drive_goal(
        "9V red LED circuit", llm=llm, simulator=AnalyticSim()
    )
    assert llm.design_calls == [("9V red LED circuit", 3)]
    assert not any(
        (e.get("agent") or "").startswith("Designer ") for e in events
    )
    assert any(e.get("agent") == "designer" for e in events)
    assert result.report.verdict == "PASS"
    assert result.netlist.components[1].value == GOOD_R
    assert result.leaderboard == []


# ---------------------------------------------------------------------------
# Goal-mode prefs (swarm/goal-prefs): optional objective / target_ma,
# threaded run_goal -> design_team, honoured by the tuner score and
# the designer prompts. Default (no prefs) is exactly the old
# behaviour: balanced / 12 mA, no prompt note.
# ---------------------------------------------------------------------------

def test_design_team_prefs_battery_retargets_tuner_score():
    """``prefs={"objective": "battery"}`` (5 mA target): the dim
    5 mA PASS candidate now scores 95.0 (on target) while the
    12 mA WARN candidate drops to ~33.95. The winner is still
    Faithful: prefs retarget the score, they never pick the winner
    (verdict rank still rules)."""
    result = design_team(
        "9V red LED circuit",
        llm=_mismatch_team(),
        simulator=AnalyticSim(),
        prefs={"objective": "battery"},
    )
    assert result.netlist.components[1].value == DIM_R
    assert result.report.verdict == "PASS"
    by_name = {row["name"]: row for row in result.leaderboard}
    assert by_name["Faithful"]["score"] == pytest.approx(95.0, abs=0.01)
    assert by_name["Simple"]["score"] == pytest.approx(33.95, abs=0.05)
    assert by_name["Safe"]["score"] == pytest.approx(15.85, abs=0.05)
    assert by_name["Simple"]["score"] < by_name["Faithful"]["score"]
    assert [row["name"] for row in result.leaderboard] == [
        "Faithful",
        "Safe",
        "Simple",
        "Efficient",
    ]


def test_design_team_prefs_target_ma_overrides_objective():
    """An explicit ``target_ma`` wins over the objective's target:
    battery + 12 mA scores exactly like the balanced default."""
    result = design_team(
        "9V red LED circuit",
        llm=_mismatch_team(),
        simulator=AnalyticSim(),
        prefs={"objective": "battery", "target_ma": 12.0},
    )
    by_name = {row["name"]: row for row in result.leaderboard}
    assert by_name["Faithful"]["score"] == pytest.approx(71.67, abs=0.01)
    assert by_name["Simple"]["score"] == pytest.approx(89.98, abs=0.01)


def test_design_team_prefs_unknown_objective_raises():
    """An unknown objective is a caller error, not a silent
    fallback to balanced."""
    with pytest.raises(ValueError, match="unknown objective"):
        design_team(
            "9V red LED circuit",
            llm=_mismatch_team(),
            simulator=AnalyticSim(),
            prefs={"objective": "dim"},
        )
    with pytest.raises(ValueError, match="target_ma must be positive"):
        design_team(
            "9V red LED circuit",
            llm=_mismatch_team(),
            simulator=AnalyticSim(),
            prefs={"target_ma": 0},
        )


def test_design_team_prefs_reaches_designer_prompt():
    """With prefs, every designer prompt carries the preference
    note (objective + resolved mA target); without prefs the prompt
    is exactly goal + strategy, no note."""
    llm = _mismatch_team()
    design_team(
        "9V red LED circuit",
        llm=llm,
        simulator=AnalyticSim(),
        prefs={"objective": "battery"},
    )
    assert len(llm.design_calls) == 4
    for call in llm.design_calls:
        assert "Preference: battery objective" in call
        assert "near 5 mA" in call

    plain = _mismatch_team()
    design_team("9V red LED circuit", llm=plain, simulator=AnalyticSim())
    assert len(plain.design_calls) == 4
    assert all("Preference:" not in call for call in plain.design_calls)


def test_run_goal_prefs_threaded_to_team_and_terminal():
    """``run_goal(team=True, prefs=...)`` threads prefs to the team
    (battery scores on the terminal frame) and reports the resolved
    objective on the terminal event and the PipelineResult; the
    default run still reports ``balanced`` with the old scores."""
    events, result = _drive_goal(
        "9V red LED circuit",
        llm=_mismatch_team(),
        simulator=AnalyticSim(),
        team=True,
        prefs={"objective": "battery"},
    )
    terminal = events[-1]
    assert terminal.get("event") == "result"
    assert terminal["data"]["objective"] == "battery"
    assert result.objective == "battery"
    by_name = {row["name"]: row for row in terminal["data"]["leaderboard"]}
    assert by_name["Faithful"]["score"] == pytest.approx(95.0, abs=0.01)

    _, default = _drive_goal(
        "9V red LED circuit",
        llm=_mismatch_team(),
        simulator=AnalyticSim(),
        team=True,
    )
    assert default.objective == "balanced"
    by_name = {row["name"]: row for row in default.leaderboard}
    assert by_name["Faithful"]["score"] == pytest.approx(71.67, abs=0.01)
