"""Netproof backend server (FastAPI).

File owner: ``backend/netproof/server.py`` ONLY. This module owns the HTTP
layer and nothing else: it never implements IR parsing, SPICE rendering,
rule checks, simulation, or LLM calls itself. Those live in the sibling
modules, whose real APIs this file is written against (read 2026-11-15)::

    ir.py     - Netlist / Component / Finding / Report (pydantic v2).
                Severity STOP/WARN/INFO; Verdict PASS/WARN/STOP (derived).
                Report REQUIRES ``netlist_used``; findings sort STOP first.
    spice.py  - ``simulate(netlist) -> SimResult`` (the one-call real
                path: ``netlist_to_spice`` + ``run_ngspice``),
                ``netlist_to_spice(netlist) -> str`` (the exact deck that
                gets simulated; raises ValueError on an empty netlist),
                ``run_ngspice(spice_text) -> SimResult`` (raises
                RuntimeError if ngspice is missing or times out after
                30 s; a failed sim returns ``success=False`` instead),
                ``parse_op_output(text)``.
    rules.py  - ``pre_sim_checks(netlist) -> list[Finding]`` (static ERC),
                ``check_rules(netlist, sim_result=None)``,
                ``verify(netlist, sim_result=None) -> ir.Report``.
                ``verify`` accepts a ``SimResult`` or a plain dict with
                ``node_voltages`` / ``branch_currents`` keys.
    llm.py    - ``read_schematic(image_bytes, client=None) -> Netlist``,
                ``design(goal, client=None) -> Netlist``,
                ``repair(netlist, findings, client=None)``
                -> ``(repaired_netlist, reasons)``. ``repair`` REQUIRES a
                non-empty findings list and raises ValueError loudly on
                bad replies; there is never a silent fallback. Only real
                calls (``client=None``) need the selected provider's SDK
                + key (google-genai + ``GEMINI_API_KEY`` by default;
                Anthropic SDK + ``ANTHROPIC_API_KEY`` for the fallback).
    bom.py    - ``netlist_to_bom_csv(netlist) -> str`` (header
                ``id,type,value,rating,qty``; groups by type+value).
    parts.py  - part limits table (used by rules/spice, not by the server).
    pipeline.py - The streaming endpoints delegate to it (see
                ``_run_stream``): ``/run-image`` runs
                ``pipeline.run_image`` (3 parallel schematic reads,
                >= 2/3 consensus, then verify + repair while STOP)
                and ``/run-goal`` runs ``pipeline.run_goal``
                (best-of-3 design, each candidate simulated, then
                repair while STOP). The server NEVER makes its own
                ``llm.read_schematic`` / ``llm.design`` call on those
                paths: a single read / single design would bypass the
                consensus and best-of-3 trust checks, so the pipeline
                is the one real path. ``/check`` still composes
                ``spice`` + ``rules`` directly (no LLM, ever) and
                ``/repair`` is one frontend-driven repair round;
                both are equivalent to ``pipeline.analyze`` /
                ``pipeline.repair_loop`` for a single round, so they
                stay direct rather than being re-plumbed.

Endpoints::

    GET  /                    serve web/index.html
    Static files: web/ is mounted at "/" AFTER all API routes, so
    /3d/main3d.js and the rest of web/3d/ are served same-origin
    without shadowing any API route.
    GET  /health              {"status","ngspice","ngspice_path",
                              "simulator_override","modules"}
    POST /check               JSON Netlist -> report dict {verdict,
                              findings, netlist, mock} (NO LLM, ever)
    GET  /rules               rule inventory (12 rows, thresholds from
                              rules.py constants, no ngspice, no mock)
    GET  /parts               standard-value catalog from parts.py
                              (E12 resistors/capacitors, LED colours,
                              diode Vf, source values; optional
                              ?type= filter; no ngspice, no mock)
    POST /sweep               JSON {netlist, resistor_id, values?,
                              led_id?, include_findings?} -> per-value
                              verification points (same pipeline as
                              /check per point; never updates downloads)
    POST /run-image           multipart image -> SSE events, then Report
    POST /run-goal            JSON {"goal": str} -> SSE events, then Report
    POST /repair              JSON {"netlist","findings","round"} ->
                              repaired netlist rows + changes + round +
                              verdict + findings (via llm.repair + verify)
    POST /download/netlist    JSON Netlist -> netlist.cir (text/plain,
                              the exact SPICE text that was simulated)
    GET  /download/netlist.cir  same, for the last checked netlist
    POST /download/bom        JSON Netlist -> bom.csv
                              (id,type,value,rating,qty)
    GET  /download/bom.csv     same, for the last checked netlist

    There is deliberately NO bodyless GET /download/netlist or
    GET /download/bom: a GET carries no netlist to render, so those
    routes were removed (they used to 400 on the missing body).
    GET /download/netlist.cir and /download/bom.csv serve the last
    checked netlist (``_LAST_NETLIST``): one entry per owner
    (``user:<id>`` for a signed-in session, else ``ip:<client ip>``),
    lost on restart (404 until the next check), so POST /download/*
    with the current table is the reliable path. A second tab of the
    SAME user overwrites that user's download; another user's check
    never touches it.

    SSE contract (``/run-image``, ``/run-goal``): ``progress`` events
    carry only agent / message / stage (plus filename/goal) / mock,
    never a netlist, verdict, or findings, so nothing is applied
    mid-stream. Goal-mode team events additionally carry ``state`` /
    ``score`` / ``calls`` / ``physics`` (the dock dialect) so the
    stats dock can light up the Designer / Judge / Physics lanes.
    The terminal event is ``result`` and carries the full
    shaped report plus ``done: true`` (and the design-team
    ``leaderboard`` for goal runs); stream close is also a
    terminator (the UI treats both as "run finished"). An ``error``
    event carries ``error`` / ``http_status`` / ``detail`` and ends
    the stream with no result; it is never the final result event.

Startup rule: ngspice MUST be on PATH. The check runs in the FastAPI
startup event (``shutil.which("ngspice")``) and raises ``RuntimeError``
with install instructions if absent, so the app refuses to start. It is
deliberately NOT checked at import time, so tests can import this module
without ngspice installed. One exception, for tests only: if a fake
simulator has been injected via :func:`set_simulator` (see below), the
app starts without ngspice, because simulation is then served by the
injected fake. Production never injects one.

Test hook: :func:`set_simulator` (from ``backend/tests/test_server.py``).
Pass an object with ``.run(netlist) -> SimResult | dict``, or a plain
callable ``sim(netlist)``, or ``None`` to restore real ngspice. The
injected simulator is used by ``/check`` (and therefore by repair
verification and the streaming runs) instead of ngspice.

Failure policy: LLM failure -> HTTP 502 with a visible error JSON (or an
SSE ``error`` event carrying ``http_status: 502`` once streaming has
started). There is NEVER a silent fallback: no alternate model, no
returning the input as "repaired", no empty report. Simulation-infra
failure (ngspice missing / timeout) -> HTTP 500 with a visible error.
Invalid netlist JSON -> HTTP 422 with the same ``{"error",
"message", ...}`` shape as every other error (``error:
"validation_error"`` for request validation, ``"invalid_netlist"``
for a download body that fails ir validation), never FastAPI's
default ``{"detail": [...]}`` shape. One deliberate exception:
``/repair`` returns HTTP 200 with ``verified: false``,
``report: null``, ``verdict: null`` when verification of the repaired
netlist fails; the repaired rows are still returned for manual
inspection, and the UI MUST check ``verified`` before applying
anything (see ``RepairOut``).

Mock mode: ``?mock=1`` ONLY, on ``/check``, ``/run-image`` and
``/run-goal``. Every mock event and mock Report carries ``mock=true`` so
the UI can show a MOCK badge. ``/repair`` does not support mock (a
fabricated repair would be a lie). Mock reports have exactly the real
Report keys (plus ``mock``): no ``spice`` / ``summary`` extras. Mock
streams use exactly the real progress stages (image: started,
vision_extract, spice_render, simulate, rules_check; goal: started,
plan, spice_render, simulate, rules_check) and end with the same
``done: true`` result event; the stages fire instantly, so mock
``elapsed_s`` / timing means nothing.

CORS: not needed (same origin).

Dependencies (see pyproject.toml): fastapi, sse-starlette,
python-multipart, uvicorn, pydantic, anthropic (llm only, lazy).
Run from ``backend/``: ``uv run uvicorn netproof.server:app --reload``.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import queue
import shutil
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

try:  # Preferred SSE implementation (task requirement).
    from sse_starlette.sse import EventSourceResponse
except ImportError:  # pragma: no cover - fallback if sse-starlette missing.
    from starlette.responses import StreamingResponse

    class EventSourceResponse(StreamingResponse):  # type: ignore[no-redef]
        """Minimal SSE shim with the same call shape as sse-starlette.

        Only used when ``sse-starlette`` is not installed. Wire format is
        identical (``event:`` / ``data:`` frames), so clients cannot tell.
        Install ``sse-starlette`` to use the real implementation.
        """

        def __init__(self, content: Any, **kwargs: Any) -> None:
            async def _body() -> AsyncIterator[bytes]:
                async for item in content:
                    if isinstance(item, dict):
                        event = item.get("event", "message")
                        data = item.get("data", "")
                        if not isinstance(data, str):
                            data = json.dumps(data, default=str)
                        frame = f"event: {event}\ndata: {data}\n\n"
                    else:
                        frame = f"data: {item}\n\n"
                    yield frame.encode("utf-8")

            super().__init__(
                _body(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # backend/netproof/ -> repo root
_INDEX_CANDIDATES = [
    _PROJECT_ROOT / "web" / "index.html",
    Path.cwd() / "web" / "index.html",
]
_WEB_DIR_CANDIDATES = [
    _PROJECT_ROOT / "web",
    Path.cwd() / "web",
]

MAX_UPLOAD_BYTES = 10_000_000  # 10 MB cap on /run-image uploads.

MAX_REPAIR_ROUNDS = 3  # Repair runs at most 3 rounds (see web/index.html).

NGSPICE_INSTALL_HINT = (
    "ngspice was not found on PATH. bench. refuses to start without it: "
    "simulation and verification run through ngspice. Install it "
    "(e.g. macOS: 'brew install ngspice'; Debian/Ubuntu: "
    "'sudo apt install ngspice') and make sure 'ngspice' is on PATH, "
    "then restart the server."
)

# Sibling modules this server adapts to.
_SIBLINGS = ("ir", "spice", "rules", "llm", "bom", "parts", "pipeline")
_module_cache: dict[str, Any] = {}
_module_error: dict[str, str] = {}


def _load_sibling(name: str) -> Optional[Any]:
    """Import a sibling module by name, or return None if it is absent.

    Tries a normal package import first, then loads the ``<name>.py`` file
    sitting next to this file, so the server works whether it is run as
    ``netproof.server`` or loaded standalone. Failures are recorded in
    ``_module_error`` for /health and 501 messages.

    Only SUCCESSES are cached. A failed import is never cached: a
    transient failure (a dependency that is briefly broken, a half-
    written file mid-deploy) must not 501 every AI route until the
    process restarts. The next call retries the import from scratch,
    so as soon as the module is importable again the routes recover
    with no restart.
    """
    if name in _module_cache:
        return _module_cache[name]
    module: Optional[Any] = None
    error = ""
    for attempt in range(3):
        try:
            if attempt == 0 and __package__:
                module = importlib.import_module(f".{name}", __package__)
            elif attempt == 1:
                module = importlib.import_module(f"netproof.{name}")
            else:
                path = _HERE / f"{name}.py"
                if not path.is_file():
                    raise ImportError(f"no file {_HERE / (name + '.py')}")
                spec = importlib.util.spec_from_file_location(
                    f"netproof.{name}", path
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load spec for {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            break
        except Exception as exc:  # noqa: BLE001 - record and try next way
            error = f"{type(exc).__name__}: {exc}"
            module = None
    if module is not None:
        _module_cache[name] = module
        _module_error.pop(name, None)
        return module
    _module_error[name] = error or "module not found"
    return None


def _sibling_status() -> dict[str, bool]:
    return {name: _load_sibling(name) is not None for name in _SIBLINGS}


def _ngspice_available() -> bool:
    return shutil.which("ngspice") is not None


# ---------------------------------------------------------------------------
# IR models (real ones from ir.py; loose stand-ins only if ir.py is absent,
# so importing this module never crashes on a missing sibling).
# ---------------------------------------------------------------------------

_ir_mod = _load_sibling("ir")


class _FallbackComponent(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    type: str = ""
    value: float = 0.0
    nodes: list[str] = Field(default_factory=list)


class _FallbackNetlist(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = ""
    components: list[_FallbackComponent] = Field(default_factory=list)


class _FallbackFinding(BaseModel):
    model_config = ConfigDict(extra="allow")
    severity: str = "INFO"
    rule_id: str = "UNKNOWN"
    component_ids: list[str] = Field(default_factory=list)
    message: str = ""


if _ir_mod is not None:
    IRNetlist = _ir_mod.Netlist
    IRFinding = _ir_mod.Finding
    IRReport = _ir_mod.Report
else:  # pragma: no cover - ir.py is present in the repo
    IRNetlist = _FallbackNetlist  # type: ignore[assignment]
    IRFinding = _FallbackFinding  # type: ignore[assignment]
    IRReport = None  # type: ignore[assignment]


class GoalIn(BaseModel):
    goal: str = Field(..., min_length=1)


class RepairIn(BaseModel):
    netlist: IRNetlist
    findings: list[IRFinding] = Field(default_factory=list)
    round: int = 1


class SweepIn(BaseModel):
    netlist: IRNetlist
    resistor_id: str
    values: list[float] | None = None
    led_id: str | None = None
    include_findings: bool = True


# Default sweep values (ohms): 10 E12 values, 100 ohm to 2.2 kohm.
DEFAULT_SWEEP_VALUES: list[float] = [
    100, 120, 180, 270, 390, 560, 820, 1200, 1800, 2200,
]
MAX_SWEEP_POINTS = 25


class RepairOut(BaseModel):
    """Repair result, in the shape web/index.html consumes.

    ``netlist`` is the repaired netlist as editable table rows (same
    shape as /check: a list of ``{id, type, value, nodes,
    node_voltage, current, power}`` dicts), NOT an ir.Netlist; callers
    needing a Netlist wrap the rows as ``{"title": "", "components":
    rows}``. ``changes`` is one reason per changed component,
    ``round`` echoes the request round, and ``verdict`` / ``findings``
    are the fresh verification results. ``reasons`` is kept as an
    alias of ``changes`` (identical list, for older consumers); new
    code should read ``changes``. ``report`` carries the full shaped
    report.

    Unverified repair (HTTP 200, NOT an error status, so the repaired
    rows survive for manual inspection): ``verified`` is False,
    ``report`` is None, ``verdict`` is None, ``findings`` is empty,
    and ``verification_error`` names the failure. Consumers MUST
    check ``verified`` before applying ``netlist`` / ``verdict`` /
    ``findings``: a 200 here does not mean the repair is good.
    """

    model_config = ConfigDict(extra="allow")

    netlist: Any
    report: Optional[Any] = None
    reasons: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    round: int = 0
    verdict: Optional[str] = None
    findings: list = Field(default_factory=list)
    verified: bool = False
    verification_error: Optional[str] = None
    mock: bool = False


# ---------------------------------------------------------------------------
# Simulator injection hook (tests only; see module docstring)
# ---------------------------------------------------------------------------

_SIMULATOR_OVERRIDE: Any = None

# Last netlist that went through a real check (covers /check, repair
# verification and the streaming runs). Serves GET /download/netlist.cir
# and GET /download/bom.csv, which take no request body. Scoped PER
# OWNER, not one process-global value: the store is keyed by
# ``_owner_key(request)`` (``user:<id>`` for a signed-in session, else
# ``ip:<client ip>``), so one user's check can never overwrite -- or be
# downloaded by -- another user on the public deploy. A second tab of
# the same user still overwrites that user's own download, and a
# restart clears the store (404 until the next check). POST /download/*
# renders from the posted netlist and has no such staleness; prefer it.
# The value starts as a dict; older code/tests that assigned None to
# clear it still work because the helpers below treat any non-dict
# value as an empty store.
_LAST_NETLIST: dict[str, Any] = {}

# Owner key for internal callers that record a netlist with no request
# in hand (direct _verify_netlist / _run_stream calls, tests). HTTP
# endpoints always pass a real per-request key instead.
_UNKNOWN_OWNER = "ip:unknown"


def _owner_key(request: Request | None) -> str:
    """Scope key for one user's last netlist.

    A signed ``bench_session`` cookie (see accounts.py) wins and yields
    ``user:<id>``: distinct accounts -- including distinct guests --
    never share, even behind one NAT. Without a session, fall back to
    the client IP (``ip:<ip>``), which still separates different
    networks; same-IP anonymous users share, which is the documented
    limit of the fallback (on the public deploy AUTH_REQUIRED=1, so
    every /check caller has a session and the IP fallback never runs).
    """
    if request is None:
        return _UNKNOWN_OWNER
    accounts = globals().get("_accounts")
    if accounts is not None:
        try:
            uid = accounts.read_session(request.cookies.get(accounts.COOKIE))
        except Exception:  # noqa: BLE001 - bad cookie must not 500 a check
            uid = None
        if uid is not None:
            return f"user:{uid}"
    return f"ip:{_client_ip(request)}"


def _record_last_netlist(owner: str, netlist: Any) -> None:
    """Store ``netlist`` as ``owner``'s last checked netlist."""
    global _LAST_NETLIST
    if not isinstance(_LAST_NETLIST, dict):
        _LAST_NETLIST = {}
    _LAST_NETLIST[owner] = netlist


def _last_netlist_for(owner: str) -> Any | None:
    """Return ``owner``'s last checked netlist, or None (never another
    owner's). A non-dict ``_LAST_NETLIST`` (e.g. None assigned by older
    clearing code) reads as an empty store."""
    if not isinstance(_LAST_NETLIST, dict):
        return None
    return _LAST_NETLIST.get(owner)


def set_simulator(simulator: Any) -> None:
    """Inject a fake simulator (tests only) or restore real ngspice.

    ``simulator`` is an object with ``.run(netlist) -> SimResult | dict``,
    a plain callable ``sim(netlist)``, or ``None`` to restore ngspice.
    Also gates the ngspice startup check: with a simulator injected the
    app starts without ngspice on PATH.
    """
    global _SIMULATOR_OVERRIDE
    _SIMULATOR_OVERRIDE = simulator


def get_simulator() -> Any:
    """Return the currently injected simulator, or None for real ngspice."""
    return _SIMULATOR_OVERRIDE


# ---------------------------------------------------------------------------
# Check pipeline: pre-sim ERC -> simulate -> rules.verify (NO LLM, ever)
# ---------------------------------------------------------------------------


def _require_module(name: str) -> Any:
    module = _load_sibling(name)
    if module is None:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "module_unavailable",
                "message": (
                    f"{name}.py is not present yet, so this endpoint cannot "
                    "run. See the server.py module docstring for the "
                    "expected sibling API."
                ),
                "module": name,
            },
        )
    return module


def _run_simulation(netlist: Any) -> Any:
    """Simulate ``netlist`` and return a SimResult (or a sim-result dict).

    Uses the injected simulator when one is set, else renders the SPICE
    deck with ``spice.netlist_to_spice`` and runs real ngspice.
    Raises HTTPException 422 (empty/unrenderable netlist) or 500
    (ngspice infra failure) with a visible error.
    """
    simulator = _SIMULATOR_OVERRIDE
    if simulator is not None:
        run = getattr(simulator, "run", None)
        if callable(run):
            return run(netlist)
        if callable(simulator):
            return simulator(netlist)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "bad_simulator",
                "message": (
                    "injected simulator has neither a .run(netlist) method "
                    "nor a callable interface."
                ),
            },
        )
    spice = _require_module("spice")
    try:
        spice_text = spice.netlist_to_spice(netlist)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "unrenderable_netlist", "message": str(exc)},
        ) from exc
    try:
        return spice.run_ngspice(spice_text)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "simulation_error", "message": str(exc)},
        ) from exc


def _verify_netlist(
    netlist: Any, *, record: bool = True, owner: str | None = None
) -> Any:
    """Full check of a netlist: ERC, then simulate + rules, -> ir.Report.

    If pre-simulation checks already contain a STOP finding, simulation
    is skipped (a degenerate deck must not burn a 30 s ngspice timeout)
    and the report is built from the ERC findings alone. No LLM is
    involved on this path, ever.

    ``record=False`` skips updating the last-netlist store: the sweep
    endpoint checks 10 variants, not one canonical netlist, so it
    must not corrupt the downloads served from the last checked
    netlist. /check, repair verification and the streaming runs keep
    the default ``record=True``. ``owner`` scopes the recorded
    netlist to one user (see ``_owner_key``); HTTP endpoints pass
    their request's key, direct callers fall back to ``_UNKNOWN_OWNER``.
    """
    rules = _require_module("rules")
    if record:
        _record_last_netlist(owner or _UNKNOWN_OWNER, netlist)
    pre_findings = rules.pre_sim_checks(netlist)
    if any(getattr(f, "severity", None) and str(f.severity) == "STOP" for f in pre_findings):
        return rules.verify(netlist, None)
    sim_result = _run_simulation(netlist)
    return rules.verify(netlist, sim_result)


def _render_spice_text(netlist: Any) -> str:
    """The exact SPICE text that would be simulated for ``netlist``."""
    spice = _require_module("spice")
    return spice.netlist_to_spice(netlist)


def _netlist_rows(
    netlist: Any,
    node_voltages: dict,
    branch_currents: dict,
    component_power: dict,
) -> list[dict]:
    """One table row per component, in the shape web/index.html renders.

    ``node_voltage`` is the voltage at the component's first node,
    ``current`` the branch current when ngspice reports one (else the
    resistor current from Ohm's law), ``power`` the dissipated power.
    """
    components = getattr(netlist, "components", None)
    if components is None and isinstance(netlist, dict):
        components = netlist.get("components") or []
    # spice.py lowercases branch-current keys ("v1" for "V1") and node
    # names, so look all three maps up case-insensitively; a row id
    # "V1" / node "N1" must still find "v1" / "n1" in the Report.
    voltages = {str(k).lower(): v for k, v in node_voltages.items()}
    currents = {str(k).lower(): v for k, v in branch_currents.items()}
    powers = {str(k).lower(): v for k, v in component_power.items()}
    rows: list[dict] = []
    for comp in components or []:
        if isinstance(comp, dict):
            cid = comp.get("id")
            ctype = comp.get("type")
            value = comp.get("value")
            nodes = list(comp.get("nodes") or [])
        else:
            cid = getattr(comp, "id", None)
            raw_type = getattr(comp, "type", None)
            ctype = getattr(raw_type, "value", raw_type)
            value = getattr(comp, "value", None)
            nodes = list(getattr(comp, "nodes", None) or [])
        current = currents.get(str(cid).lower()) if cid else None
        if current is None and str(ctype).lower() in ("led", "diode") and not isinstance(comp, dict):
            # Same derivation the rules use (KCL at the LED's nodes), so the
            # table and the LED-current readout can never disagree with the verdict.
            try:
                from . import rules as _rules

                current = _rules.led_current(comp, list(components), node_voltages, branch_currents)
            except Exception:  # underdetermined / unusual circuit: leave it unknown
                current = None
        if (
            current is None
            and str(ctype).lower() == "resistor"
            and value
            and len(nodes) >= 2
        ):
            v0 = voltages.get(nodes[0].lower())
            v1 = voltages.get(nodes[1].lower())
            if v0 is not None and v1 is not None:
                current = (v0 - v1) / value
        rows.append(
            {
                "id": cid,
                "type": ctype,
                "value": value,
                "nodes": nodes,
                "node_voltage": voltages.get(nodes[0].lower()) if nodes else None,
                "current": current,
                "power": powers.get(str(cid).lower()) if cid else None,
            }
        )
    return rows


def _report_response(report: Any, netlist: Any) -> dict:
    """Shape a check report into the JSON the web UI consumes.

    ``report`` is an ir.Report (or a plain dict in the same shape).
    Adds the editable-netlist ``netlist`` rows and ``mock: false`` to
    the report fields; findings already carry ``rule_id`` (ir shape),
    so they round-trip into POST /repair unchanged.
    """
    if hasattr(report, "model_dump"):
        data = report.model_dump(mode="json")
    elif hasattr(report, "to_dict"):
        data = report.to_dict()
    elif isinstance(report, dict):
        data = dict(report)
    else:
        raise ValueError(
            f"cannot serialise report of type {type(report).__name__}"
        )
    data["netlist"] = _netlist_rows(
        netlist,
        data.get("node_voltages") or {},
        data.get("branch_currents") or {},
        data.get("power") or {},
    )
    data["mock"] = bool(data.get("mock", False))
    return data


# ---------------------------------------------------------------------------
# Mock mode (only when ?mock=1; every event/report carries mock=true)
# ---------------------------------------------------------------------------

_MOCK_MESSAGE = (
    "Mock mode: no image, simulation, or LLM ran. "
    "This Report is a placeholder so the UI can be exercised."
)


def _mock_report(netlist: Any) -> dict:
    """A mock ir.Report as a plain dict, plus mock=true for the UI.

    Same keys as a real Report (verdict, findings, node_voltages,
    branch_currents, power, netlist_used, uncertain) plus ``mock``.
    No ``spice`` / ``summary`` extras: mock must not carry fields the
    real path never sends, or consumers learn to depend on them.
    """
    ir_mod = _load_sibling("ir")
    if ir_mod is not None:
        report = ir_mod.Report(
            verdict="PASS",
            findings=[
                ir_mod.Finding(
                    severity="INFO", rule_id="MOCK", message=_MOCK_MESSAGE
                )
            ],
            netlist_used=netlist,
            uncertain=["Mock mode: nothing was simulated or checked."],
        )
        data = report.model_dump(mode="json")
    else:  # pragma: no cover - ir.py is present in the repo
        data = {
            "verdict": "PASS",
            "findings": [
                {
                    "severity": "INFO",
                    "rule_id": "MOCK",
                    "component_ids": [],
                    "message": _MOCK_MESSAGE,
                }
            ],
            "node_voltages": {},
            "branch_currents": {},
            "power": {},
            "netlist_used": (
                netlist.model_dump(mode="json")
                if hasattr(netlist, "model_dump")
                else netlist
            ),
            "uncertain": ["Mock mode: nothing was simulated or checked."],
        }
    data["mock"] = True
    return data


async def _mock_event_stream(
    kind: str, *, detail: dict, report: dict
) -> AsyncIterator[dict]:
    """Mock SSE stages, identical to the real stream's stages.

    Image: started, vision_extract, spice_render, simulate,
    rules_check. Goal: started, plan, spice_render, simulate,
    rules_check (no "generate": the real goal stream has no such
    stage). Stages fire instantly, so mock timing means nothing.
    The terminal ``result`` event carries ``done: true``, exactly
    like the real stream; stream close is also a terminator.
    """
    stages = (
        ["started", "vision_extract", "spice_render", "simulate", "rules_check"]
        if kind == "image"
        else ["started", "plan", "spice_render", "simulate", "rules_check"]
    )
    for stage in stages:
        yield {
            "event": "progress",
            "data": {"stage": stage, "mock": True, **detail},
        }
    yield {"event": "result", "data": {**report, "mock": True, "done": True}}


# ---------------------------------------------------------------------------
# Streaming runs (delegated to pipeline.run_image / pipeline.run_goal)
# ---------------------------------------------------------------------------


def _pipeline_progress_event(event: dict) -> dict:
    """Translate one pipeline progress event into an SSE event dict.

    Pipeline events carry ``stage`` / ``agent`` / ``message`` /
    ``elapsed_s``; the SSE shape the web UI consumes is
    ``{"event": "progress", "data": {...}}`` with only agent /
    message / stage / elapsed_s / mock keys, never a netlist,
    verdict, or findings mid-stream. Design-team events (goal mode,
    ``pipeline.design_team``) additionally carry the dock dialect —
    ``state`` / ``score`` / ``calls`` / ``physics`` — which is
    forwarded when present so the stats dock can light up the
    Designer / Judge / Physics lanes with live state and scores.
    """
    data: dict = {
        "agent": event.get("agent", "pipeline"),
        "message": event.get("message", ""),
        "stage": event.get("stage", ""),
        "elapsed_s": event.get("elapsed_s"),
        "mock": False,
    }
    for key in ("state", "score", "calls", "physics"):
        if event.get(key) is not None:
            data[key] = event[key]
    return _sse_event({"event": "progress", "data": data})


def _pipeline_error_event(exc: BaseException) -> dict:
    """Classify a pipeline failure for an SSE error event.

    LLM failures (pipeline wraps them as ``RuntimeError("LLM call
    failed: ...")``) keep the old ``_llm_error_event`` mapping
    (400 for bad image bytes / empty input, else 502). Any other
    failure (ngspice missing / timeout, unrenderable netlist) is a
    500 ``verification_failed``, as ``_verify_netlist`` used to
    raise for the same conditions. An error event ends the stream
    with no result; it is never the final result event.
    """
    message = str(exc)
    if (
        "LLM call failed" in message
        or "unsupported image format" in message
        or "requires non-empty" in message
    ):
        return _sse_event(_llm_error_event(exc))
    if isinstance(exc, ValueError):
        return _sse_event(
            {
                "event": "error",
                "data": {
                    "error": "invalid_input",
                    "http_status": 400,
                    "detail": message,
                    "mock": False,
                },
            }
        )
    return _sse_event(
        {
            "event": "error",
            "data": {
                "error": "verification_failed",
                "http_status": 500,
                "detail": f"{type(exc).__name__}: {message}",
                "mock": False,
            },
        }
    )


async def _run_stream(
    kind: str,
    *,
    mock: bool,
    image_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    goal: Optional[str] = None,
    owner: str | None = None,
) -> AsyncIterator[dict]:
    """Yield SSE-ready event dicts for /run-image or /run-goal.

    The real work is ``pipeline.run_image`` (image: 3 parallel
    reads + >= 2/3 consensus) or ``pipeline.run_goal`` (goal: a team
    of 4 strategists, every candidate simulated, winner by verdict
    rank), each followed by verify + repair-while-STOP. The pipeline generator is blocking,
    so it runs on a worker thread and forwards its progress events
    through a queue; this coroutine only translates them to SSE.
    The pipeline's own terminal event is NOT forwarded as-is: the
    final result is built from the generator's return value
    (``PipelineResult``) with ``_report_response``, so the result
    keeps the exact prior shape (netlist rows, ``mock: false``)
    plus ``repairs`` / ``rounds`` / ``round`` and ``done: true``.
    The injected simulator (``set_simulator``, tests only) is passed
    through so a fake simulator still serves the pipeline's
    simulations.
    """
    if mock:
        ir_mod = _load_sibling("ir")
        empty = ir_mod.Netlist(title="mock") if ir_mod is not None else {"title": "mock", "components": []}
        detail = {"filename": filename} if kind == "image" else {"goal": goal}
        report = _mock_report(empty)
        async for event in _mock_event_stream(kind, detail=detail, report=report):
            yield _sse_event(event)
        return

    pipeline = _require_module("pipeline")
    yield _sse_event(
        {
            "event": "progress",
            "data": {
                "agent": "system",
                "message": "Run started.",
                "stage": "started",
                "mock": False,
            },
        }
    )

    events: queue.Queue = queue.Queue()

    def _drive() -> None:
        """Run the blocking pipeline generator; queue its events,
        then its return value (``("done", PipelineResult)``), or
        its exception (``("error", exc)``)."""
        try:
            if kind == "image":
                gen = pipeline.run_image(
                    image_bytes, simulator=_SIMULATOR_OVERRIDE
                )
            else:
                # Goal mode is a real team (bench #8): four strategists
                # propose in parallel and the dock shows one lane per
                # designer plus Judge / Physics.
                gen = pipeline.run_goal(
                    goal, simulator=_SIMULATOR_OVERRIDE, team=True
                )
            while True:
                try:
                    event = next(gen)
                except StopIteration as done:
                    events.put(("done", done.value))
                    return
                events.put(("event", event))
        except BaseException as exc:  # noqa: BLE001 - forwarded to SSE
            events.put(("error", exc))

    threading.Thread(target=_drive, daemon=True).start()
    while True:
        tag, payload = await asyncio.to_thread(events.get)
        if tag == "event":
            # The pipeline's terminal event carries the report in
            # its "data" key; skip it here, the "done" return value
            # below is what the final result is built from.
            if isinstance(payload, dict) and payload.get("event") == "result":
                continue
            yield _pipeline_progress_event(payload)
        elif tag == "done":
            result = payload
            _record_last_netlist(owner or _UNKNOWN_OWNER, result.netlist)
            report_data = _report_response(result.report, result.netlist)
            report_data["repairs"] = list(result.repairs)
            report_data["rounds"] = result.rounds
            report_data["round"] = result.rounds
            report_data["unmodeled"] = list(getattr(result, "unmodeled", []) or [])
            report_data["leaderboard"] = list(
                getattr(result, "leaderboard", []) or []
            )
            report_data["done"] = True
            yield _sse_event({"event": "result", "data": report_data})
            return
        else:  # tag == "error"
            yield _pipeline_error_event(payload)
            return


def _llm_error_event(exc: Exception) -> dict:
    """Classify an LLM-call failure for an SSE error event.

    Client errors (bad image bytes, empty goal) -> http_status 400;
    everything else (missing SDK/key, model failure, bad reply) -> 502.
    """
    message = str(exc)
    if "unsupported image format" in message or "requires non-empty" in message:
        return {
            "event": "error",
            "data": {
                "error": "invalid_input",
                "http_status": 400,
                "detail": message,
                "mock": False,
            },
        }
    return {
        "event": "error",
        "data": {
            "error": "llm_error",
            "http_status": 502,
            "detail": f"LLM call failed: {type(exc).__name__}: {message}",
            "mock": False,
        },
    }


def _sse_event(event: dict) -> dict:
    data = event["data"]
    return {
        "event": event["event"],
        "data": data if isinstance(data, str) else json.dumps(data, default=str),
    }




# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup (NOT import time, so tests can import this module without
    # ngspice installed). Refuses to start without ngspice on PATH,
    # unless a test simulator was injected via set_simulator (in which
    # case simulation is served by the fake, not ngspice).
    if not _ngspice_available() and _SIMULATOR_OVERRIDE is None:
        raise RuntimeError(NGSPICE_INSTALL_HINT)
    yield


app = FastAPI(title="bench.", version="0.1.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# AI rate limit (public deploys only). Off unless NETPROOF_AI_RATE_LIMIT is
# set, so local runs and tests are unaffected. Counts POSTs to the routes that
# call the LLM (mock runs never call it, so they are free):
#   NETPROOF_AI_RATE_LIMIT  requests per visitor IP per hour
#   NETPROOF_AI_DAILY_CAP   requests per day across all visitors (0 = no cap)
# ponytail: in-memory, per instance, resets on restart; move to Redis if the
# app ever runs on more than one instance.
# ---------------------------------------------------------------------------
_AI_PATHS = {"/run-goal", "/run-image", "/repair", "/fiber/design", "/optics/design",
             "/fiber/design/stream", "/optics/design/stream"}
_AI_HITS: dict[str, deque] = defaultdict(deque)
_AI_DAY: deque = deque()


def _env_int(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name, "0")))
    except ValueError:
        return 0


def _client_ip(request: Request) -> str:
    # Behind one proxy (Koyeb) the RIGHTMOST X-Forwarded-For entry is the
    # one the proxy added; earlier entries are client-controlled.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd.strip():
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def _ai_rate_limit(request: Request, call_next):
    per_hour = _env_int("NETPROOF_AI_RATE_LIMIT")
    if (
        per_hour
        and request.method == "POST"
        and request.url.path in _AI_PATHS
        and request.query_params.get("mock", "").lower() not in ("1", "true")
    ):
        now = time.monotonic()
        hits = _AI_HITS[_client_ip(request)]
        while hits and now - hits[0] > 3600:
            hits.popleft()
        while _AI_DAY and now - _AI_DAY[0] > 86400:
            _AI_DAY.popleft()
        daily = _env_int("NETPROOF_AI_DAILY_CAP")
        if len(hits) >= per_hour or (daily and len(_AI_DAY) >= daily):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": "AI request limit reached for now. Examples, Re-check and Sweep "
                    "still work: they only use the simulator.",
                },
            )
        hits.append(now)
        _AI_DAY.append(now)
    return await call_next(request)


@app.get("/", response_class=FileResponse)
async def index() -> Any:
    """Serve the web UI (web/index.html)."""
    for candidate in _INDEX_CANDIDATES:
        if candidate.is_file():
            return FileResponse(candidate, media_type="text/html")
    raise HTTPException(
        status_code=404,
        detail={
            "error": "frontend_not_built",
            "message": "web/index.html not found; the web UI has not been built yet.",
        },
    )


@app.get("/health")
async def health() -> dict:
    # "ok" only when simulation can actually run (ngspice on PATH or
    # a test simulator injected) AND every sibling module loaded.
    # Anything else is "degraded": the old unconditional "ok" hid a
    # missing module behind a green status. (ngspice missing with no
    # override still refuses startup in the lifespan handler, so a
    # degraded ngspice status is only reachable in tests.)
    modules = _sibling_status()
    healthy = (_ngspice_available() or _SIMULATOR_OVERRIDE is not None) and all(
        modules.values()
    )
    return {
        "status": "ok" if healthy else "degraded",
        "ngspice": _ngspice_available(),
        "ngspice_path": shutil.which("ngspice"),
        "simulator_override": _SIMULATOR_OVERRIDE is not None,
        "modules": modules,
    }


@app.get("/rules")
async def rules_table() -> dict:
    """Rule inventory: one row per (rule_id, severity) pair, 12 rows.

    Thresholds are read live from the ``rules`` module constants
    (which mirror ``parts.py``); no threshold is hardcoded here, so
    if a constant changes in ``rules.py``, ``/rules`` follows it.
    Shared rule_ids (LED_OVERCURRENT, RESISTOR_POWER) span two
    severity tiers, distinguished by the ``band`` field ("warn" /
    "stop"); clients must key on (rule_id, band), not rule_id alone.
    Row order is STOP-first within a shared rule_id, matching
    ``rules.py`` check order and ``SEVERITY_RANK``. Read-only: no
    body, no ngspice, no ``?mock=1``.
    """
    rules = _require_module("rules")
    rows = [
        {
            "rule_id": "NO_GROUND",
            "severity": "STOP",
            "band": None,
            "applies_to": "circuit",
            "quantity": None,
            "comparator": None,
            "threshold": None,
            "threshold_unit": None,
            "threshold_display": None,
            "source": "rules.py: pre_sim_checks (NO_GROUND)",
        },
        {
            "rule_id": "FLOATING_NODE",
            "severity": "STOP",
            "band": None,
            "applies_to": "circuit",
            "quantity": None,
            "comparator": None,
            "threshold": None,
            "threshold_unit": None,
            "threshold_display": None,
            "source": "rules.py: pre_sim_checks (FLOATING_NODE)",
        },
        {
            "rule_id": "SHORTED_SOURCE",
            "severity": "STOP",
            "band": None,
            "applies_to": "source",
            "quantity": None,
            "comparator": None,
            "threshold": None,
            "threshold_unit": None,
            "threshold_display": None,
            "source": "rules.py: pre_sim_checks (SHORTED_SOURCE)",
        },
        {
            "rule_id": "SHORTED_COMPONENT",
            "severity": "WARN",
            "band": None,
            "applies_to": "component",
            "quantity": None,
            "comparator": None,
            "threshold": None,
            "threshold_unit": None,
            "threshold_display": None,
            "source": "rules.py: pre_sim_checks (SHORTED_COMPONENT)",
        },
        {
            "rule_id": "NO_SOURCE",
            "severity": "INFO",
            "band": None,
            "applies_to": "circuit",
            "quantity": None,
            "comparator": None,
            "threshold": None,
            "threshold_unit": None,
            "threshold_display": None,
            "source": "rules.py: pre_sim_checks (NO_SOURCE)",
        },
        {
            "rule_id": "REVERSED_LED",
            "severity": "INFO",
            "band": None,
            "applies_to": "led",
            "quantity": "anode_minus_cathode_v",
            "comparator": "<",
            "threshold": -1e-9,
            "threshold_unit": "V",
            "threshold_display": "anode 1 nV below cathode",
            "source": "rules.py: post_sim_checks (REVERSED_LED)",
        },
        {
            "rule_id": "LED_OVERCURRENT",
            "severity": "STOP",
            "band": "stop",
            "applies_to": "led",
            "quantity": "current_a",
            "comparator": ">",
            "threshold": rules.LED_STOP_A,
            "threshold_unit": "A",
            "threshold_display": "30 mA",
            "source": "parts.py: _LED_IF_MAX (via rules.py LED_STOP_A)",
        },
        {
            "rule_id": "LED_OVERCURRENT",
            "severity": "WARN",
            "band": "warn",
            "applies_to": "led",
            "quantity": "current_a",
            "comparator": ">=",
            "threshold": rules.LED_WARN_A,
            "threshold_unit": "A",
            "threshold_display": "20 mA",
            "source": "parts.py: _LED_IF_WARN (via rules.py LED_WARN_A)",
        },
        {
            "rule_id": "LED_UNDERCURRENT",
            "severity": "INFO",
            "band": None,
            "applies_to": "led",
            "quantity": "current_a",
            "comparator": "<",
            "threshold": rules.LED_UNDER_A,
            "threshold_unit": "A",
            "threshold_display": "1 mA",
            "source": "parts.py: _LED_IF_MIN (via rules.py LED_UNDER_A)",
        },
        {
            "rule_id": "RESISTOR_POWER",
            "severity": "STOP",
            "band": "stop",
            "applies_to": "resistor",
            "quantity": "power_fraction_of_rating",
            "comparator": ">",
            "threshold": rules.RESISTOR_STOP_FRACTION,
            "threshold_unit": "fraction",
            "threshold_display": "100% of rating",
            "source": "rules.py: RESISTOR_STOP_FRACTION",
        },
        {
            "rule_id": "RESISTOR_POWER",
            "severity": "WARN",
            "band": "warn",
            "applies_to": "resistor",
            "quantity": "power_fraction_of_rating",
            "comparator": ">",
            "threshold": rules.RESISTOR_WARN_FRACTION,
            "threshold_unit": "fraction",
            "threshold_display": "70% of rating",
            "source": "rules.py: RESISTOR_WARN_FRACTION",
        },
        {
            "rule_id": "SOURCE_OVERCURRENT",
            "severity": "WARN",
            "band": None,
            "applies_to": "source",
            "quantity": "current_a",
            "comparator": ">",
            "threshold": rules.SOURCE_OVERCURRENT_A,
            "threshold_unit": "A",
            "threshold_display": "1 A",
            "source": "rules.py: SOURCE_OVERCURRENT_A",
        },
    ]
    return {
        "rules": rows,
        "defaults": {"resistor_rating_w": rules.DEFAULT_RESISTOR_RATING_W},
        "notes": [
            "LED current limits are identical for all colours; only Vf differs per colour.",
            "Resistor power thresholds are fractions of each resistor's own rating_watts.",
        ],
    }


class _PartsType(str, Enum):
    """Filter for GET /parts. Unknown values 422 via FastAPI enum validation."""

    resistor = "resistor"
    capacitor = "capacitor"
    led = "led"
    diode = "diode"
    voltage_source = "voltage_source"
    current_source = "current_source"


@app.get("/parts")
async def parts_catalog_endpoint(
    type: _PartsType | None = Query(None),
) -> Any:
    """Standard-value catalog from parts.py (the parts picker data).

    No body, no auth, no LLM, no ngspice, no server-side state: the
    catalog is static data, identical in mock and real runs, so there
    is no ``?mock=1``. ``?type=<one type>`` returns only that block
    (plus ``version``); no param returns the whole catalog in one
    round trip. ``Cache-Control: public, max-age=86400``: the catalog
    only changes when parts.py changes (bump ``version`` then).
    Returns 501 ``module_unavailable`` if parts.py cannot be loaded.
    """
    parts = _require_module("parts")
    catalog = parts.parts_catalog()
    if type is None:
        payload: dict[str, Any] = catalog
    else:
        payload = {"version": catalog["version"], type.value: catalog[type.value]}
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/check")
def check(request: Request, netlist: IRNetlist, mock: bool = Query(False)) -> Any:
    """Check a netlist JSON -> report dict. NO LLM is ever called here.

    Sync ``def`` so FastAPI runs the (blocking, up to 30 s) ngspice call
    in its threadpool instead of blocking the event loop.
    """
    if _ir_mod is None:
        raise HTTPException(
            status_code=501,
            detail={"error": "module_unavailable", "message": "ir.py is not present.", "module": "ir"},
        )
    if mock:
        _record_last_netlist(_owner_key(request), netlist)
        # Same keys as a real report (plus mock): no "spice" extra.
        report = _mock_report(netlist)
        report["netlist"] = _netlist_rows(netlist, {}, {}, {})
        return JSONResponse(content=report)
    return JSONResponse(
        content=_report_response(
            _verify_netlist(netlist, owner=_owner_key(request)), netlist
        )
    )


def _sweep_error(code: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"error": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=422, detail=detail)


@app.post("/sweep")
def sweep(body: SweepIn, mock: bool = Query(False)) -> Any:
    """Sweep one resistor across a list of values, re-verifying at
    each point with the same pipeline as /check (ERC -> simulate ->
    rules.verify). Sync ``def`` so the blocking ngspice runs land in
    the threadpool, as /check does.

    Every point's verdict/findings come from ``rules.verify``; LED
    current is read via the public ``rules.led_current`` helper (the
    same KCL derivation ``verify`` used), never recomputed here.
    A per-point sim failure yields a null-valued point (a gap in the
    chart), never an aborted sweep. The sweep never updates
    ``_LAST_NETLIST`` (``record=False``), so downloads keep serving
    the last checked netlist.
    """
    import math

    rules = _require_module("rules")
    parts = _load_sibling("parts")
    netlist = body.netlist
    components = list(netlist.components)

    def _type_of(comp: Any) -> str:
        raw = getattr(comp, "type", None)
        return str(getattr(raw, "value", raw) or "").lower()

    # resistor_id must name a resistor.
    resistor = next(
        (c for c in components if c.id == body.resistor_id), None
    )
    if resistor is None:
        raise _sweep_error(
            "unknown_resistor",
            f"no component with id {body.resistor_id!r} in the netlist.",
        )
    if _type_of(resistor) != "resistor":
        raise _sweep_error(
            "invalid_resistor",
            f"component {body.resistor_id!r} is a {_type_of(resistor)}, "
            "not a resistor.",
        )

    # led_id: explicit must name an LED; default is the first LED.
    led = None
    if body.led_id is not None:
        led = next((c for c in components if c.id == body.led_id), None)
        if led is None:
            raise _sweep_error(
                "unknown_led",
                f"no component with id {body.led_id!r} in the netlist.",
            )
        if _type_of(led) != "led":
            raise _sweep_error(
                "invalid_led",
                f"component {body.led_id!r} is a {_type_of(led)}, "
                "not an LED.",
            )
    else:
        led = next((c for c in components if _type_of(c) == "led"), None)
    if led is None:
        raise _sweep_error(
            "no_led",
            "netlist has no LED to plot; a sweep needs an LED.",
        )

    # Values: defaults when omitted; explicit lists are validated.
    values = list(DEFAULT_SWEEP_VALUES) if body.values is None else list(body.values)
    if not values:
        raise _sweep_error("invalid_values", "values must not be empty.")
    if len(values) > MAX_SWEEP_POINTS:
        raise _sweep_error(
            "invalid_values",
            f"at most {MAX_SWEEP_POINTS} sweep points, got {len(values)}.",
        )
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise _sweep_error(
            "invalid_values",
            "every sweep value must be a positive finite resistance in ohms.",
        )
    if len(set(values)) != len(values):
        raise _sweep_error(
            "invalid_values", "sweep values must not contain duplicates."
        )

    if parts is not None:
        rating_w = parts.resistor_rating_w(resistor)
    else:  # pragma: no cover - parts.py is present in the repo
        rating_w = getattr(resistor, "rating_watts", None) or 0.25
    thresholds = {
        "led_warn_a": rules.LED_WARN_A,
        "led_stop_a": rules.LED_STOP_A,
        "led_under_a": rules.LED_UNDER_A,
        "resistor_warn_w": rules.RESISTOR_WARN_FRACTION * rating_w,
        "resistor_stop_w": rules.RESISTOR_STOP_FRACTION * rating_w,
    }

    if mock:
        # Placeholder points, clearly flagged mock: no ngspice, no
        # ERC gate, no invented numbers (nulls show as gaps).
        points = [
            {
                "r_ohms": v,
                "led_current_a": None,
                "resistor_power_w": None,
                "verdict": "PASS",
                "findings": [],
                "uncertain": ["Mock mode: no simulation ran."],
            }
            for v in values
        ]
        return JSONResponse(
            content={
                "resistor_id": body.resistor_id,
                "led_id": led.id,
                "rating_w": rating_w,
                "values": values,
                "points": points,
                "thresholds": thresholds,
                "mock": True,
            }
        )

    # Pre-flight ERC gate: a STOP finding means zero ngspice runs.
    pre_findings = rules.pre_sim_checks(netlist)
    if any(str(getattr(f, "severity", "")) == "STOP" for f in pre_findings):
        detail_findings = [
            f.model_dump(mode="json") if hasattr(f, "model_dump") else dict(f)
            for f in pre_findings
        ]
        raise _sweep_error(
            "erc_failed",
            "base netlist fails ERC with a STOP finding; sweep not run.",
            findings=detail_findings,
        )

    points: list[dict[str, Any]] = []
    for r_ohms in values:
        # Deep-copy the netlist with ONLY the swept resistor's value
        # replaced (Component is frozen, so model_copy(update=...)).
        # The posted netlist is never mutated.
        new_components = [
            c.model_copy(update={"value": r_ohms})
            if c.id == body.resistor_id
            else c
            for c in components
        ]
        point_netlist = netlist.model_copy(update={"components": new_components})
        report = _verify_netlist(point_netlist, record=False)
        voltages = dict(getattr(report, "node_voltages", None) or {})
        currents = dict(getattr(report, "branch_currents", None) or {})
        powers = {
            str(k).lower(): v
            for k, v in (getattr(report, "power", None) or {}).items()
        }
        uncertain = list(getattr(report, "uncertain", None) or [])
        sim_failed = any(
            "Simulation did not succeed" in str(u) for u in uncertain
        )
        if sim_failed:
            led_current_a = None
            resistor_power_w = None
        else:
            led_current_a = rules.led_current(
                led, new_components, voltages, currents
            )
            resistor_power_w = powers.get(str(body.resistor_id).lower())
        if hasattr(report, "model_dump"):
            finding_dicts = [
                {
                    "severity": str(f.get("severity", "")),
                    "rule_id": f.get("rule_id", ""),
                    "message": f.get("message", ""),
                }
                for f in report.model_dump(mode="json").get("findings", [])
            ]
        else:  # pragma: no cover - Report is a pydantic model
            finding_dicts = []
        points.append(
            {
                "r_ohms": r_ohms,
                "led_current_a": led_current_a,
                "resistor_power_w": resistor_power_w,
                "verdict": str(getattr(report, "verdict", "PASS")),
                "findings": finding_dicts if body.include_findings else [],
                "uncertain": uncertain,
            }
        )
    return JSONResponse(
        content={
            "resistor_id": body.resistor_id,
            "led_id": led.id,
            "rating_w": rating_w,
            "values": values,
            "points": points,
            "thresholds": thresholds,
            "mock": False,
        }
    )


@app.post("/run-image")
async def run_image(
    request: Request,
    image: UploadFile = File(...),
    goal: str | None = Form(None),
    mock: bool = Query(False),
) -> EventSourceResponse:
    """Multipart image -> SSE stream of pipeline events, then final Report.

    Form fields match web/index.html: ``image`` (the file) and an
    optional ``goal`` string. (``goal`` is accepted and ignored: the
    image alone determines the transcribed netlist.)
    """
    image_bytes = await image.read()
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "upload_too_large",
                "message": f"image is {len(image_bytes)} bytes; limit is {MAX_UPLOAD_BYTES}.",
            },
        )
    if not image_bytes:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_upload", "message": "uploaded file is empty."},
        )
    content_type = (image.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_an_image",
                "message": f"expected an image upload, got Content-Type {image.content_type!r}.",
            },
        )
    # Fail fast (before headers stream) if the pipeline module is
    # missing and this is not a mock run, so the client gets a clean
    # HTTP 501 instead of an SSE error frame. (Pipeline imports llm,
    # so this also gates on the LLM module.)
    if not mock:
        _require_module("pipeline")

    async def _events() -> AsyncIterator[dict]:
        async for event in _run_stream(
            "image",
            mock=mock,
            image_bytes=image_bytes,
            filename=image.filename or "upload",
            owner=_owner_key(request),
        ):
            yield event

    return EventSourceResponse(_events())


@app.post("/run-goal")
async def run_goal(
    request: Request, body: GoalIn, mock: bool = Query(False)
) -> EventSourceResponse:
    """JSON {"goal": str} -> SSE stream of pipeline events, then Report."""
    if not mock:
        _require_module("pipeline")

    async def _events() -> AsyncIterator[dict]:
        async for event in _run_stream(
            "goal", mock=mock, goal=body.goal, owner=_owner_key(request)
        ):
            yield event

    return EventSourceResponse(_events())


@app.post("/repair", response_model=RepairOut)
def repair(request: Request, body: RepairIn) -> RepairOut:
    """Repair a netlist via llm.repair, then verify with spice + rules.

    LLM failure -> HTTP 502 with a visible error JSON, never a silent
    fallback. ``llm.repair`` requires a non-empty findings list, so an
    empty list is a 400 here. If verification itself errors, the repaired
    netlist is still returned with ``verified=false`` and the error
    spelled out, never silently claimed as verified.
    """
    llm = _load_sibling("llm")
    if llm is None:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "llm_unavailable",
                "message": (
                    "llm.py is not present yet, so no repair can be made."
                ),
                "module": "llm",
            },
        )
    # RepairIn already validated these against ir.Netlist / ir.Finding.
    netlist = body.netlist
    findings = list(body.findings)
    if body.round < 1 or body.round > MAX_REPAIR_ROUNDS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "round_out_of_range",
                "message": (
                    f"repair round must be 1..{MAX_REPAIR_ROUNDS}, "
                    f"got {body.round}."
                ),
            },
        )
    if not findings:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "empty_findings",
                "message": (
                    "repair requires a non-empty findings list "
                    "(llm.repair refuses an empty list)."
                ),
            },
        )
    try:
        repaired, reasons = llm.repair(netlist, findings)
    except Exception as exc:  # noqa: BLE001 - ANY llm failure is a 502
        raise HTTPException(
            status_code=502,
            detail={
                "error": "llm_error",
                "message": f"LLM repair failed: {type(exc).__name__}: {exc}",
            },
        ) from exc

    # Verify the repaired netlist (spice + rules, no LLM involved).
    try:
        report = _verify_netlist(repaired, owner=_owner_key(request))
        shaped = _report_response(report, repaired)
        return RepairOut(
            netlist=shaped["netlist"],
            report=shaped,
            reasons=list(reasons),
            changes=list(reasons),
            round=body.round,
            verdict=shaped["verdict"],
            findings=shaped["findings"],
            verified=True,
        )
    except HTTPException as exc:
        detail = exc.detail
        return RepairOut(
            netlist=_netlist_rows(repaired, {}, {}, {}),
            report=None,
            reasons=list(reasons),
            changes=list(reasons),
            round=body.round,
            verified=False,
            verification_error=(
                detail if isinstance(detail, str) else str(detail)
            ),
        )


async def _parse_netlist_body(request: Request) -> Any:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - empty/invalid JSON body
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_json",
                "message": f"expected a JSON netlist in the request body: {exc}",
            },
        ) from exc
    try:
        return IRNetlist.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - surface validation plainly
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_netlist", "message": str(exc)},
        ) from exc


@app.post("/download/netlist")
async def download_netlist(request: Request) -> PlainTextResponse:
    """Return the exact SPICE text that would be simulated, as netlist.cir.

    POST only: there is no bodyless GET for this route (a GET carries
    no netlist to render). For the last checked netlist without a
    body, use GET /download/netlist.cir.
    """
    netlist = await _parse_netlist_body(request)
    try:
        spice_text = _render_spice_text(netlist)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "unrenderable_netlist", "message": str(exc)},
        ) from exc
    return PlainTextResponse(
        spice_text,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="netlist.cir"'},
    )


@app.post("/build-pack")
def build_pack(netlist: IRNetlist) -> Response:
    """PDF build pack for a circuit that passed: parts with estimated prices
    and buy links, breadboard steps, multimeter checks, PCB footprints.

    Re-runs the real check first (ngspice + rules, no LLM), so the pack's
    verdict is the server's own, never the client's. A STOP circuit gets
    no pack (409). Sync ``def``: ngspice runs in the threadpool.
    """
    from . import buildpack

    report = _report_response(_verify_netlist(netlist, record=False), netlist)
    if str(report.get("verdict", "")).upper() == "STOP":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_buildable",
                "message": "This circuit fails its check (STOP). Fix it first, for example with Repair, then get the build pack.",
            },
        )
    pdf = buildpack.render_pdf(buildpack.circuit_pack(netlist.model_dump(mode="json"), report))
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="bench-build-pack.pdf"'},
    )


@app.post("/fiber/check")
def fiber_check(body: dict) -> Any:
    """Fiber link -> optical power budget report. Deterministic, no LLM.

    Body: {"title"?, "elements": [...]}, ordered laser -> ... -> detector
    (see fiber.py for element fields and default losses).
    """
    from . import fiber

    try:
        link = fiber.FiberLink.model_validate(body)
    except Exception as exc:  # pydantic ValidationError -> 422 like other routes
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(exc).splitlines()[0]})
    return JSONResponse(content=fiber.check_link(link))


@app.post("/fiber/build-pack")
def fiber_build_pack(body: dict) -> Response:
    """PDF build pack for a fiber link that passed its power budget (409 on STOP)."""
    from . import buildpack, fiber

    try:
        link = fiber.FiberLink.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(exc).splitlines()[0]})
    report = fiber.check_link(link)
    if report["verdict"] == "STOP":
        raise HTTPException(
            status_code=409,
            detail={"error": "not_buildable", "message": "This link fails its power budget (STOP). Fix it first, then get the build pack."},
        )
    return Response(
        buildpack.render_pdf(buildpack.fiber_pack(report)),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="bench-fiber-build-pack.pdf"'},
    )


@app.post("/optics/check")
def optics_check(body: dict) -> Any:
    """Optics bench -> 2D ray trace report. Deterministic, no LLM (see optics.py)."""
    from . import optics

    try:
        bench = optics.OpticsBench.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(exc).splitlines()[0]})
    return JSONResponse(content=optics.trace_bench(bench))


@app.post("/optics/build-pack")
def optics_build_pack(body: dict) -> Response:
    """PDF build pack for an optics bench that passed its ray trace (409 on STOP)."""
    from . import buildpack, optics

    try:
        bench = optics.OpticsBench.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(exc).splitlines()[0]})
    report = optics.trace_bench(bench)
    if report["verdict"] == "STOP":
        raise HTTPException(
            status_code=409,
            detail={"error": "not_buildable", "message": "This bench fails its ray trace (STOP). Fix it first, then get the build pack."},
        )
    return Response(
        buildpack.render_pdf(buildpack.optics_pack(report)),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="bench-optics-build-pack.pdf"'},
    )


async def _design_with_ai(domain: str, goal: str | None, image: UploadFile | None) -> JSONResponse:
    """AI proposes a fiber link / optics bench, then the deterministic check decides."""
    from . import design_ai, fiber, optics

    image_bytes = None
    if image is not None:
        image_bytes = await image.read()
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail={"error": "upload_too_large", "message": f"image is over {MAX_UPLOAD_BYTES} bytes."})
        if not image_bytes:
            image_bytes = None
    if not (goal or "").strip() and not image_bytes:
        raise HTTPException(status_code=400, detail={"error": "empty_input", "message": "Describe the design or upload an image."})
    try:
        design = await asyncio.to_thread(design_ai.propose, domain, goal, image_bytes)
    except Exception as exc:  # any LLM failure is a visible 502, never a guessed design
        raise HTTPException(status_code=502, detail={"error": "llm_error", "message": f"AI design failed: {exc}"})
    report = fiber.check_link(design) if domain == "fiber" else optics.trace_bench(design)
    return JSONResponse(content={"design": design, "report": report})


def _sse(event: dict) -> dict:
    return {"data": json.dumps(event)}


async def _run_streamed(work, first_events: list[dict], wait_label: str) -> AsyncIterator[dict]:
    """Run ``work(emit)`` on a worker thread; stream its events as SSE.

    While the thread is busy and quiet (e.g. waiting on the AI), a heartbeat
    event shows the elapsed time, so the UI log never looks frozen. The
    return value of ``work`` is sent as the final ``done`` event.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    t0 = time.monotonic()

    def emit(ev: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {**ev, "t": round(time.monotonic() - t0, 1)})

    for ev in first_events:
        yield _sse({**ev, "t": 0.0})
    task = loop.run_in_executor(None, work, emit)
    while True:
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=2.0)
            yield _sse(ev)
            continue
        except asyncio.TimeoutError:
            pass
        if task.done():
            break
        yield _sse({"stage": "wait", "message": f"{wait_label} {time.monotonic() - t0:.0f}s", "tick": True,
                    "t": round(time.monotonic() - t0, 1)})
    while not queue.empty():
        yield _sse(queue.get_nowait())
    try:
        result = task.result()
    except Exception as exc:  # visible error event, never a guessed result
        yield _sse({"stage": "error", "message": str(exc)[:600], "t": round(time.monotonic() - t0, 1)})
        return
    yield _sse({"stage": "done", **result, "t": round(time.monotonic() - t0, 1)})


@app.post("/{domain}/design/stream")
async def design_stream(domain: str, goal: str | None = Form(None), image: UploadFile | None = File(None)) -> EventSourceResponse:
    """Live version of /fiber/design and /optics/design: streams each step
    (image received, AI reading, schema validated, physics check) and ends
    with {design, report, bboxes}. The AI proposes; the check decides."""
    from . import design_ai, fiber, optics

    if domain not in ("fiber", "optics"):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Unknown design domain."})
    image_bytes = await image.read() if image is not None else None
    if image_bytes is not None and len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail={"error": "upload_too_large", "message": f"image is over {MAX_UPLOAD_BYTES} bytes."})
    if not (goal or "").strip() and not image_bytes:
        raise HTTPException(status_code=400, detail={"error": "empty_input", "message": "Describe the design or upload an image."})
    first = []
    if image_bytes:
        first.append({"stage": "input", "message": f"Received {image.filename or 'image'} ({len(image_bytes) // 1024} KB)"})
    if (goal or "").strip():
        first.append({"stage": "input", "message": f"Goal: {goal.strip()[:160]}"})

    def work(emit):
        meta: dict = {}
        design = design_ai.propose_team(domain, goal, image_bytes or None, on_event=emit, meta=meta,
                                        designers=int(os.environ.get("NETPROOF_AGENTS", "4") or 4))
        emit({"stage": "verify", "agent": "Physics", "state": "working", "message": "Running the " + ("optical power budget" if domain == "fiber" else "ray trace") + " on the winner\u2026"})
        report = fiber.check_link(design) if domain == "fiber" else optics.trace_bench(design)
        emit({"stage": "verify", "agent": "Physics", "state": "done", "message": f"Verdict {report['verdict']}: " + (", ".join(f["rule_id"] for f in report["findings"]) or "no rules fired")})
        return {"design": design, "report": report, "bboxes": meta.get("bboxes") or {}, "leaderboard": meta.get("leaderboard") or []}

    return EventSourceResponse(_run_streamed(work, first, "Waiting for the AI\u2026"))


class TuneIn(BaseModel):
    design: dict
    prefs: dict = Field(default_factory=dict)


@app.post("/tune/{domain}")
async def tune(domain: str, body: TuneIn) -> EventSourceResponse:
    """Deterministic tuner (no AI): streams each step and ends with
    {design, report, changes, score_before, score_after}."""
    from . import tuner

    if domain not in ("circuit", "fiber", "optics"):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Unknown tuning domain."})
    try:
        if domain == "fiber":
            from .fiber import FiberLink
            design = FiberLink.model_validate(body.design).model_dump(mode="json", exclude_none=True)
        elif domain == "optics":
            from .optics import OpticsBench
            design = OpticsBench.model_validate(body.design).model_dump(mode="json", exclude_none=True)
        else:
            design = IRNetlist.model_validate(body.design).model_dump(mode="json")
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(exc).splitlines()[0]})

    def check_circuit(nl: dict) -> dict:
        model = IRNetlist.model_validate(nl)
        return _report_response(_verify_netlist(model, record=False), model)

    def work(raw_emit):
        def emit(ev):
            raw_emit({"agent": "Tuner", **ev})
        if domain == "circuit":
            return tuner.tune_circuit(design, body.prefs, check_circuit, emit)
        if domain == "fiber":
            return tuner.tune_fiber(design, body.prefs, emit)
        return tuner.tune_optics(design, body.prefs, emit)

    return EventSourceResponse(_run_streamed(work, [{"stage": "tune", "message": f"Tuning for: {body.prefs.get('objective') or 'default'}"}], "Tuning\u2026"))


@app.post("/fiber/design")
async def fiber_design(goal: str | None = Form(None), image: UploadFile | None = File(None)) -> JSONResponse:
    """Describe a fiber link (and/or upload a photo / figure): AI proposes, power budget decides."""
    return await _design_with_ai("fiber", goal, image)


@app.post("/optics/design")
async def optics_design(goal: str | None = Form(None), image: UploadFile | None = File(None)) -> JSONResponse:
    """Describe an optics bench (and/or upload a figure): AI proposes, ray trace decides."""
    return await _design_with_ai("optics", goal, image)


@app.post("/download/bom")
async def download_bom(request: Request) -> PlainTextResponse:
    """Return bom.csv with columns id,type,value,rating,qty.

    POST only: there is no bodyless GET for this route (a GET carries
    no netlist to render). For the last checked netlist without a
    body, use GET /download/bom.csv.
    """
    netlist = await _parse_netlist_body(request)
    bom = _require_module("bom")
    return PlainTextResponse(
        bom.netlist_to_bom_csv(netlist),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bom.csv"'},
    )


def _last_netlist_or_404(request: Request) -> Any:
    """The requesting owner's last checked netlist, or a 404 telling
    the UI to run a check first. Never another owner's netlist: a user
    who has not checked yet gets 404 even if someone else has."""
    netlist = _last_netlist_for(_owner_key(request))
    if netlist is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_netlist",
                "message": (
                    "No netlist has been checked yet: run /check, "
                    "/run-image, /run-goal or /repair first."
                ),
            },
        )
    return netlist


@app.get("/download/netlist.cir")
async def download_netlist_cir(request: Request) -> PlainTextResponse:
    """SPICE deck of the requesting user's last checked netlist (no
    request body needed).

    Serves that user's own ``_LAST_NETLIST`` entry: shared across
    that user's tabs, lost on restart (404 until the next check).
    POST /download/netlist with the current table is the reliable path.
    """
    netlist = _last_netlist_or_404(request)
    try:
        spice_text = _render_spice_text(netlist)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "unrenderable_netlist", "message": str(exc)},
        ) from exc
    return PlainTextResponse(
        spice_text,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="netlist.cir"'},
    )


@app.get("/download/bom.csv")
async def download_bom_csv(request: Request) -> PlainTextResponse:
    """BOM CSV of the requesting user's last checked netlist (no request
    body needed).

    Serves that user's own ``_LAST_NETLIST`` entry: shared across
    that user's tabs, lost on restart (404 until the next check).
    POST /download/bom with the current table is the reliable path.
    Delivered filename is ``bom.csv``, matching the POST route (it
    used to be ``BOM.csv`` here).
    """
    netlist = _last_netlist_or_404(request)
    bom = _require_module("bom")
    return PlainTextResponse(
        bom.netlist_to_bom_csv(netlist),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bom.csv"'},
    )


# ---------------------------------------------------------------------------
# Static files (web/): mounted AFTER every API route above, so the mount
# can never shadow /check, /repair, /run-goal, /run-image, /download/*,
# /health or GET "/". Serves web/index.html's dynamic import
# ./3d/main3d.js (and the rest of web/3d/) as same-origin files.
# ---------------------------------------------------------------------------
# Accounts + saved builds (accounts.py). Routes go before the static mount
# below, which would otherwise catch every path; the login gate only acts
# when AUTH_REQUIRED=1.
from . import accounts as _accounts  # noqa: E402

app.include_router(_accounts.router)


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    """Browsers must re-check page code on every visit (ETag makes that a
    cheap 304), so a deploy is never hidden behind a stale cached module."""
    response = await call_next(request)
    if request.method == "GET" and request.url.path.endswith((".js", ".css", ".html", "/")):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response
app.middleware("http")(_accounts.auth_gate)


@app.get("/login", include_in_schema=False)
async def login_page() -> Any:
    page = _PROJECT_ROOT / "web" / "login.html"
    if not page.is_file():
        page = Path.cwd() / "web" / "login.html"
    return FileResponse(page, media_type="text/html")


_WEB_DIR = next(
    (c for c in _WEB_DIR_CANDIDATES if c.is_dir()),
    _PROJECT_ROOT / "web",
)
app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")


@app.exception_handler(RequestValidationError)
async def _validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Request validation (bad Netlist / goal JSON) in the same error
    shape as every other endpoint error: {"error", "message",
    "detail"}, never FastAPI's default {"detail": [...]} shape, so
    the UI's error banner (which reads error/message) works for 422s
    too. The raw pydantic error list is kept under "detail".
    """
    errors = exc.errors()
    first = errors[0] if errors else {}
    message = str(first.get("msg", "request validation failed"))
    loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    if loc:
        message = f"{loc}: {message}"
    # Pydantic error dicts can carry non-JSON values in "ctx"
    # (e.g. a ValueError); serialise defensively so the 422 itself
    # never 500s on encoding.
    safe_errors = json.loads(json.dumps(errors, default=str))
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": message,
            "detail": safe_errors,
        },
    )


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """Every HTTP error in the flat {"error", "message"} shape.

    Dict details (the {"error", "message"} dicts the endpoints raise)
    pass through unchanged. A bare string detail becomes
    {"error": "http_error", "message": ...} instead of FastAPI's
    default {"detail": ...} wrapper, which no consumer reads. This
    replaces FastAPI's built-in HTTPException handler; without it the
    handler below (registered for Exception) would never see an
    HTTPException, because Starlette prefers the exact-class handler.
    """
    detail = exc.detail
    body = (
        detail
        if isinstance(detail, dict)
        else {"error": "http_error", "message": str(detail)}
    )
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    # HTTPException never reaches here (see _http_error above); this
    # is only for unexpected failures.
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": f"{type(exc).__name__}: {exc}",
        },
    )


if __name__ == "__main__":  # pragma: no cover - manual run helper
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
