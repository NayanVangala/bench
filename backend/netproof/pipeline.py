"""Netproof pipeline (MVP).

File owner: backend/netproof/pipeline.py ONLY.

Orchestration only: this module holds no simulation, no rules, and no
model calls of its own. It wires together the real sibling modules:

* ``ir.py``     -- Pydantic types: ``Netlist`` / ``Component`` /
  ``Finding`` / ``Report`` (verdict derived from findings: any STOP
  -> STOP, else any WARN -> WARN, else PASS).
* ``spice.py``  -- ``simulate(netlist) -> SimResult`` (the one-call
  real path: ``netlist_to_spice`` + ``run_ngspice``),
  ``netlist_to_spice(netlist) -> str`` and
  ``run_ngspice(spice_text) -> SimResult``. ``run_ngspice`` raises
  ``RuntimeError`` with a clear message when the ngspice binary is
  missing or times out, and returns ``SimResult(success=False)`` for
  a failed simulation.
* ``rules.py``  -- ``verify(netlist, sim_result=None) -> ir.Report``,
  ``check_rules(...)``, ``pre_sim_checks(...)`` /
  ``post_sim_checks(...)``. Findings are ``ir.Finding`` objects
  (``rule_id``, ``component_ids``, ``message``). A sim result is a
  ``spice.SimResult`` or a plain dict with ``node_voltages`` /
  ``branch_currents`` / ``power`` keys.
* ``llm.py``    -- the ONLY module that talks to the LLM:
  ``read_schematic(image_bytes, client=None) ->
  SchematicNetlist`` (a ``Netlist`` plus an ``unmodeled`` list of
  ``{id, type, note}`` dicts for parts on the schematic the circuit
  model cannot simulate — ICs, transistors, crystals, connectors —
  listed instead of silently dropped),
  ``design(goal, client=None) -> Netlist`` (ONE netlist per call),
  ``repair(netlist, findings, client=None) ->
  (netlist, reasons)``. There is NO ``llm.propose_repair``. Failures
  raise ``ValueError`` after one retry, never a fallback netlist.

Pipeline contract (``backend/tests/test_pipeline.py``)
------------------------------------------------------
``from netproof.pipeline import analyze, design, repair_loop,
run_pipeline``.

* ``analyze(netlist, *, simulator, llm=None) -> ir.Report``.
  Simulate once, apply the rules, return the report. The verdict
  comes from the simulator + rules, never from the LLM. When
  pre-simulation (ERC) checks already contain a STOP finding the
  simulation is skipped (a degenerate deck must not burn ngspice
  time) and the report is built from the ERC findings alone.
* ``repair_loop(netlist, *, simulator, llm, max_rounds=3) ->
  RepairResult`` (``.report`` is the final ``ir.Report``).
  Check once; while the verdict is not PASS and rounds remain, ask
  the LLM for a repair, re-simulate, count one round per
  repair-proposal + re-simulation. The initial check is not a round.
  Stops early on PASS; after ``max_rounds`` rounds still STOP if no
  repair works. Also bounded by ``deadline_s`` (default 90 s).
* ``design(prompt, *, llm, simulator, n=3) -> DesignResult``
  (``.netlist`` / ``.report``). Ask the LLM for ``n`` candidate
  netlists, simulate every candidate, return the one with the best
  verdict (PASS beats WARN beats STOP; ties: fewest STOP, then
  fewest WARN, then earliest). Never just return the first.
* ``design_team(prompt, *, llm, simulator, designers=4,
  on_event=None) -> DesignResult`` (bench #8). Like ``design`` but
  as a real team: each of the first ``designers`` strategists in
  ``CIRCUIT_DESIGNERS`` (Faithful / Simple / Safe / Efficient —
  single-word names, so the dock's ``/Designer (\\w+)/`` winner
  regex matches) gets the goal plus its strategy, proposes one
  candidate in parallel, and every candidate is simulated +
  verified. The winner is picked by the same verdict rank as
  ``design`` (the tuner score is emitted per designer for the dock
  but never picks the winner — the two scores can disagree, e.g. a
  PASS-but-dim candidate scores 71.7 while a WARN on-target one
  scores ~90.0; no tuning runs inside the pipeline). ``on_event``
  receives the ``propose_team``-dialect events (``Designer <Name>``
  working/done with ``state`` / ``score`` / ``calls`` /
  ``physics``, ``Judge`` with ``"Winner: Designer <Name> ..."``,
  ``Physics`` with the winner's verdict). ``run_goal(..., team=True)``
  runs this team instead of ``design`` and drops the aggregate
  lowercase ``designer`` / ``verifier`` events, so the dock shows
  one lane per designer.
* ``run_pipeline(netlist, *, simulator, llm=None) ->
  Iterator[event]``. A generator yielding progress events. Every
  event is a dict with exactly ``stage``, ``agent``, ``message`` and
  ``elapsed_s`` (seconds since the run started, numeric and
  non-decreasing).

Pipeline contract (``backend/tests/test_consensus.py``)
-------------------------------------------------------
``consensus_netlists(runs)`` (aliases: ``consensus``,
``build_consensus``, ``run_consensus``, ``merge_candidates``) merges
a list of ``Netlist`` runs (``N_CANDIDATES`` transcriptions: 3 by
default, 5 in production via ``NETPROOF_AGENTS``) into
``(netlist, uncertain)``. Semantics, pinned by that file's reference
implementation:

* Node labels are canonicalised per run before any comparison, so a
  relabel does not look like a different circuit. A node's canonical
  label comes from the sorted tuple of component ids incident to it
  (ids only, not types/values, so a value or type disagreement
  cannot break the relabelling). Ground ``"0"`` is never renamed.
  The accepted netlist reports nodes in the first run's original
  labels, so an all-agree input comes back literally unchanged.
* A component id seen by a majority of the runs is included (2 of 3,
  3 of 5; never fewer than 2); seen by fewer than a majority it is
  ``uncertain`` (a sorted list of plain component ids) and excluded
  from the netlist.
* Type: majority type among the runs containing the id wins.
  A tie with no majority (e.g. 1-1-1 in 3 runs) is uncertain and
  excluded.
* Value: voted on among the runs carrying the winning type only, so
  a minority type's value can never leak in. A value held by
  strictly more runs than any other value (and by >= 2 runs) wins;
  otherwise the median (``statistics.median``; for two distinct
  values that is their mean).
* Nodes / rating / colour: majority among the winning-type runs,
  ties broken by run order.

Other entry points (original brief)
------------------------------------
* ``verify_netlist(netlist, run_sim=None)`` -- alias of ``analyze``.
* ``design_best_of_3(goal, ...)`` -- alias of ``design``.
* ``run_image(image_bytes, ...)`` / ``run_goal(goal, ...)`` --
  generators yielding ``{stage, agent, message, elapsed_s}`` events
  (same shape as ``run_pipeline``). ``run_image`` transcribes the
  schematic ``N_CANDIDATES`` times in parallel
  (``ThreadPoolExecutor``; 3 by default, 5 in production via
  ``NETPROOF_AGENTS``) and merges with ``consensus_netlists``; a
  read failure never produces a guessed netlist, and fewer than 2
  successful reads raises.
  Unmodeled parts reported by any read are merged (deduped by id,
  first note wins, ids that land in the consensus netlist dropped)
  into ``PipelineResult.unmodeled`` (``[{id, type, note}]``) and the
  terminal event's ``data``. When >= 1 unmodeled part is present the
  final verdict is capped at WARN (PASS becomes WARN; STOP stays
  STOP; see ``_cap_verdict_for_unmodeled``).
  ``run_goal`` designs best-of-``N_CANDIDATES``, then repairs while
  STOP. Both return a ``PipelineResult`` as the generator's return
  value (``StopIteration.value``).
* ``check_netlist(netlist, ...) -> Report`` -- verify with no LLM
  call (server docstring shape; ``mock=True`` returns the ERC-only
  report with no simulation).

Injectable seams (for tests)
----------------------------
* Simulator: ``simulator=`` (or ``run_sim=``) on every verify path.
  An object with ``.simulate(netlist)`` wins, then ``.run(netlist)``,
  then a plain callable ``sim(netlist)``. The result may be an
  ``ir.Report`` (a fake standing in for spice + rules: returned
  as-is), a ``SimResult``, or a sim-result dict (passed to
  ``rules.verify``). ``None`` means the real path
  (``spice.simulate``); if ngspice
  is missing that raises ``RuntimeError`` with install
  instructions. A module-level override can also be installed with
  ``set_simulator`` (``None`` clears it); an explicit argument wins.
  A ``SimResult``/dict with ``success=False`` becomes a STOP
  ``SIM_FAILED`` finding, never a PASS.
* LLM: ``llm=`` on every LLM path, plus ``client=`` (an
  Anthropic-shaped client, passed through to the real ``llm.py``
  functions), ``read_fn=`` / ``design_fn=`` callables. The injected
  LLM may use the test seam (``llm.propose_repair(netlist, report)
  -> Netlist``, ``llm.design(prompt, n=3) -> list[Netlist]``) or the
  real ``llm.py`` seam (``repair(netlist, findings) ->
  (netlist, reasons)``, ``design(goal) -> Netlist``); both are
  detected and both return shapes are accepted. Any LLM failure is
  wrapped as ``RuntimeError("LLM call failed: ...")`` and propagates:
  it is never swallowed into a PASS verdict or a guessed netlist.
"""

from __future__ import annotations

import inspect
import os
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import llm as _llm_module
from . import rules as _rules
from . import spice as _spice
from .ir import (
    Component,
    ComponentType,
    Finding,
    Netlist,
    Report,
    Severity,
    Verdict,
)

__all__ = [
    "PipelineResult",
    "ConsensusResult",
    "DesignResult",
    "RepairResult",
    "CIRCUIT_DESIGNERS",
    "DESIGNERS",
    "analyze",
    "verify_netlist",
    "check_netlist",
    "circuit_score",
    "design",
    "design_best_of_3",
    "design_team",
    "repair_loop",
    "repair_netlist",
    "run_pipeline",
    "run_image",
    "run_goal",
    "consensus_netlists",
    "consensus",
    "build_consensus",
    "run_consensus",
    "merge_candidates",
    "canonicalize",
    "set_simulator",
    "finding_counts",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Transcriptions / designs per consensus / best-of-N round.
# Readers / designers per round. 3 locally and in tests; production can run a
# bigger team cheaply (Haiku), e.g. NETPROOF_AGENTS=5 in render.yaml.
N_CANDIDATES = max(1, int(os.environ.get("NETPROOF_AGENTS", "3") or 3))
#: Repair loop limits.
MAX_REPAIR_ROUNDS = 3
REPAIR_BUDGET_S = 90.0

#: Verdict rank for best-of-N: lower is better.
_VERDICT_RANK = {"PASS": 0, "WARN": 1, "STOP": 2}


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Outcome of one pipeline run (``run_pipeline`` / ``run_image`` /
    ``run_goal`` generator return value).

    ``report`` is the ``ir.Report`` for the final netlist; ``events``
    are the ``{stage, agent, message, elapsed_s}`` dicts yielded
    along the way; ``repairs`` collects every reason string the
    repair stage returned; ``rounds`` is the number of repair rounds
    actually run; ``unmodeled`` (``run_image`` only) lists the parts
    seen on the schematic that the circuit model cannot simulate,
    as ``[{id, type, note}]`` dicts, merged across the parallel
    reads — empty for ``run_pipeline`` / ``run_goal``; ``leaderboard``
    (``run_goal`` with ``team=True`` only) is the design-team ranking,
    a list of ``{"name", "score", "verdict"}`` dicts, winner first.
    ``objective`` (``run_goal`` only) is the resolved goal-mode
    objective (``"balanced"`` by default).
    """

    netlist: Netlist
    report: Report
    events: list[dict] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    rounds: int = 0
    unmodeled: list[dict] = field(default_factory=list)
    leaderboard: list[dict] = field(default_factory=list)
    objective: str = "balanced"


@dataclass
class ConsensusResult:
    """Result of :func:`consensus_netlists`.

    Unpacks as ``(netlist, uncertain)``. ``uncertain`` is a sorted
    list of plain component ids seen in fewer than a majority of the
    runs (or with no type majority). ``runs`` holds the coerced input
    runs.
    """

    netlist: Netlist
    uncertain: list[str] = field(default_factory=list)
    runs: list[Netlist] = field(default_factory=list)
    accepted: int = 0
    rejected: int = 0

    def __iter__(self) -> Iterable[Any]:
        return iter((self.netlist, self.uncertain))


@dataclass
class DesignResult:
    """Result of :func:`design` / :func:`design_best_of_3` /
    :func:`design_team`.

    Unpacks as ``(netlist, report)``. ``candidates`` and ``reports``
    are parallel lists in the order the designs were made/simulated.
    For :func:`design_team` only: ``scores`` is the tuner score per
    candidate (parallel to ``candidates``, informational — see
    :func:`design_team`); ``leaderboard`` is the pick-order ranking,
    a list of ``{"name", "score", "verdict"}`` dicts with the winner
    first. Both are empty for plain :func:`design`.
    """

    netlist: Netlist
    report: Report
    candidates: list[Netlist] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    leaderboard: list[dict] = field(default_factory=list)

    def __iter__(self) -> Iterable[Any]:
        return iter((self.netlist, self.report))


@dataclass
class RepairResult:
    """Result of :func:`repair_loop`.

    Unpacks as ``(netlist, history)``. ``report`` is the final
    ``ir.Report``; ``history`` is the list of reports, one per
    verification (first entry: the original netlist); ``rounds`` is
    the number of repair rounds actually run; ``reasons`` holds the
    LLM's per-round reason lists.
    """

    netlist: Netlist
    report: Report
    history: list[Report] = field(default_factory=list)
    rounds: int = 0
    reasons: list[list[str]] = field(default_factory=list)

    def __iter__(self) -> Iterable[Any]:
        return iter((self.netlist, self.history))


# ---------------------------------------------------------------------------
# Events / timing
# ---------------------------------------------------------------------------


class _Timer:
    """Monotonic clock for event ``elapsed_s`` values."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0


def _event(stage: str, agent: str, message: str, timer: _Timer) -> dict:
    return {
        "stage": stage,
        "agent": agent,
        "message": message,
        "elapsed_s": round(timer.elapsed(), 3),
    }


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def finding_counts(report: Any) -> dict[str, int]:
    """Count findings by severity: ``{"STOP": n, "WARN": n, "INFO": n}``.

    Accepts an ``ir.Report`` or a plain dict with a ``findings``
    list. Severity may be a string or a StrEnum.
    """
    counts = {"STOP": 0, "WARN": 0, "INFO": 0}
    if isinstance(report, dict):
        findings = report.get("findings") or []
    else:
        findings = getattr(report, "findings", None) or []
    for finding in findings:
        if isinstance(finding, dict):
            severity = finding.get("severity", "")
        else:
            severity = getattr(finding, "severity", "")
        severity = str(getattr(severity, "value", severity)).upper()
        if severity in counts:
            counts[severity] += 1
    return counts


def _verdict_str(report: Any) -> str:
    verdict = getattr(report, "verdict", "")
    return str(getattr(verdict, "value", verdict)).upper()


def _is_pass(report: Any) -> bool:
    return _verdict_str(report) == "PASS"


def _verdict_rank(report: Any) -> tuple[int, int, int]:
    """Sort key for "best verdict": verdict rank, then fewest STOP,
    then fewest WARN."""
    counts = finding_counts(report)
    return (
        _VERDICT_RANK.get(_verdict_str(report), 2),
        counts["STOP"],
        counts["WARN"],
    )


def _coerce_netlist(netlist: Any) -> Netlist:
    """Accept an ``ir.Netlist``, a netlist dict, or a bare component
    list; anything else raises loudly."""
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
        "expected an ir.Netlist (or a netlist dict / component list), "
        f"got {type(netlist).__name__}"
    )


def _accepts_kw(fn: Any, name: str) -> bool:
    """True when ``fn`` accepts keyword argument ``name`` (directly
    or via ``**kwargs``)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _max_positional(fn: Any) -> int:
    """How many positional arguments ``fn`` accepts (99 = variadic)."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return 99
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return 99
    return sum(
        1
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )


def _call_with_optional_client(fn: Any, *args: Any, client: Any = None, **kwargs: Any) -> Any:
    """Call ``fn``, passing ``client=`` only when its signature
    accepts it (real ``llm.py`` functions do; a plain fake taking
    ``(image_bytes)`` or ``(prompt, n=3)`` does not)."""
    if client is not None and _accepts_kw(fn, "client"):
        return fn(*args, client=client, **kwargs)
    return fn(*args, **kwargs)


def _with_uncertain(report: Report, extra: Any) -> Report:
    """Return ``report`` with ``extra`` notes appended to
    ``uncertain`` (a rebuild through the ``Report`` constructor, so
    findings stay sorted and the verdict stays derived)."""
    if not extra:
        return report
    return Report(
        verdict=report.verdict,
        findings=list(report.findings),
        node_voltages=dict(report.node_voltages),
        branch_currents=dict(report.branch_currents),
        power=dict(report.power),
        netlist_used=report.netlist_used,
        uncertain=[*report.uncertain, *[str(n) for n in extra]],
    )


def _cap_verdict_for_unmodeled(report: Report, unmodeled: list[dict]) -> Report:
    """Cap a ``run_image`` verdict at WARN when parts were unmodeled.

    ``ir.Report`` derives its verdict from its findings, so the cap
    is a WARN finding, not a verdict override: with >= 1 unmodeled
    part a PASS becomes WARN (the finding names every part), a WARN
    stays WARN, and a STOP stays STOP (a STOP finding still outranks
    the WARN). The finding is only added when it changes the verdict,
    so a STOP / WARN report keeps exactly the findings the rules
    produced. Applied at the end of :func:`run_image`, after the
    repair loop, so no repair round is ever spent on a verdict the
    repairer cannot fix (unmodeled parts are not in the netlist).
    """
    if not unmodeled or _verdict_str(report) != "PASS":
        return report
    parts = ", ".join(
        f"{p['id']} ({p['type']})" if p.get("type") else p["id"]
        for p in unmodeled
    )
    capped = Finding(
        severity=Severity.WARN,
        rule_id="UNMODELED_PART",
        component_ids=[p["id"] for p in unmodeled],
        message=(
            f"{parts} seen on the schematic but the circuit model "
            "cannot simulate them; verdict capped at WARN"
        ),
    )
    return Report(
        verdict=report.verdict,
        findings=[*report.findings, capped],
        node_voltages=dict(report.node_voltages),
        branch_currents=dict(report.branch_currents),
        power=dict(report.power),
        netlist_used=report.netlist_used,
        uncertain=list(report.uncertain),
    )


def _sim_success(sim_result: Any) -> tuple[Any, Any]:
    """``(success, error)`` for a sim result, ``(None, None)`` when
    the result carries no success flag (e.g. a plain dict of maps)."""
    if sim_result is None:
        return None, None
    if isinstance(sim_result, dict):
        return sim_result.get("success"), sim_result.get("error")
    return getattr(sim_result, "success", None), getattr(
        sim_result, "error", None
    )


# ---------------------------------------------------------------------------
# Simulator injection / real ngspice path
# ---------------------------------------------------------------------------

_SIMULATOR_OVERRIDE: Any = None


def set_simulator(simulator: Any) -> None:
    """Install a module-level simulator override (``None`` clears it).

    The server / tests use this to inject a fake simulator; an
    explicit ``simulator=`` / ``run_sim=`` argument to any pipeline
    function wins over the override.
    """
    global _SIMULATOR_OVERRIDE
    _SIMULATOR_OVERRIDE = simulator


def _resolve_simulator(simulator: Any, run_sim: Any) -> Any:
    if simulator is not None:
        return simulator
    if run_sim is not None:
        return run_sim
    return _SIMULATOR_OVERRIDE


def _simulate(netlist: Netlist, simulator: Any, run_sim: Any) -> Any:
    """Run one simulation, returning whatever ``rules.verify``
    accepts (an ``ir.Report``, a ``SimResult`` or a plain dict).
    ``None`` simulator means the real ngspice path. Raises (never
    returns a fake result) when ngspice is missing, times out, or
    the injected simulator raises.
    """
    sim = _resolve_simulator(simulator, run_sim)
    if sim is None:
        return _spice.simulate(netlist)
    for attr in ("simulate", "run"):
        method = getattr(sim, attr, None)
        if callable(method):
            return method(netlist)
    if callable(sim):
        return sim(netlist)
    raise TypeError(
        "simulator must be callable or expose a .simulate(netlist) / "
        f".run(netlist) method; got {type(sim).__name__}"
    )


# ---------------------------------------------------------------------------
# Verify: ERC, then simulate, then rules -> ir.Report
# ---------------------------------------------------------------------------


def analyze(
    netlist: Netlist,
    *,
    simulator: Any = None,
    run_sim: Any = None,
    llm: Any = None,
    extra_uncertain: list[str] | None = None,
) -> Report:
    """Simulate ``netlist`` once and check the rules; return the
    ``ir.Report``. The verdict comes from the simulator + rules,
    never from the LLM (the ``llm`` argument is accepted for
    call-site compatibility and ignored).

    When pre-simulation (ERC) checks already contain a STOP finding
    the simulation is skipped and the report is built from the ERC
    findings alone. An injected simulator may return an ``ir.Report``
    directly (a fake standing in for spice + rules); it is returned
    as-is. A sim result with ``success=False`` becomes a STOP
    ``SIM_FAILED`` finding, never a PASS.
    """
    netlist = _coerce_netlist(netlist)
    pre = _rules.pre_sim_checks(netlist)
    if any(str(getattr(f, "severity", "")) == "STOP" for f in pre):
        return _with_uncertain(_rules.verify(netlist, None), extra_uncertain)
    sim_result = _simulate(netlist, simulator, run_sim)
    if isinstance(sim_result, Report):
        return _with_uncertain(sim_result, extra_uncertain)
    report = _rules.verify(netlist, sim_result)
    success, error = _sim_success(sim_result)
    if success is False:
        failed = Finding(
            severity=Severity.STOP,
            rule_id="SIM_FAILED",
            component_ids=[],
            message=(
                "ngspice .op simulation failed: "
                f"{error or 'no error reported'}; "
                "no operating point could be computed."
            ),
        )
        report = Report(
            verdict=report.verdict,
            findings=[failed, *report.findings],
            node_voltages=dict(report.node_voltages),
            branch_currents=dict(report.branch_currents),
            power=dict(report.power),
            netlist_used=report.netlist_used,
            uncertain=list(report.uncertain),
        )
    return _with_uncertain(report, extra_uncertain)


def verify_netlist(
    netlist: Netlist,
    run_sim: Any = None,
    simulator: Any = None,
    llm: Any = None,
    extra_uncertain: list[str] | None = None,
) -> Report:
    """Alias of :func:`analyze` (original brief name):
    ``verify_netlist(netlist, run_sim=None) -> Report``."""
    return analyze(
        netlist,
        simulator=simulator,
        run_sim=run_sim,
        llm=llm,
        extra_uncertain=extra_uncertain,
    )


def check_netlist(
    netlist: Netlist,
    simulator: Any = None,
    run_sim: Any = None,
    mock: bool = False,
    **_ignored: Any,
) -> Report:
    """Verify a netlist with no LLM call. ``mock=True`` returns the
    ERC-only report with no simulation (mock check)."""
    netlist = _coerce_netlist(netlist)
    if mock:
        return _rules.verify(netlist, None)
    return analyze(netlist, simulator=simulator, run_sim=run_sim)


# ---------------------------------------------------------------------------
# LLM seams (test seam + real llm.py seam; failures raise loudly)
# ---------------------------------------------------------------------------


def _llm_error(exc: Exception) -> RuntimeError:
    """Wrap any LLM failure so its message always names the failure
    (``LLM call failed: ...``). Never swallowed, never a fallback."""
    return RuntimeError(f"LLM call failed: {exc}")


def _unpack_repair_result(result: Any) -> tuple[Netlist, list[str]]:
    """Accept a repaired ``Netlist``, a ``(netlist, reasons)`` tuple
    (real ``llm.repair``), or a dict with a ``netlist`` key."""
    if isinstance(result, tuple) and len(result) == 2:
        repaired, reasons = result
        return _coerce_netlist(repaired), [str(r) for r in (reasons or [])]
    if isinstance(result, dict) and "netlist" in result:
        return (
            _coerce_netlist(result["netlist"]),
            [str(r) for r in (result.get("reasons") or [])],
        )
    return _coerce_netlist(result), []


def _call_repair(
    llm_obj: Any, netlist: Netlist, report: Report, client: Any = None
) -> tuple[Netlist, list[str]]:
    """Ask the LLM for a repair of ``netlist`` against ``report``.

    Test seam first: ``llm.propose_repair(netlist, report)`` (or
    ``suggest_repair``). Then the real ``llm.py`` seam:
    ``repair(netlist, findings) -> (netlist, reasons)``. Returns
    ``(repaired_netlist, reasons)``. Any failure raises
    ``RuntimeError("LLM call failed: ...")``.
    """
    try:
        if llm_obj is None:
            result = _call_with_optional_client(
                _llm_module.repair, netlist, list(report.findings), client=client
            )
            return _unpack_repair_result(result)
        for name in ("propose_repair", "suggest_repair"):
            method = getattr(llm_obj, name, None)
            if callable(method):
                if _max_positional(method) >= 2:
                    result = method(netlist, report)
                else:
                    result = method(netlist)
                return _unpack_repair_result(result)
        method = getattr(llm_obj, "repair", None)
        if callable(method):
            result = _call_with_optional_client(
                method, netlist, list(report.findings), client=client
            )
            return _unpack_repair_result(result)
        if callable(llm_obj):
            return _unpack_repair_result(llm_obj(netlist, report))
        raise RuntimeError(
            "LLM call failed: injected llm has no propose_repair / "
            "repair method"
        )
    except RuntimeError as exc:
        if str(exc).startswith("LLM call failed"):
            raise
        raise _llm_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - any LLM failure is visible
        raise _llm_error(exc) from exc


def _call_design(
    llm_obj: Any, prompt: str, n: int, client: Any = None
) -> list[Netlist]:
    """Ask the LLM for ``n`` candidate netlists for ``prompt``.

    Test seam: ``llm.design(prompt, n=3) -> list[Netlist]`` (one
    call). Real ``llm.py`` seam: ``design(goal, client=None) ->
    Netlist`` (one netlist per call, called ``n`` times). Any failure
    raises ``RuntimeError("LLM call failed: ...")``.
    """
    try:
        if llm_obj is None:
            return [
                _coerce_netlist(
                    _call_with_optional_client(
                        _llm_module.design, prompt, client=client
                    )
                )
                for _ in range(n)
            ]
        method = None
        for name in ("design", "generate", "generate_candidates"):
            candidate = getattr(llm_obj, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            if callable(llm_obj):
                method = llm_obj
            else:
                raise RuntimeError(
                    "LLM call failed: injected llm has no design method"
                )
        if _accepts_kw(method, "n"):
            first = _call_with_optional_client(
                method, prompt, n=n, client=client
            )
            if isinstance(first, (list, tuple)):
                return [_coerce_netlist(c) for c in first]
            if isinstance(first, Netlist):
                rest = [
                    _call_with_optional_client(
                        method, prompt, n=n, client=client
                    )
                    for _ in range(n - 1)
                ]
                return [first, *[_coerce_netlist(c) for c in rest]]
            raise RuntimeError(
                "LLM call failed: design returned "
                f"{type(first).__name__}, expected Netlist or "
                "list[Netlist]"
            )
        return [
            _coerce_netlist(
                _call_with_optional_client(method, prompt, client=client)
            )
            for _ in range(n)
        ]
    except RuntimeError as exc:
        if str(exc).startswith("LLM call failed"):
            raise
        raise _llm_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - any LLM failure is visible
        raise _llm_error(exc) from exc


def _normalize_unmodeled(entry: Any) -> dict | None:
    """Normalize one unmodeled part (dict, ``llm.UnmodeledPart``, or
    any object with ``id`` / ``type`` / ``note`` attributes) to a
    plain ``{"id", "type", "note"}`` dict. Returns ``None`` when the
    entry carries no usable id (it is dropped, never invented)."""
    if isinstance(entry, dict):
        part_id = str(entry.get("id", "") or "").strip()
        part_type = str(entry.get("type", "") or "").strip()
        note = str(entry.get("note", "") or "")
    else:
        part_id = str(getattr(entry, "id", "") or "").strip()
        part_type = str(getattr(entry, "type", "") or "").strip()
        note = str(getattr(entry, "note", "") or "")
    if not part_id:
        return None
    return {"id": part_id, "type": part_type, "note": note}


def _split_read_result(raw: Any) -> tuple[Netlist, list[dict]]:
    """Split one schematic-read result into ``(netlist, unmodeled)``.

    Accepts every shape a read can return: a ``(netlist, unmodeled)``
    tuple, a netlist dict carrying an ``"unmodeled"`` key, or a plain
    ``Netlist`` (including ``llm.SchematicNetlist``, whose
    ``.unmodeled`` attribute is read). A plain ``Netlist`` with no
    unmodeled information yields ``[]`` — reads from older fakes keep
    working unchanged.
    """
    if isinstance(raw, tuple) and len(raw) == 2:
        netlist, unmodeled = raw
        parts = [
            p
            for p in (
                _normalize_unmodeled(e) for e in (unmodeled or [])
            )
            if p is not None
        ]
        return _coerce_netlist(netlist), parts
    if isinstance(raw, dict) and "unmodeled" in raw:
        parts = [
            p
            for p in (
                _normalize_unmodeled(e) for e in (raw.get("unmodeled") or [])
            )
            if p is not None
        ]
        return _coerce_netlist(raw), parts
    netlist = _coerce_netlist(raw)
    parts = [
        p
        for p in (
            _normalize_unmodeled(e)
            for e in (getattr(netlist, "unmodeled", None) or [])
        )
        if p is not None
    ]
    return netlist, parts


def _call_read(
    llm_obj: Any,
    read_fn: Any,
    image_bytes: bytes,
    client: Any = None,
) -> tuple[Netlist, list[dict]]:
    """Transcribe one schematic image with the LLM. ``read_fn`` (a
    plain callable) wins, then ``llm.read_schematic``, then the real
    ``llm.read_schematic``. Returns ``(netlist, unmodeled)`` where
    ``unmodeled`` is the list of ``{id, type, note}`` dicts for parts
    the circuit model cannot simulate (``[]`` when every part was
    modeled). Failures propagate to the caller, which wraps them as
    ``LLM call failed``."""
    if read_fn is not None:
        return _split_read_result(
            _call_with_optional_client(read_fn, image_bytes, client=client)
        )
    if llm_obj is not None:
        method = getattr(llm_obj, "read_schematic", None)
        if callable(method):
            return _split_read_result(
                _call_with_optional_client(method, image_bytes, client=client)
            )
        raise RuntimeError(
            "LLM call failed: injected llm has no read_schematic method"
        )
    return _split_read_result(
        _call_with_optional_client(
            _llm_module.read_schematic, image_bytes, client=client
        )
    )


# ---------------------------------------------------------------------------
# Consensus over runs (test_consensus.py reference semantics)
# ---------------------------------------------------------------------------


def _canonicalise_run(
    netlist: Netlist,
) -> tuple[Netlist, dict[str, str], dict[str, str]]:
    """Rename nodes to canonical labels, keeping ``"0"`` as ground.

    A node's canonical label comes from the sorted tuple of
    component ids incident to it (ids only, not types/values), so two
    runs of the same graph under different labels canonicalise to
    the same netlist. Returns ``(canonical_netlist, original ->
    canonical, canonical -> original)``.
    """
    incident: dict[str, list[str]] = {}
    for comp in netlist.components:
        for node in comp.nodes:
            incident.setdefault(node, []).append(comp.id)
    signatures = {
        node: tuple(sorted(ids)) for node, ids in incident.items()
    }
    non_ground = sorted(
        (n for n in netlist.node_names() if n != "0"),
        key=lambda n: (signatures[n], n),
    )
    mapping: dict[str, str] = {"0": "0"}
    for i, node in enumerate(non_ground, start=1):
        mapping[node] = f"c{i}"
    inverse = {canon: orig for orig, canon in mapping.items()}
    canon_components = [
        Component(
            id=comp.id,
            type=comp.type,
            value=comp.value,
            nodes=[mapping[n] for n in comp.nodes],
            rating_watts=comp.rating_watts,
            color=comp.color,
        )
        for comp in netlist.components
    ]
    return Netlist(components=canon_components, title=netlist.title), mapping, inverse


def canonicalize(netlist: Netlist) -> Netlist:
    """Return ``netlist`` with nodes renamed to canonical labels
    (ground stays ``"0"``). Component ids, types, values, ratings
    and colours are preserved."""
    canon, _, _ = _canonicalise_run(_coerce_netlist(netlist))
    return canon


def _majority_or_median(values: list[float]) -> float:
    """Most-common value wins if it strictly beats every other value
    and is held by >= 2 runs. Otherwise the median (for two distinct
    values, their mean)."""
    counts = Counter(values)
    ranked = counts.most_common()
    if (
        len(ranked) > 1
        and ranked[0][1] > ranked[1][1]
        and ranked[0][1] >= 2
    ):
        return ranked[0][0]
    if len(ranked) == 1:
        return ranked[0][0]
    return float(statistics.median(values))


def consensus_netlists(runs: list[Netlist] | Netlist) -> ConsensusResult:
    """Merge ``runs`` (``N_CANDIDATES`` transcriptions: 3 by default,
    5 in production via ``NETPROOF_AGENTS``) into one netlist.

    Returns a ``ConsensusResult`` (unpacks as ``(netlist,
    uncertain)``). Per component id: seen by a majority of the runs
    it is included with the majority type and the majority/median
    value voted among the winning-type runs only; seen by fewer than
    a majority, or with no type majority (e.g. 1-1-1), it is
    ``uncertain`` and excluded. Output nodes use the first run's
    original labels.
    """
    if isinstance(runs, Netlist):
        runs = [runs]
    runs = [_coerce_netlist(r) for r in runs]
    if not runs:
        raise ValueError("consensus needs at least one run")
    canon_runs = [_canonicalise_run(run) for run in runs]

    ids: list[str] = []
    for canon, _, _ in canon_runs:
        for comp in canon.components:
            if comp.id not in ids:
                ids.append(comp.id)

    accepted: list[Component] = []
    uncertain: list[str] = []
    for comp_id in ids:
        present = [
            (canon, inverse)
            for canon, _, inverse in canon_runs
            if canon.get(comp_id) is not None
        ]
        # A part must be seen by a majority of the readers (2 of 3, 3 of 5),
        # and never fewer than 2.
        if len(present) < max(2, len(runs) // 2 + 1):
            uncertain.append(comp_id)
            continue
        type_counts = Counter(
            canon.get(comp_id).type  # type: ignore[union-attr]
            for canon, _ in present
        )
        ranked_types = type_counts.most_common()
        if len(ranked_types) > 1 and ranked_types[0][1] == ranked_types[1][1]:
            uncertain.append(comp_id)  # no type majority (e.g. 1-1-1)
            continue
        winning_type = ranked_types[0][0]
        majority = [
            (canon, inverse)
            for canon, inverse in present
            if canon.get(comp_id).type == winning_type  # type: ignore[union-attr]
        ]
        majority_comps = [
            canon.get(comp_id)  # type: ignore[union-attr]
            for canon, _ in majority
        ]
        value = _majority_or_median([c.value for c in majority_comps])  # type: ignore[union-attr]
        node_tuples = Counter(
            tuple(c.nodes)  # type: ignore[union-attr]
            for c in majority_comps
        )
        canon_nodes = node_tuples.most_common(1)[0][0]
        # Report nodes in the first winning-type run's original labels.
        inverse0 = majority[0][1]
        out_nodes = [inverse0.get(n, n) for n in canon_nodes]
        rating = Counter(
            c.rating_watts  # type: ignore[union-attr]
            for c in majority_comps
        ).most_common(1)[0][0]
        color = Counter(
            c.color  # type: ignore[union-attr]
            for c in majority_comps
        ).most_common(1)[0][0]
        accepted.append(
            Component(
                id=comp_id,
                type=winning_type,
                value=value,
                nodes=list(out_nodes),
                rating_watts=rating,
                color=color,
            )
        )
    accepted.sort(key=lambda c: c.id)
    return ConsensusResult(
        netlist=Netlist(components=accepted, title="consensus"),
        uncertain=sorted(uncertain),
        runs=list(runs),
        accepted=len(accepted),
        rejected=len(uncertain),
    )


#: Aliases (test_consensus.py tries these names in order).
consensus = consensus_netlists
build_consensus = consensus_netlists
run_consensus = consensus_netlists
merge_candidates = consensus_netlists


# ---------------------------------------------------------------------------
# Design: best-of-N
# ---------------------------------------------------------------------------


def design(
    prompt: str,
    *,
    llm: Any = None,
    simulator: Any = None,
    run_sim: Any = None,
    n: int = 3,
    client: Any = None,
    **_ignored: Any,
) -> DesignResult:
    """Design a circuit for ``prompt``: ask the LLM for ``n``
    candidate netlists, simulate + verify every candidate, return
    the one with the best verdict (PASS beats WARN beats STOP; ties:
    fewest STOP, then fewest WARN, then earliest). Never just
    returns the first candidate. Any LLM failure raises.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("design requires a non-empty goal string")
    candidates = _call_design(llm, prompt, n, client)
    if not candidates:
        raise RuntimeError("LLM call failed: design returned no candidates")
    reports = [
        analyze(c, simulator=simulator, run_sim=run_sim) for c in candidates
    ]
    best = min(
        range(len(candidates)),
        key=lambda i: (_verdict_rank(reports[i]), i),
    )
    return DesignResult(
        netlist=candidates[best],
        report=reports[best],
        candidates=list(candidates),
        reports=list(reports),
    )


def design_best_of_3(
    goal: str,
    *,
    llm: Any = None,
    client: Any = None,
    simulator: Any = None,
    run_sim: Any = None,
    n: int = 3,
) -> DesignResult:
    """Alias of :func:`design` (original brief name):
    ``design_best_of_3(goal)`` -- 3 candidates, each verified, keep
    the best verdict."""
    return design(
        goal,
        llm=llm,
        client=client,
        simulator=simulator,
        run_sim=run_sim,
        n=n,
    )


# ---------------------------------------------------------------------------
# Design team: one strategist per designer (bench #8)
# ---------------------------------------------------------------------------
#
# Circuit goal mode as a real team, mirroring
# ``design_ai.propose_team`` (fiber/optics): each designer gets the
# same goal plus a different strategy, proposes ONE candidate, and
# every candidate is simulated + verified by deterministic code; the
# simulator + rules decide, the model never does. Per-designer
# ``agent`` / ``state`` / ``score`` events (``Designer <Name>``,
# ``Judge``, ``Physics``) give the dock one lane per designer.
#
# Strategy names are single ``\\w+`` words on purpose: the dock's
# Judge-winner regex is ``/Designer (\\w+)/``, so ``Low-power`` would
# capture only ``Low`` and never match a lane. The low-power
# strategist is therefore named ``Efficient``, not ``Low-power``.
#
# TUNER-SCORE MISMATCH (documented, not fixed here): the winner is
# picked by verdict rank (``_verdict_rank``: PASS beats WARN beats
# STOP; ties: fewest STOP, then fewest WARN, then earliest) — exactly
# like :func:`design`. The per-designer ``score`` is the tuner score
# (``tuner._circuit_score``: ``95 - 25*STOP - 5*WARN - 40*LED-miss``,
# default target balanced / 12 mA; goal-mode ``prefs`` choose another
# target via :func:`_resolve_goal_prefs`). The two scores can and do
# disagree: a PASS-but-dim candidate (5 mA LED) scores 71.7 while a
# WARN candidate whose LED sits exactly on target scores ~90.0 — yet
# the picker still takes the PASS candidate, and the tuner score is
# shown, not hidden.
# The tuner score is informational only: it never picks the winner,
# and NO tuning runs inside the pipeline (tuning stays the separate
# manual ``POST /tune/circuit`` step on the final netlist, so a tuned
# STOP can never slip out of ``run_goal`` unverified and a repair can
# never silently undo tuned values).


#: Circuit designer strategies, mirroring ``design_ai.DESIGNERS``.
#: ``(name, strategy prompt)``; the name is a single word (see above).
CIRCUIT_DESIGNERS: list[tuple[str, str]] = [
    (
        "Faithful",
        "Strategy: match the goal as faithfully as possible, "
        "using standard real component values.",
    ),
    (
        "Simple",
        "Strategy: use the fewest parts that still meet the goal.",
    ),
    (
        "Safe",
        "Strategy: safety first - keep every part well inside its "
        "ratings, with margin on resistor power and LED current.",
    ),
    (
        "Efficient",
        "Strategy: use as little power as safely possible while "
        "still meeting the goal; keep currents low.",
    ),
]
#: Alias, so callers can write ``pipeline.DESIGNERS`` exactly like
#: ``design_ai.DESIGNERS``.
DESIGNERS = CIRCUIT_DESIGNERS


def _tuner_report_dict(report: Report, netlist: Netlist) -> dict:
    """Shape an ``ir.Report`` + netlist into the dict
    ``tuner._circuit_score`` reads: ``findings`` as ``{"severity"}``
    dicts and ``netlist`` rows carrying each component's branch
    current (``_led_currents`` reads the LED rows). Branch-current
    keys are matched case-insensitively (ngspice lowercases them).
    """
    currents = {
        str(k).lower(): v for k, v in (report.branch_currents or {}).items()
    }
    rows = [
        {
            "id": comp.id,
            "type": str(
                getattr(comp.type, "value", comp.type)
            ),
            "current": currents.get(comp.id.lower()),
        }
        for comp in netlist.components
    ]
    findings = [
        {
            "severity": str(
                getattr(f.severity, "value", f.severity)
            ).upper(),
            "rule_id": f.rule_id,
        }
        for f in report.findings
    ]
    return {"findings": findings, "netlist": rows}


def circuit_score(
    report: Report, netlist: Netlist, target_ma: float | None = None
) -> float:
    """Tuner score for one verified candidate (informational only).

    ``tuner._circuit_score`` on the ``(report, netlist)`` pair:
    ``95 - 25*STOP - 5*WARN - 40 * mean(|I_LED - target| / target)``.
    ``target_ma`` defaults to the tuner's ``balanced`` target
    (12 mA); goal mode passes its resolved prefs target here (see
    :func:`_resolve_goal_prefs`). With no LEDs the miss term is 0.
    The score is NOT clamped and is NOT the team picker — see the
    mismatch note above :data:`CIRCUIT_DESIGNERS`.
    """
    from . import tuner as _tuner

    target_a = (
        float(target_ma) / 1000.0
        if target_ma is not None
        else _tuner.CIRCUIT_TARGET_MA["balanced"] / 1000.0
    )
    return _tuner._circuit_score(
        _tuner_report_dict(report, netlist), target_a
    )


def _resolve_goal_prefs(
    prefs: dict | None, target_ma: float | None = None
) -> tuple[str, float]:
    """Resolve goal-mode prefs to ``(objective, target_ma)``.

    ``prefs`` may carry ``objective`` (one of
    ``tuner.CIRCUIT_TARGET_MA``: ``balanced`` / ``bright`` /
    ``battery`` / ``cheap``) and/or an explicit ``target_ma`` (LED
    target current in mA), which wins over the objective's target.
    An explicit ``target_ma`` argument wins over ``prefs``. An
    unknown objective or a non-positive target raises ``ValueError``
    (the API surfaces it as HTTP 400). ``None`` prefs resolve to
    ``("balanced", 12.0)``: the tuner-score default, so goal mode
    without prefs behaves exactly as before.
    """
    from . import tuner as _tuner

    prefs = prefs or {}
    objective = prefs.get("objective") or "balanced"
    targets = _tuner.CIRCUIT_TARGET_MA
    if objective not in targets:
        raise ValueError(
            f"unknown objective {objective!r}: expected one of "
            + ", ".join(sorted(targets))
        )
    resolved = target_ma if target_ma is not None else prefs.get("target_ma")
    if resolved is None:
        resolved = targets[objective]
    try:
        resolved = float(resolved)
    except (TypeError, ValueError):
        raise ValueError(
            f"target_ma must be a number, got {resolved!r}"
        ) from None
    if resolved <= 0:
        raise ValueError(f"target_ma must be positive, got {resolved:g}")
    return objective, resolved


def _call_design_single(
    llm_obj: Any, prompt: str, client: Any = None
) -> Netlist:
    """Ask the LLM for ONE candidate netlist for ``prompt`` (one
    designer in :func:`design_team`).

    Test seam: ``llm.design(prompt, n=1) -> list[Netlist]`` (or a
    single ``Netlist``) — the first element is the designer's
    candidate. Real ``llm.py`` seam: ``design(goal, client=None) ->
    Netlist`` (one netlist per call). Any failure raises
    ``RuntimeError("LLM call failed: ...")``.
    """
    try:
        if llm_obj is None:
            return _coerce_netlist(
                _call_with_optional_client(
                    _llm_module.design, prompt, client=client
                )
            )
        method = None
        for name in ("design", "generate", "generate_candidates"):
            candidate = getattr(llm_obj, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            if callable(llm_obj):
                method = llm_obj
            else:
                raise RuntimeError(
                    "LLM call failed: injected llm has no design method"
                )
        if _accepts_kw(method, "n"):
            result = _call_with_optional_client(
                method, prompt, n=1, client=client
            )
        else:
            result = _call_with_optional_client(
                method, prompt, client=client
            )
        if isinstance(result, (list, tuple)):
            if not result:
                raise RuntimeError(
                    "LLM call failed: design returned no candidates"
                )
            return _coerce_netlist(result[0])
        return _coerce_netlist(result)
    except RuntimeError as exc:
        if str(exc).startswith("LLM call failed"):
            raise
        raise _llm_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - any LLM failure is visible
        raise _llm_error(exc) from exc


def _safe_team_call(fn: Any, arg: Any, emit: Any, agent: str) -> Any:
    """Run one designer; on failure emit a ``failed`` event and
    return the exception (mirrors ``design_ai._safe``). One designer
    failing never sinks the team."""
    try:
        return fn(arg)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        emit(
            {
                "stage": "agent",
                "agent": agent,
                "state": "failed",
                "message": str(exc)[:160],
            }
        )
        return exc


def design_team(
    prompt: str,
    *,
    llm: Any = None,
    simulator: Any = None,
    run_sim: Any = None,
    client: Any = None,
    designers: int = 4,
    on_event: Any = None,
    target_ma: float | None = None,
    prefs: dict | None = None,
) -> DesignResult:
    """Design a circuit as a team of strategists.

    Runs the first ``designers`` entries of
    :data:`CIRCUIT_DESIGNERS` in parallel (``ThreadPoolExecutor``,
    like ``design_ai.propose_team``): each designer gets the goal
    plus its strategy text, proposes one candidate, and every
    candidate is simulated + verified by deterministic code. The
    winner is picked by verdict rank (``_verdict_rank``; ties:
    earliest designer) — the tuner score is emitted per designer
    for the dock but never picks the winner (see the mismatch note
    above :data:`CIRCUIT_DESIGNERS`). No tuning runs here.

    ``prefs`` is an optional goal-mode preferences dict (see
    :func:`_resolve_goal_prefs`): ``objective`` in
    ``balanced`` / ``bright`` / ``battery`` / ``cheap`` and/or an
    explicit ``target_ma`` (LED target in mA). It is honoured two
    ways: the resolved target feeds the per-designer tuner score,
    and a one-line preference note is appended to each designer's
    prompt so the LLM can aim at it. An explicit ``target_ma``
    argument wins over ``prefs``. With no ``prefs`` the target is
    the ``balanced`` 12 mA default and no note is added, i.e. the
    previous behaviour exactly.

    ``on_event(dict)`` receives the team events in the
    ``propose_team`` dialect, each with ``elapsed_s`` added:
    ``team`` announcement, per-designer ``Designer <Name>`` working
    (``stage: "agent"``) then done (``stage: "verify"``,
    ``state: "done"``, ``calls``, ``physics: 1``, numeric ``score``)
    or failed, ``Judge`` (``state: "done"``, winner's score,
    ``"Winner: Designer <Name> (...)"``) and ``Physics`` (``state:
    "done"``, ``"Verdict <V>: ..."``). A failing designer is
    reported, not fatal; if every designer fails, raises
    ``RuntimeError("LLM call failed: ...")``.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("design requires a non-empty goal string")
    timer = _Timer()
    objective, resolved_target = _resolve_goal_prefs(prefs, target_ma)
    # Only when prefs were actually given: the note must not change
    # the default prompt at all.
    pref_note = (
        f"\n\nPreference: {objective} objective — aim for an LED "
        f"current near {resolved_target:g} mA."
        if prefs
        else ""
    )
    raw_emit = on_event or (lambda e: None)

    def emit(event: dict) -> None:
        raw_emit({**event, "elapsed_s": round(timer.elapsed(), 3)})

    picked = CIRCUIT_DESIGNERS[
        : max(1, min(int(designers), len(CIRCUIT_DESIGNERS)))
    ]
    emit(
        {
            "stage": "model",
            "agent": "team",
            "message": f"{len(picked)} designers proposing in parallel: "
            + ", ".join(name for name, _ in picked),
        }
    )
    emit(
        {
            "stage": "verify",
            "agent": "Physics",
            "state": "working",
            "message": "Simulating each candidate\u2026",
        }
    )

    def _run_designer(spec: tuple[str, str]) -> dict:
        name, strategy = spec
        agent = f"Designer {name}"
        emit(
            {
                "stage": "agent",
                "agent": agent,
                "state": "working",
                "message": strategy.replace("Strategy: ", ""),
            }
        )
        candidate = _call_design_single(
            llm, f"{prompt}\n\n{strategy}{pref_note}", client
        )
        report = analyze(candidate, simulator=simulator, run_sim=run_sim)
        score = circuit_score(report, candidate, target_ma=resolved_target)
        emit(
            {
                "stage": "verify",
                "agent": agent,
                "state": "done",
                "calls": 1,
                "physics": 1,
                "score": score,
                "message": f"{len(candidate.components)} components "
                f"\u2192 {_verdict_str(report)}, score {score:.1f}",
            }
        )
        return {
            "name": name,
            "netlist": candidate,
            "report": report,
            "score": score,
        }

    with ThreadPoolExecutor(max_workers=len(picked)) as pool:
        results = [
            r
            for r in pool.map(
                lambda s: _safe_team_call(
                    _run_designer, s, emit, f"Designer {s[0]}"
                ),
                picked,
            )
            if isinstance(r, dict)
        ]
    if not results:
        raise RuntimeError(
            "LLM call failed: every designer failed to return a "
            "valid design."
        )
    # Winner by verdict rank, ties to the earliest designer (same
    # rule as design()); the tuner score never reorders the pick.
    order = {name: i for i, (name, _) in enumerate(picked)}
    ranked = sorted(
        results,
        key=lambda r: (_verdict_rank(r["report"]), order[r["name"]]),
    )
    best = ranked[0]
    board = ", ".join(
        f"{r['name']} {r['score']:.1f}" for r in ranked
    )
    emit(
        {
            "stage": "verify",
            "agent": "Judge",
            "state": "done",
            "score": best["score"],
            "message": f"Winner: Designer {best['name']} ({board})",
        }
    )
    emit(
        {
            "stage": "verify",
            "agent": "Physics",
            "state": "done",
            "message": f"Verdict {_verdict_str(best['report'])}: "
            + (
                ", ".join(f.rule_id for f in best["report"].findings)
                or "no rules fired"
            ),
        }
    )
    # Candidates/reports/scores stay in picked (designer) order, but a
    # failed designer has no candidate at all — it is simply absent.
    in_order = sorted(results, key=lambda r: order[r["name"]])
    candidates = [r["netlist"] for r in in_order]
    reports = [r["report"] for r in in_order]
    scores = [r["score"] for r in in_order]
    leaderboard = [
        {
            "name": r["name"],
            "score": r["score"],
            "verdict": _verdict_str(r["report"]),
        }
        for r in ranked
    ]
    return DesignResult(
        netlist=best["netlist"],
        report=best["report"],
        candidates=candidates,
        reports=reports,
        scores=scores,
        leaderboard=leaderboard,
    )


# ---------------------------------------------------------------------------
# Repair loop (max 3 rounds / 90 s)
# ---------------------------------------------------------------------------


def _repair_loop_gen(
    netlist: Netlist,
    report: Report,
    *,
    simulator: Any,
    run_sim: Any,
    llm_obj: Any,
    client: Any,
    max_rounds: int,
    deadline_s: float,
    timer: _Timer,
) -> Any:
    """Repair generator: yields ``{stage, agent, message, elapsed_s}``
    events per round and returns a ``RepairResult``. The caller drives
    it (``repair_loop`` consumes it silently; the ``run_*``
    generators yield its events through)."""
    history = [report]
    reasons: list[list[str]] = []
    rounds = 0
    while not _is_pass(report) and rounds < max_rounds:
        if timer.elapsed() >= deadline_s:
            yield _event(
                "repair",
                "repairer",
                f"repair budget of {deadline_s:g} s reached after "
                f"{rounds} round(s); stopping with verdict "
                f"{_verdict_str(report)}",
                timer,
            )
            break
        rounds += 1
        yield _event(
            "repair",
            "repairer",
            f"repair round {rounds}/{max_rounds}: "
            f"{len(report.findings)} finding(s), worst is "
            f"{report.findings[0].rule_id if report.findings else 'none'}",
            timer,
        )
        repaired, round_reasons = _call_repair(
            llm_obj, netlist, report, client
        )
        reasons.append(round_reasons)
        netlist = repaired
        report = analyze(netlist, simulator=simulator, run_sim=run_sim)
        history.append(report)
        yield _event(
            "repair",
            "repairer",
            f"repair round {rounds} done: verdict "
            f"{_verdict_str(report)}"
            + (f" ({len(round_reasons)} change(s))" if round_reasons else " (no changes)"),
            timer,
        )
    return RepairResult(
        netlist=netlist,
        report=report,
        history=history,
        rounds=rounds,
        reasons=reasons,
    )


def _drive_generator(gen: Any) -> Any:
    """Consume a generator that yields events and returns a result;
    return the generator's return value (``StopIteration.value``)."""
    try:
        while True:
            next(gen)
    except StopIteration as done:
        return done.value


def repair_loop(
    netlist: Netlist,
    *,
    simulator: Any = None,
    run_sim: Any = None,
    llm: Any = None,
    client: Any = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    deadline_s: float = REPAIR_BUDGET_S,
    **_ignored: Any,
) -> RepairResult:
    """Verify ``netlist``, then repair while the verdict is not PASS,
    up to ``max_rounds`` rounds (default 3) and ``deadline_s``
    seconds (default 90). One round = one repair proposal + one
    re-simulation; the initial check is not a round. Stops early on
    PASS. Any LLM failure raises. Returns a ``RepairResult`` whose
    ``.report`` is the final report and ``.history`` is the list of
    reports, one per verification.
    """
    netlist = _coerce_netlist(netlist)
    timer = _Timer()
    report = analyze(netlist, simulator=simulator, run_sim=run_sim)
    return _drive_generator(
        _repair_loop_gen(
            netlist,
            report,
            simulator=simulator,
            run_sim=run_sim,
            llm_obj=llm,
            client=client,
            max_rounds=max_rounds,
            deadline_s=deadline_s,
            timer=timer,
        )
    )


def repair_netlist(
    netlist: Netlist,
    *,
    simulator: Any = None,
    run_sim: Any = None,
    llm: Any = None,
    client: Any = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    deadline_s: float = REPAIR_BUDGET_S,
    **_ignored: Any,
) -> RepairResult:
    """Alias of :func:`repair_loop` (previous pipeline name)."""
    return repair_loop(
        netlist,
        simulator=simulator,
        run_sim=run_sim,
        llm=llm,
        client=client,
        max_rounds=max_rounds,
        deadline_s=deadline_s,
    )


# ---------------------------------------------------------------------------
# Event generators: run_pipeline / run_image / run_goal
# ---------------------------------------------------------------------------


def _findings_text(report: Report) -> str:
    return ", ".join(f.rule_id for f in report.findings) or "none"


def _drive_repair_events(gen: Any, events: list[dict]) -> Any:
    """Drive a repair generator, forwarding each event it yields to
    both the ``events`` list and the caller's generator (via
    ``yield from`` in the caller, which also captures this helper's
    return value: the ``RepairResult``).
    """
    try:
        while True:
            event = next(gen)
            events.append(event)
            yield event
    except StopIteration as done:
        return done.value


def run_pipeline(
    netlist: Netlist,
    *,
    simulator: Any = None,
    run_sim: Any = None,
    llm: Any = None,
    client: Any = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    deadline_s: float = REPAIR_BUDGET_S,
    **_ignored: Any,
) -> Any:
    """Verify ``netlist`` (simulate + rules), repairing while the
    verdict is STOP when an LLM is provided. Generator yielding
    ``{stage, agent, message, elapsed_s}`` events; returns a
    ``PipelineResult`` as the generator's return value.
    """
    timer = _Timer()
    events: list[dict] = []

    def emit(stage: str, agent: str, message: str) -> dict:
        event = _event(stage, agent, message, timer)
        events.append(event)
        return event

    netlist = _coerce_netlist(netlist)
    yield emit("start", "pipeline", "pipeline started")
    yield emit("simulate", "verifier", "simulating and checking rules")
    report = analyze(netlist, simulator=simulator, run_sim=run_sim)
    yield emit(
        "verify",
        "verifier",
        f"verdict {_verdict_str(report)} "
        f"({len(report.findings)} finding(s)): {_findings_text(report)}",
    )
    repairs: list[str] = []
    rounds = 0
    if not _is_pass(report) and llm is not None:
        result = yield from _drive_repair_events(
            _repair_loop_gen(
                netlist,
                report,
                simulator=simulator,
                run_sim=run_sim,
                llm_obj=llm,
                client=client,
                max_rounds=max_rounds,
                deadline_s=deadline_s,
                timer=timer,
            ),
            events,
        )
        netlist, report = result.netlist, result.report
        rounds = result.rounds
        repairs = [r for rs in result.reasons for r in rs]
    done_event = emit(
        "done",
        "pipeline",
        f"done: verdict {_verdict_str(report)} "
        f"({len(report.findings)} finding(s))"
        + (f" after {rounds} repair round(s)" if rounds else ""),
    )
    # Terminal result event: the server's SSE adapter treats a "done"
    # stage as the terminal event, but the UI needs the report payload
    # (verdict / findings / netlist) in "data". The progress-event
    # keys stay, so this event still passes the event-shape checks.
    done_event["event"] = "result"
    done_event["data"] = {
        **report.model_dump(mode="json"),
        "netlist": netlist.model_dump(mode="json"),
        "repairs": repairs,
        "rounds": rounds,
    }
    yield done_event
    return PipelineResult(
        netlist=netlist,
        report=report,
        events=events,
        repairs=repairs,
        rounds=rounds,
    )


def run_image(
    image_bytes: bytes,
    *,
    client: Any = None,
    llm: Any = None,
    read_fn: Any = None,
    simulator: Any = None,
    run_sim: Any = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    deadline_s: float = REPAIR_BUDGET_S,
    **_ignored: Any,
) -> Any:
    """Transcribe a schematic image (``N_CANDIDATES`` parallel reads,
    majority consensus), verify by simulation, repair while STOP.
    Generator yielding ``{stage, agent, message, elapsed_s}`` events;
    returns a ``PipelineResult`` as the generator's return value.
    Fewer than 2 successful reads raises; an LLM failure raises,
    never a guessed netlist. With >= 1 unmodeled part the final
    verdict is capped at WARN (PASS becomes WARN; STOP stays STOP).
    """
    timer = _Timer()
    events: list[dict] = []

    def emit(stage: str, agent: str, message: str) -> dict:
        event = _event(stage, agent, message, timer)
        events.append(event)
        return event

    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("run_image requires non-empty image bytes")
    image_bytes = bytes(image_bytes)
    yield emit("start", "pipeline", "pipeline started")
    yield emit("read", "reader", f"{N_CANDIDATES} readers transcribing the schematic in parallel")

    def _read_once() -> tuple[Netlist, list[dict]]:
        try:
            return _call_read(llm, read_fn, image_bytes, client)
        except Exception as exc:  # noqa: BLE001 - wrapped below
            raise RuntimeError(f"LLM call failed: {exc}") from exc

    runs: list[Netlist] = []
    unmodeled_runs: list[list[dict]] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=N_CANDIDATES) as pool:
        futures = [pool.submit(_read_once) for _ in range(N_CANDIDATES)]
        for future in futures:
            try:
                netlist, unmodeled = future.result()
                runs.append(netlist)
                unmodeled_runs.append(unmodeled)
            except Exception as exc:  # noqa: BLE001 - collected, raised below
                failures.append(str(exc))
    if len(runs) < 2:
        raise RuntimeError(
            "LLM call failed: schematic transcription failed, "
            f"{len(runs)} of {N_CANDIDATES} reads succeeded; "
            f"last error: {failures[-1] if failures else 'none'}"
        )
    for i, run in enumerate(runs, 1):
        yield emit("read", f"Reader {i}", f"read {len(run.components)} part(s): "
                   + ", ".join(c.id for c in run.components[:12]))
    for i, msg in enumerate(failures, len(runs) + 1):
        yield emit("read", f"Reader {i}", f"failed: {msg[:120]}")
    consensus = consensus_netlists(runs)
    netlist = consensus.netlist
    # Merge unmodeled parts across the parallel reads: dedupe by id
    # (first note wins), drop any id that landed in the consensus
    # netlist after all (a part modeled by the majority is modeled),
    # sort by id so the order is deterministic.
    consensus_ids = {c.id for c in netlist.components}
    seen_unmodeled: set[str] = set()
    unmodeled: list[dict] = []
    for run_unmodeled in unmodeled_runs:
        for part in run_unmodeled:
            if part["id"] in consensus_ids or part["id"] in seen_unmodeled:
                continue
            seen_unmodeled.add(part["id"])
            unmodeled.append(part)
    unmodeled.sort(key=lambda p: p["id"])
    yield emit(
        "consensus",
        "reader",
        f"consensus netlist: {len(netlist.components)} component(s)"
        + (
            f"; {len(consensus.uncertain)} below-majority component(s) excluded"
            if consensus.uncertain
            else ""
        )
        + (
            f"; {len(unmodeled)} unmodeled part(s) noted: "
            + ", ".join(p["id"] for p in unmodeled)
            if unmodeled
            else ""
        ),
    )
    yield emit("simulate", "verifier", "simulating and checking rules")
    uncertain_notes = [
        f"{cid}: seen in fewer than a majority of the {len(runs)} "
        "schematic reads; excluded from the consensus netlist"
        for cid in consensus.uncertain
    ]
    report = analyze(
        netlist,
        simulator=simulator,
        run_sim=run_sim,
        extra_uncertain=uncertain_notes or None,
    )
    yield emit(
        "verify",
        "verifier",
        f"verdict {_verdict_str(report)} "
        f"({len(report.findings)} finding(s)): {_findings_text(report)}",
    )
    repairs: list[str] = []
    rounds = 0
    # Repair only when an LLM is actually available: an explicitly
    # injected one, or the real llm module (read_fn is None).
    if not _is_pass(report) and (llm is not None or read_fn is None):
        result = yield from _drive_repair_events(
            _repair_loop_gen(
                netlist,
                report,
                simulator=simulator,
                run_sim=run_sim,
                llm_obj=llm,
                client=client,
                max_rounds=max_rounds,
                deadline_s=deadline_s,
                timer=timer,
            ),
            events,
        )
        netlist, report = result.netlist, result.report
        rounds = result.rounds
        repairs = [r for rs in result.reasons for r in rs]
    # Unmodeled parts cap the verdict at WARN: a PASS becomes WARN
    # (a WARN finding names the parts), STOP / WARN are unchanged.
    # Capped here, after repair, so no round is spent chasing a
    # verdict the repairer cannot fix.
    report = _cap_verdict_for_unmodeled(report, unmodeled)
    done_event = emit(
        "done",
        "pipeline",
        f"done: verdict {_verdict_str(report)} "
        f"({len(report.findings)} finding(s))"
        + (f" after {rounds} repair round(s)" if rounds else ""),
    )
    # Terminal result event: the server's SSE adapter treats a "done"
    # stage as the terminal event, but the UI needs the report payload
    # (verdict / findings / netlist) in "data". The progress-event
    # keys stay, so this event still passes the event-shape checks.
    done_event["event"] = "result"
    done_event["data"] = {
        **report.model_dump(mode="json"),
        "netlist": netlist.model_dump(mode="json"),
        "repairs": repairs,
        "rounds": rounds,
        "unmodeled": unmodeled,
    }
    yield done_event
    return PipelineResult(
        netlist=netlist,
        report=report,
        events=events,
        repairs=repairs,
        rounds=rounds,
        unmodeled=unmodeled,
    )


def run_goal(
    goal: str,
    *,
    client: Any = None,
    llm: Any = None,
    design_fn: Any = None,
    simulator: Any = None,
    run_sim: Any = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    deadline_s: float = REPAIR_BUDGET_S,
    team: bool = False,
    designers: int = 4,
    prefs: dict | None = None,
    **_ignored: Any,
) -> Any:
    """Design a circuit for ``goal`` (best-of-``N_CANDIDATES``, each
    verified), then repair while STOP. Generator yielding ``{stage,
    agent, message, elapsed_s}`` events; returns a ``PipelineResult``
    as the generator's return value. Any LLM failure raises.

    ``prefs`` is the optional goal-mode preferences dict (see
    :func:`_resolve_goal_prefs`): ``objective`` and/or
    ``target_ma``. In team mode it is passed to
    :func:`design_team`, so the tuner scores and the designer
    prompts honour it; the resolved ``objective`` is carried on the
    terminal result event and the returned ``PipelineResult``.
    Without ``prefs`` the objective is ``"balanced"`` and behaviour
    is unchanged.

    With ``team=True`` the design step runs :func:`design_team`
    instead of :func:`design`: one strategist per
    :data:`CIRCUIT_DESIGNERS` entry, each emitting its own
    ``Designer <Name>`` working/done events plus ``Judge`` and
    ``Physics``, so the dock shows one lane per designer. In team
    mode the aggregate lowercase ``designer`` / ``verifier`` events
    are dropped (they would duplicate the per-designer / ``Physics``
    lanes) and the terminal ``data`` plus the returned
    ``PipelineResult`` carry the ``leaderboard``. The terminal frame
    itself is unchanged: ``event: "result"`` with the flat report
    and ``done: true`` (dialect A, Option 1).
    """
    timer = _Timer()
    objective, _ = _resolve_goal_prefs(prefs)
    events: list[dict] = []

    def emit(stage: str, agent: str, message: str) -> dict:
        event = _event(stage, agent, message, timer)
        events.append(event)
        return event

    yield emit("start", "pipeline", "pipeline started")
    leaderboard: list[dict] = []
    if team:
        # Team events carry their own elapsed_s (design_team's
        # clock); re-stamp them on this run's clock so the stream's
        # elapsed_s stays non-decreasing.
        team_events: list[dict] = []
        designed = design_team(
            goal,
            llm=design_fn if design_fn is not None else llm,
            client=client,
            simulator=simulator,
            run_sim=run_sim,
            designers=designers,
            prefs=prefs,
            on_event=team_events.append,
        )
        for team_event in team_events:
            stamped = {
                **team_event,
                "elapsed_s": round(timer.elapsed(), 3),
            }
            events.append(stamped)
            yield stamped
        netlist, report = designed.netlist, designed.report
        leaderboard = list(designed.leaderboard)
    else:
        yield emit("design", "designer", f"designing circuit (best of {N_CANDIDATES})")
        designed = design(
            goal,
            llm=design_fn if design_fn is not None else llm,
            client=client,
            simulator=simulator,
            run_sim=run_sim,
            n=N_CANDIDATES,
        )
        netlist, report = designed.netlist, designed.report
        yield emit(
            "design",
            "designer",
            f"best-of-{len(designed.candidates)}: verdict "
            f"{_verdict_str(report)} ({len(report.findings)} finding(s))",
        )
        yield emit(
            "verify",
            "verifier",
            f"verdict {_verdict_str(report)} "
            f"({len(report.findings)} finding(s)): {_findings_text(report)}",
        )
    repairs: list[str] = []
    rounds = 0
    if not _is_pass(report) and (llm is not None or design_fn is None):
        result = yield from _drive_repair_events(
            _repair_loop_gen(
                netlist,
                report,
                simulator=simulator,
                run_sim=run_sim,
                llm_obj=llm,
                client=client,
                max_rounds=max_rounds,
                deadline_s=deadline_s,
                timer=timer,
            ),
            events,
        )
        netlist, report = result.netlist, result.report
        rounds = result.rounds
        repairs = [r for rs in result.reasons for r in rs]
    done_event = emit(
        "done",
        "pipeline",
        f"done: verdict {_verdict_str(report)} "
        f"({len(report.findings)} finding(s))"
        + (f" after {rounds} repair round(s)" if rounds else ""),
    )
    # Terminal result event: the server's SSE adapter treats a "done"
    # stage as the terminal event, but the UI needs the report payload
    # (verdict / findings / netlist) in "data". The progress-event
    # keys stay, so this event still passes the event-shape checks.
    done_event["event"] = "result"
    done_event["data"] = {
        **report.model_dump(mode="json"),
        "netlist": netlist.model_dump(mode="json"),
        "repairs": repairs,
        "rounds": rounds,
        "leaderboard": leaderboard,
        "objective": objective,
    }
    yield done_event
    return PipelineResult(
        netlist=netlist,
        report=report,
        events=events,
        repairs=repairs,
        rounds=rounds,
        leaderboard=leaderboard,
        objective=objective,
    )
