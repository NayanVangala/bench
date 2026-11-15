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
                calls (``client=None``) need the Anthropic SDK + key.
    bom.py    - ``netlist_to_bom_csv(netlist) -> str`` (header
                ``id,type,value,rating,qty``; groups by type+value).
    parts.py  - part limits table (used by rules/spice, not by the server).
    pipeline.py - Exists (landed mid-task) but the server does NOT depend
                on it: ``/check`` composes ``spice`` + ``rules`` directly,
                and the streaming endpoints compose ``llm`` + the same
                check. Delegating to pipeline later is optional, not
                required; the server keeps working unchanged either way.

Endpoints::

    GET  /                    serve web/index.html
    Static files: web/ is mounted at "/" AFTER all API routes, so
    /3d/main3d.js and the rest of web/3d/ are served same-origin
    without shadowing any API route.
    GET  /health              {"status","ngspice","ngspice_path",
                              "simulator_override","modules"}
    POST /check               JSON Netlist -> report dict {verdict,
                              findings, netlist, mock} (NO LLM, ever)
    POST /run-image           multipart image -> SSE events, then Report
    POST /run-goal            JSON {"goal": str} -> SSE events, then Report
    POST /repair              JSON {"netlist","findings","round"} ->
                              repaired netlist rows + changes + round +
                              verdict + findings (via llm.repair + verify)
    POST /download/netlist    JSON Netlist -> netlist.cir (text/plain,
    GET  /download/netlist    the exact SPICE text that was simulated)
    GET  /download/netlist.cir  same, for the last checked netlist
    POST /download/bom        JSON Netlist -> bom.csv
    GET  /download/bom        (id,type,value,rating,qty)
    GET  /download/bom.csv     same, for the last checked netlist

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
Invalid netlist JSON -> HTTP 422 via ir.py validation.

Mock mode: ``?mock=1`` ONLY, on ``/check``, ``/run-image`` and
``/run-goal``. Every mock event and mock Report carries ``mock=true`` so
the UI can show a MOCK badge. ``/repair`` does not support mock (a
fabricated repair would be a lie).

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
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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
    "ngspice was not found on PATH. Netproof refuses to start without it: "
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
    ``netproof.server`` or loaded standalone. Failures are cached and
    recorded in ``_module_error`` for /health and 501 messages.
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
    _module_cache[name] = module
    if module is None:
        _module_error[name] = error or "module not found"
    return module


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


class RepairOut(BaseModel):
    """Repair result, in the shape web/index.html consumes.

    ``netlist`` is the repaired netlist as editable table rows (same
    shape as /check), ``changes`` is one reason per changed component,
    ``round`` echoes the request round, and ``verdict`` / ``findings``
    are the fresh verification results. ``reasons`` is kept as an alias
    of ``changes`` and ``report`` carries the full shaped report.
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
# and GET /download/bom.csv, which take no request body.
_LAST_NETLIST: Any = None


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


def _verify_netlist(netlist: Any) -> Any:
    """Full check of a netlist: ERC, then simulate + rules, -> ir.Report.

    If pre-simulation checks already contain a STOP finding, simulation
    is skipped (a degenerate deck must not burn a 30 s ngspice timeout)
    and the report is built from the ERC findings alone. No LLM is
    involved on this path, ever.
    """
    rules = _require_module("rules")
    global _LAST_NETLIST
    _LAST_NETLIST = netlist
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
    # rules.py lowercases branch-current keys ("v1" for "V1"); look both
    # maps up case-insensitively so the current/power columns fill in.
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
        if (
            current is None
            and str(ctype).lower() == "resistor"
            and value
            and len(nodes) >= 2
        ):
            v0 = node_voltages.get(nodes[0])
            v1 = node_voltages.get(nodes[1])
            if v0 is not None and v1 is not None:
                current = (v0 - v1) / value
        rows.append(
            {
                "id": cid,
                "type": ctype,
                "value": value,
                "nodes": nodes,
                "node_voltage": node_voltages.get(nodes[0]) if nodes else None,
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


def _mock_report(netlist: Any, *, summary: str) -> dict:
    """A mock ir.Report as a plain dict, plus mock=true for the UI."""
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
    data["summary"] = summary
    return data


async def _mock_event_stream(
    kind: str, *, detail: dict, report: dict
) -> AsyncIterator[dict]:
    stages = (
        ["started", "vision_extract", "spice_render", "simulate", "rules_check"]
        if kind == "image"
        else ["started", "plan", "generate", "spice_render", "simulate", "rules_check"]
    )
    for stage in stages:
        yield {
            "event": "progress",
            "data": {"stage": stage, "mock": True, **detail},
        }
    yield {"event": "result", "data": {**report, "mock": True}}


# ---------------------------------------------------------------------------
# Streaming runs (llm + check, composed here; no pipeline module needed)
# ---------------------------------------------------------------------------


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


async def _run_stream(
    kind: str,
    *,
    mock: bool,
    image_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    goal: Optional[str] = None,
) -> AsyncIterator[dict]:
    """Yield SSE-ready event dicts for /run-image or /run-goal."""
    if mock:
        ir_mod = _load_sibling("ir")
        empty = ir_mod.Netlist(title="mock") if ir_mod is not None else {"title": "mock", "components": []}
        detail = {"filename": filename} if kind == "image" else {"goal": goal}
        report = _mock_report(empty, summary=f"Mock {kind} run: " + _MOCK_MESSAGE)
        async for event in _mock_event_stream(kind, detail=detail, report=report):
            yield _sse_event(event)
        return

    llm = _require_module("llm")
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
    try:
        if kind == "image":
            yield _sse_event(
                {
                    "event": "progress",
                    "data": {
                        "agent": "reader",
                        "message": f"Reading schematic {filename}...",
                        "stage": "vision_extract",
                        "filename": filename,
                        "mock": False,
                    },
                }
            )
            netlist = await asyncio.to_thread(llm.read_schematic, image_bytes)
        else:
            yield _sse_event(
                {
                    "event": "progress",
                    "data": {
                        "agent": "designer",
                        "message": f"Designing circuit for goal: {goal}",
                        "stage": "plan",
                        "goal": goal,
                        "mock": False,
                    },
                }
            )
            netlist = await asyncio.to_thread(llm.design, goal)
    except Exception as exc:  # noqa: BLE001 - ANY llm failure is a 502/400 event
        yield _sse_event(_llm_error_event(exc))
        return

    yield _sse_event(
        {
            "event": "progress",
            "data": {
                "agent": "simulator",
                "message": "Netlist ready; rendering SPICE deck.",
                "stage": "spice_render",
                "netlist": netlist.model_dump(mode="json"),
                "mock": False,
            },
        }
    )
    yield _sse_event(
        {
            "event": "progress",
            "data": {
                "agent": "simulator",
                "message": "Running ngspice DC operating point...",
                "stage": "simulate",
                "mock": False,
            },
        }
    )
    try:
        report = await asyncio.to_thread(_verify_netlist, netlist)
    except HTTPException as exc:
        detail = exc.detail
        yield _sse_event(
            {
                "event": "error",
                "data": {
                    "error": "verification_failed",
                    "http_status": exc.status_code,
                    "detail": detail if isinstance(detail, str) else str(detail),
                    "mock": False,
                },
            }
        )
        return
    yield _sse_event(
        {
            "event": "progress",
            "data": {
                "agent": "checker",
                "message": "Checking rules...",
                "stage": "rules_check",
                "mock": False,
            },
        }
    )
    # Final event: verdict/findings/netlist rows (+ mock: false). No
    # "done" flag: the web UI applies a result event, then treats a
    # done-flagged event (or stream end) as the terminator.
    report_data = _report_response(report, netlist)
    yield _sse_event({"event": "result", "data": report_data})


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


app = FastAPI(title="Netproof", version="0.1.0", lifespan=_lifespan)


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
    return {
        "status": "ok",
        "ngspice": _ngspice_available(),
        "ngspice_path": shutil.which("ngspice"),
        "simulator_override": _SIMULATOR_OVERRIDE is not None,
        "modules": _sibling_status(),
    }


@app.post("/check")
def check(netlist: IRNetlist, mock: bool = Query(False)) -> Any:
    """Check a netlist JSON -> report dict. NO LLM is ever called here.

    Sync ``def`` so FastAPI runs the (blocking, up to 30 s) ngspice call
    in its threadpool instead of blocking the event loop.
    """
    global _LAST_NETLIST
    if _ir_mod is None:
        raise HTTPException(
            status_code=501,
            detail={"error": "module_unavailable", "message": "ir.py is not present.", "module": "ir"},
        )
    if mock:
        _LAST_NETLIST = netlist
        try:
            spice_text = _render_spice_text(netlist)
        except HTTPException:
            spice_text = None
        report = _mock_report(netlist, summary="Mock check: no simulation ran.")
        if spice_text is not None:
            report["spice"] = spice_text
        report["netlist"] = _netlist_rows(netlist, {}, {}, {})
        return JSONResponse(content=report)
    return JSONResponse(content=_report_response(_verify_netlist(netlist), netlist))


@app.post("/run-image")
async def run_image(
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
    # Fail fast (before headers stream) if the LLM module is missing and
    # this is not a mock run, so the client gets a clean HTTP 501
    # instead of an SSE error frame.
    if not mock:
        _require_module("llm")

    async def _events() -> AsyncIterator[dict]:
        async for event in _run_stream(
            "image",
            mock=mock,
            image_bytes=image_bytes,
            filename=image.filename or "upload",
        ):
            yield event

    return EventSourceResponse(_events())


@app.post("/run-goal")
async def run_goal(body: GoalIn, mock: bool = Query(False)) -> EventSourceResponse:
    """JSON {"goal": str} -> SSE stream of pipeline events, then Report."""
    if not mock:
        _require_module("llm")

    async def _events() -> AsyncIterator[dict]:
        async for event in _run_stream("goal", mock=mock, goal=body.goal):
            yield event

    return EventSourceResponse(_events())


@app.post("/repair", response_model=RepairOut)
def repair(body: RepairIn) -> RepairOut:
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
        report = _verify_netlist(repaired)
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
@app.get("/download/netlist")
async def download_netlist(request: Request) -> PlainTextResponse:
    """Return the exact SPICE text that would be simulated, as netlist.cir."""
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


@app.post("/download/bom")
@app.get("/download/bom")
async def download_bom(request: Request) -> PlainTextResponse:
    """Return bom.csv with columns id,type,value,rating,qty."""
    netlist = await _parse_netlist_body(request)
    bom = _require_module("bom")
    return PlainTextResponse(
        bom.netlist_to_bom_csv(netlist),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bom.csv"'},
    )


def _last_netlist_or_404() -> Any:
    """Last checked netlist, or a 404 telling the UI to run a check first."""
    if _LAST_NETLIST is None:
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
    return _LAST_NETLIST


@app.get("/download/netlist.cir")
async def download_netlist_cir() -> PlainTextResponse:
    """SPICE deck of the last checked netlist (no request body needed)."""
    netlist = _last_netlist_or_404()
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
async def download_bom_csv() -> PlainTextResponse:
    """BOM CSV of the last checked netlist (no request body needed)."""
    netlist = _last_netlist_or_404()
    bom = _require_module("bom")
    return PlainTextResponse(
        bom.netlist_to_bom_csv(netlist),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="BOM.csv"'},
    )


# ---------------------------------------------------------------------------
# Static files (web/): mounted AFTER every API route above, so the mount
# can never shadow /check, /repair, /run-goal, /run-image, /download/*,
# /health or GET "/". Serves web/index.html's dynamic import
# ./3d/main3d.js (and the rest of web/3d/) as same-origin files.
# ---------------------------------------------------------------------------
_WEB_DIR = next(
    (c for c in _WEB_DIR_CANDIDATES if c.is_dir()),
    _PROJECT_ROOT / "web",
)
app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        body = detail if isinstance(detail, dict) else {"detail": detail}
        return JSONResponse(status_code=exc.status_code, content=body)
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
