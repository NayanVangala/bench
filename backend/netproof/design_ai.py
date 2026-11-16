"""AI proposals for fiber links and optics benches (text and/or image).

Same contract as llm.design / llm.read_schematic for circuits: the model
only PROPOSES a design as strict JSON; it is validated against the exact
models the deterministic checkers use (fiber.FiberLink, optics.OpticsBench),
with one retry on invalid output and a loud ValueError after that. The
verdict always comes from fiber.check_link / optics.trace_bench, never
from the model.
"""
from __future__ import annotations

import base64
from typing import Any, Literal

from pydantic import ValidationError

from . import llm
from .fiber import FiberLink
from .optics import OpticsBench

Domain = Literal["fiber", "optics"]

_FIBER_SCHEMA = """Return ONLY strict JSON, no prose, no markdown, no code fences:

{
  "title": "short name",
  "elements": [   // in order along the light path: first a laser, last a detector
    {"id": "L1", "type": "laser", "wavelength_nm": number, "power_dbm": number},
    {"id": "F1", "type": "fiber", "fiber_type": "smf" | "mmf", "length_m": number},
    {"id": "C1", "type": "connector", "polish_a": "PC" | "UPC" | "APC", "polish_b": "PC" | "UPC" | "APC"},
    {"id": "J1", "type": "splice", "splice_kind": "fusion" | "mechanical"},
    {"id": "S1", "type": "splitter", "ports": integer 2-128},
    {"id": "A1", "type": "attenuator", "loss_db": number},
    {"id": "I1", "type": "isolator"},
    {"id": "D1", "type": "detector", "sensitivity_dbm": number, "max_dbm": number, "min_nm": number, "max_nm": number}
  ]
}

Rules: exactly one laser (first) and one detector (last); unique ids; use only the
types above; put a connector element wherever two fibers or a fiber and a device
are mated; numbers only (no units in values). Omit optional fields you do not know."""

_OPTICS_SCHEMA = """Return ONLY strict JSON, no prose, no markdown, no code fences:

{
  "title": "short name",
  "width_mm": number (breadboard width, e.g. 600),
  "depth_mm": number (breadboard depth, e.g. 300),
  "elements": [
    {"id": "L1", "type": "laser", "x_mm": n, "y_mm": n, "angle_deg": n, "wavelength_nm": n, "power_mw": n, "beam_mm": n},
    {"id": "M1", "type": "mirror", "x_mm": n, "y_mm": n, "angle_deg": n},
    {"id": "BS1", "type": "beamsplitter", "x_mm": n, "y_mm": n, "angle_deg": n, "split": 0-1},
    {"id": "LN1", "type": "lens", "x_mm": n, "y_mm": n, "angle_deg": n, "focal_mm": n},
    {"id": "ND1", "type": "filter", "x_mm": n, "y_mm": n, "angle_deg": n, "od": n},
    {"id": "BD1", "type": "dump", "x_mm": n, "y_mm": n, "angle_deg": n},
    {"id": "D1", "type": "detector", "x_mm": n, "y_mm": n, "angle_deg": n, "max_mw": n}
  ]
}

Geometry (top view, millimetres, origin at the lower-left corner, +x right, +y up):
- A laser's angle_deg is its beam direction (0 = +x, 90 = +y).
- A mirror or beamsplitter's angle_deg is its face NORMAL. A beam travelling +x
  turns to +y off a mirror at 135; a beam travelling +y turns to +x off a mirror at 135;
  a beam travelling +x turns to -y off a mirror at 45.
- Lenses, filters, dumps and detectors face the beam: angle_deg along the beam axis.
- Place elements exactly on the beam path, on the 25 mm hole grid, inside the board.
- End every beam (including the other arm of each beamsplitter) on a detector or a dump.
Use only the types above; unique ids; numbers only."""

_RETRY = ("Your previous reply was not valid: {error}. Return ONLY strict JSON matching "
          "the schema above, with no prose, markdown or code fences.")


_BBOX_NOTE = ('\n\nFor EVERY element also include "bbox": [x0, y0, x1, y1], where that part appears '
              'in the image, as fractions of the image width/height (0-1, left, top, right, bottom).')


def _bboxes(obj: Any) -> dict:
    """id -> [x0, y0, x1, y1] (0-1) from the raw reply, before validation drops it."""
    out = {}
    for e in (obj.get("elements") or []) if isinstance(obj, dict) else []:
        b = e.get("bbox") if isinstance(e, dict) else None
        if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
            out[str(e.get("id"))] = [max(0.0, min(1.0, float(v))) for v in b]
    return out


def _validate(domain: Domain, obj: Any) -> dict:
    model = FiberLink if domain == "fiber" else OpticsBench
    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object, got {type(obj).__name__}")
    try:
        return model.model_validate(obj).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise ValueError(f"design failed validation: {exc}") from exc


def _model_label(provider: str | None) -> str:
    try:
        p = llm.get_provider(provider)
    except Exception:
        return "the AI model"
    return llm._resolve_gemini_model() if p == "gemini" else llm.MODEL


def propose(domain: Domain, goal: str | None = None, image_bytes: bytes | None = None,
            client: Any = None, provider: str | None = None,
            on_event: Any = None, meta: dict | None = None, extra: str | None = None) -> dict:
    """Ask the model for a fiber link / optics bench; return the validated design dict.

    ``on_event(dict)`` receives progress events; ``meta["bboxes"]`` gets the
    per-element image locations (image mode) for the fly-in animation.
    """
    emit = on_event or (lambda e: None)
    goal = (goal or "").strip()
    if not goal and not image_bytes:
        raise ValueError("describe the design or upload an image")
    what = "fiber-optic link" if domain == "fiber" else "free-space optics breadboard layout"
    schema = _FIBER_SCHEMA if domain == "fiber" else _OPTICS_SCHEMA
    ask = (f"Design a {what} for this goal:\n\n{goal}\n\n" if goal and not image_bytes else
           f"Transcribe the {what} shown in this image into a buildable design"
           + (f", following this note: {goal}" if goal else "") + ".\n\n")
    text = ask + "Use real, standard parts and keep it as simple as will meet the goal.\n\n" + (extra + "\n\n" if extra else "") + schema
    if image_bytes:
        text += _BBOX_NOTE
    content: Any = text
    if image_bytes:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": llm._detect_media_type(image_bytes),
                                         "data": base64.b64encode(image_bytes).decode("ascii")}},
            {"type": "text", "text": text},
        ]
    client = llm._get_client(client, provider)
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    emit({"stage": "model", "message": f"Asking {_model_label(provider)} to " + ("read the image" if image_bytes else "design it") + "\u2026"})

    def attempt(msgs: list) -> dict:
        raw = llm._extract_json(llm._call_model(client, msgs))
        design = _validate(domain, raw)
        if meta is not None:
            meta["bboxes"] = _bboxes(raw)
        emit({"stage": "parse", "message": f"Got {len(design['elements'])} elements; the design matches the schema"})
        return design

    try:
        return attempt(messages)
    except ValueError as exc:
        first = str(exc)
    emit({"stage": "retry", "message": "The first reply was not a valid design; asking once more with the error"})
    retry = messages + [{"role": "user", "content": _RETRY.format(error=first[:800])}]
    try:
        return attempt(retry)
    except ValueError as exc:
        raise ValueError(f"The AI did not return a valid {what} after one retry: {exc}") from exc


# ---------------------------------------------------------------------------
# Agent team (BEAMTEAM-style): analysts read the image in parallel, then
# several designers with different strategies each propose a design; every
# candidate is checked by the physics and the best-scoring one wins.
# The AI proposes; the deterministic check picks.
# ---------------------------------------------------------------------------

ANALYSTS = [
    ("Parts", "List EVERY component you can see, one per line, with its label (e.g. M1, L2, PD1) and type. Miss nothing."),
    ("Path", "Describe how light travels: the order of components along each path or beam, and where each path ends."),
    ("Values", "Read every number and label: wavelengths, powers, lengths, focal lengths, split ratios, connector types, sensitivities, positions."),
]
DESIGNERS = [
    ("Faithful", "Strategy: match the figure / goal as faithfully as possible."),
    ("Simple", "Strategy: use the fewest parts that still meet the goal."),
    ("Safe", "Strategy: safety first - end every beam on a detector or beam dump, keep detectors inside their input range, add attenuation / ND filters where needed."),
    ("Performance", "Strategy: deliver as much power as safely possible to the detector(s); keep losses low."),
]


def _image_block(image_bytes: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": llm._detect_media_type(image_bytes),
                                        "data": base64.b64encode(image_bytes).decode("ascii")}}


def _score(domain: Domain, design: dict, report: dict) -> float:
    from . import tuner
    if domain == "fiber":
        n_att = sum(1 for e in design["elements"] if e["type"] == "attenuator")
        return tuner._fiber_score(report, tuner._fiber_target(design, "balanced", None), n_att, "balanced")
    return tuner._optics_score(report, design, "max_power")


def propose_team(domain: Domain, goal: str | None = None, image_bytes: bytes | None = None,
                 client: Any = None, provider: str | None = None, on_event: Any = None,
                 meta: dict | None = None, designers: int = 4) -> dict:
    """Run analysts (image only) + parallel designers; return the best verified design."""
    from concurrent.futures import ThreadPoolExecutor

    from .fiber import check_link
    from .optics import trace_bench

    emit = on_event or (lambda e: None)
    client = llm._get_client(client, provider)
    label = _model_label(provider)
    notes = ""
    if image_bytes:
        emit({"stage": "model", "agent": "team", "message": f"{len(ANALYSTS)} analysts reading the image in parallel ({label})"})

        def analyse(spec):
            name, task = spec
            text = llm._call_model(client, [{"role": "user", "content": [
                _image_block(image_bytes),
                {"type": "text", "text": f"You are the {name} analyst for a {'fiber-optic link' if domain == 'fiber' else 'free-space optics bench'}. {task} Plain text, under 150 words." + (f" Goal: {goal}" if goal else "")}]}])
            emit({"stage": "parse", "agent": f"Analyst {name}", "message": " ".join(text.split())[:180]})
            return f"{name} analyst notes:\n{text.strip()}"

        with ThreadPoolExecutor(max_workers=len(ANALYSTS)) as pool:
            results = list(pool.map(lambda s: _safe(analyse, s), ANALYSTS))
        notes = "\n\n".join(r for r in results if isinstance(r, str))

    picked = DESIGNERS[:max(1, designers)]
    emit({"stage": "model", "agent": "team", "message": f"{len(picked)} designers proposing in parallel: " + ", ".join(n for n, _ in picked)})

    def design(spec):
        name, strategy = spec
        m: dict = {}
        cand = propose(domain, goal, image_bytes, client=client, meta=m,
                       extra=strategy + ("\n\n" + notes if notes else ""),
                       on_event=lambda e: emit({**e, "agent": f"Designer {name}"}) if e.get("stage") == "retry" else None)
        report = check_link(cand) if domain == "fiber" else trace_bench(cand)
        score = _score(domain, cand, report)
        emit({"stage": "verify", "agent": f"Designer {name}", "score": score,
              "message": f"{len(cand['elements'])} elements \u2192 {report['verdict']}, score {score:.1f}"})
        return {"name": name, "design": cand, "report": report, "score": score, "bboxes": m.get("bboxes") or {}}

    with ThreadPoolExecutor(max_workers=len(picked)) as pool:
        results = [r for r in pool.map(lambda s: _safe(design, s), picked) if isinstance(r, dict)]
    if not results:
        raise ValueError("Every designer failed to return a valid design.")
    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]
    board = ", ".join(f"{r['name']} {r['score']:.1f}" for r in results)
    emit({"stage": "verify", "agent": "Judge", "score": best["score"],
          "message": f"Picked Designer {best['name']} ({board})"})
    if meta is not None:
        meta["bboxes"] = best["bboxes"]
        meta["leaderboard"] = [{"name": r["name"], "score": r["score"], "verdict": r["report"]["verdict"]} for r in results]
    return best["design"]


def _safe(fn, arg):
    """One agent failing never sinks the team; its error is returned instead."""
    try:
        return fn(arg)
    except Exception as exc:  # noqa: BLE001
        return exc
