"""Free-space optics bench: a deterministic 2D ray trace + fixed rules.

The optics equivalent of the circuit check. Elements sit on an optical
breadboard (mm, 25 mm hole grid) at one beam height, so the trace is 2D
(top view). Each element is an aperture line segment of width
``aperture_mm`` centred at (x, y), facing ``angle_deg``:

* laser       -- emits along angle_deg (power_mw, wavelength_nm, beam_mm)
* mirror      -- reflects (normal = angle_deg), loses (1 - reflectivity)
* beamsplitter-- reflects ``split`` of the power and transmits the rest
* lens        -- transmits (paraxial: direction unchanged), ~4 % loss
* filter      -- transmits 10**-od (neutral density)
* dump        -- absorbs (a safe end for a beam)
* detector    -- absorbs and measures (min_mw, max_mw)

A beam that reaches the table edge without hitting a dump or detector is
a stray beam. Every verdict comes from this trace, never from the LLM.
"""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .ir import Finding, Severity

ElementType = Literal["laser", "mirror", "beamsplitter", "lens", "filter", "dump", "detector"]

DEFAULT_APERTURE_MM = {"mirror": 25.4, "beamsplitter": 12.7, "lens": 25.4, "filter": 25.4, "dump": 25.4, "detector": 10.0, "laser": 6.0}
LENS_TRANSMISSION = 0.96
STRAY_STOP_MW = 1.0          # stray beams above this are a STOP (eye hazard)
LASER_SAFETY_MW = 5.0        # Class 3B boundary
MIN_POWER_MW = 1e-6          # stop following a beam below this
MAX_BOUNCES = 40


class OpticsElement(BaseModel):
    id: str = Field(min_length=1)
    type: ElementType
    x_mm: float
    y_mm: float
    angle_deg: float = 0.0
    aperture_mm: Optional[float] = Field(default=None, gt=0)
    # laser
    wavelength_nm: Optional[float] = None
    power_mw: Optional[float] = Field(default=None, ge=0)
    beam_mm: Optional[float] = Field(default=None, gt=0)
    # mirror / beamsplitter
    reflectivity: Optional[float] = Field(default=None, ge=0, le=1)
    split: Optional[float] = Field(default=None, ge=0, le=1)
    # lens / filter
    focal_mm: Optional[float] = None
    od: Optional[float] = Field(default=None, ge=0)
    # detector
    min_mw: Optional[float] = None
    max_mw: Optional[float] = None


class OpticsBench(BaseModel):
    title: str = ""
    width_mm: float = Field(default=600, gt=0)
    depth_mm: float = Field(default=300, gt=0)
    elements: list[OpticsElement] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "OpticsBench":
        ids = [e.id for e in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("element ids must be unique")
        return self


def _unit(deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    return math.cos(r), math.sin(r)


def _aperture(e: OpticsElement) -> float:
    return e.aperture_mm or DEFAULT_APERTURE_MM[e.type]


def _hit(px, py, dx, dy, e: OpticsElement):
    """Ray (p + t d) vs the element's aperture segment -> (t, offset) or None.

    The segment is centred on the element, perpendicular to its normal
    (angle_deg); offset is the signed distance from the centre along it.
    """
    nx, ny = _unit(e.angle_deg)
    sx, sy = -ny, nx  # along the optic's face
    denom = dx * nx + dy * ny
    if abs(denom) < 1e-12:
        return None
    t = ((e.x_mm - px) * nx + (e.y_mm - py) * ny) / denom
    if t <= 1e-6:
        return None
    hx, hy = px + t * dx, py + t * dy
    off = (hx - e.x_mm) * sx + (hy - e.y_mm) * sy
    if abs(off) > _aperture(e) / 2:
        return None
    return t, off


def _edge_exit(px, py, dx, dy, w, d):
    """Distance to the table edge along the ray (table spans 0..w, 0..d)."""
    ts = []
    if dx > 0: ts.append((w - px) / dx)
    if dx < 0: ts.append(-px / dx)
    if dy > 0: ts.append((d - py) / dy)
    if dy < 0: ts.append(-py / dy)
    return min(t for t in ts if t >= 0) if ts else 0.0


def trace_bench(bench: OpticsBench | dict) -> dict:
    """Ray trace + rules -> report dict (verdict, findings, segments, ...)."""
    if isinstance(bench, dict):
        bench = OpticsBench.model_validate(bench)
    els = bench.elements
    findings: list[Finding] = []
    seen_rules: set[tuple[str, str]] = set()

    def add(sev: Severity, rule: str, ids: list[str], msg: str) -> None:
        key = (rule, ",".join(ids))
        if key in seen_rules:
            return
        seen_rules.add(key)
        findings.append(Finding(severity=sev, rule_id=rule, component_ids=ids, message=msg))

    segments: list[dict] = []
    received: dict[str, float] = {}
    hit_ids: set[str] = set()
    stray: list[dict] = []

    lasers = [e for e in els if e.type == "laser"]
    if not lasers:
        add(Severity.STOP, "BENCH_NO_LASER", [], "The bench has no laser, so there is no beam to trace.")
    for laser in lasers:
        hit_ids.add(laser.id)
        p = laser.power_mw if laser.power_mw is not None else 1.0
        if p >= LASER_SAFETY_MW:
            add(Severity.WARN, "LASER_SAFETY", [laser.id],
                f"{laser.id} emits {p:g} mW, at or above 5 mW (Class 3B): wear laser safety glasses for "
                f"{laser.wavelength_nm or 633:g} nm and keep every beam below eye level.")
        beam_r = (laser.beam_mm or 1.0) / 2
        dx, dy = _unit(laser.angle_deg)
        stack = [(laser.x_mm, laser.y_mm, dx, dy, p, laser.id, 0)]
        while stack:
            px, py, dx, dy, pw, src, depth = stack.pop()
            if pw < MIN_POWER_MW:
                continue
            if depth > MAX_BOUNCES:
                add(Severity.WARN, "BEAM_TRAPPED", [src], f"A beam keeps bouncing after {MAX_BOUNCES} reflections near {src}: check for a mirror loop.")
                continue
            best = None
            for e in els:
                if e.id == src or e.type == "laser":
                    continue
                h = _hit(px, py, dx, dy, e)
                if h and (best is None or h[0] < best[0]):
                    best = (h[0], h[1], e)
            if best is None:
                t = _edge_exit(px, py, dx, dy, bench.width_mm, bench.depth_mm)
                ex, ey = px + t * dx, py + t * dy
                segments.append({"from": [px, py], "to": [ex, ey], "power_mw": pw, "source": src, "end": "edge"})
                stray.append({"from": src, "power_mw": pw, "at": [round(ex, 1), round(ey, 1)]})
                continue
            t, off, e = best
            hx, hy = px + t * dx, py + t * dy
            segments.append({"from": [px, py], "to": [hx, hy], "power_mw": pw, "source": src, "end": e.id})
            hit_ids.add(e.id)
            if abs(off) + beam_r > _aperture(e) / 2:
                add(Severity.WARN, "BEAM_CLIPPING", [e.id],
                    f"The beam hits {e.id} {abs(off):.1f} mm off centre, so part of the {beam_r * 2:g} mm beam falls off its "
                    f"{_aperture(e):g} mm aperture: move or re-angle the element upstream.")
            if e.type in ("mirror", "beamsplitter"):
                nx, ny = _unit(e.angle_deg)
                k = 2 * (dx * nx + dy * ny)
                rx, ry = dx - k * nx, dy - k * ny
                if e.type == "mirror":
                    stack.append((hx, hy, rx, ry, pw * (e.reflectivity if e.reflectivity is not None else 0.99), e.id, depth + 1))
                else:
                    r = e.split if e.split is not None else 0.5
                    stack.append((hx, hy, rx, ry, pw * r, e.id, depth + 1))
                    stack.append((hx, hy, dx, dy, pw * (1 - r) * 0.98, e.id, depth + 1))
            elif e.type == "lens":
                stack.append((hx, hy, dx, dy, pw * LENS_TRANSMISSION, e.id, depth + 1))
            elif e.type == "filter":
                stack.append((hx, hy, dx, dy, pw * 10 ** -(e.od if e.od is not None else 1.0), e.id, depth + 1))
            elif e.type == "detector":
                received[e.id] = received.get(e.id, 0.0) + pw
            # dump: absorbed

    for s in stray:
        sev = Severity.STOP if s["power_mw"] >= STRAY_STOP_MW else Severity.WARN
        add(sev, "STRAY_BEAM", [s["from"]],
            f"A {s['power_mw']:.3g} mW beam from {s['from']} leaves the table at ({s['at'][0]:g}, {s['at'][1]:g}) mm: "
            f"end it on a beam dump or detector.")

    for d in (e for e in els if e.type == "detector"):
        got = received.get(d.id, 0.0)
        mx = d.max_mw if d.max_mw is not None else 10.0
        mn = d.min_mw if d.min_mw is not None else 0.001
        if got <= 0:
            add(Severity.STOP, "DETECTOR_NO_LIGHT", [d.id], f"No beam reaches {d.id}: check the angles of the optics before it.")
        elif got > mx:
            add(Severity.STOP, "DETECTOR_OVERLOAD", [d.id], f"{d.id} receives {got:.3g} mW, over its {mx:g} mW maximum: add an ND filter.")
        elif got < mn:
            add(Severity.WARN, "DETECTOR_LOW_POWER", [d.id], f"{d.id} receives {got:.3g} mW, below its {mn:g} mW useful minimum.")

    for e in els:
        if e.id not in hit_ids:
            add(Severity.WARN, "NOT_IN_BEAM", [e.id], f"No beam reaches {e.id} ({e.type}): it is out of alignment or not needed.")

    order = {Severity.STOP: 0, Severity.WARN: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: order[f.severity])
    verdict = ("STOP" if any(f.severity is Severity.STOP for f in findings)
               else "WARN" if any(f.severity is Severity.WARN for f in findings) else "PASS")
    return {
        "domain": "optics",
        "verdict": verdict,
        "findings": [f.model_dump(mode="json") for f in findings],
        "segments": [{**s, "from": [round(v, 2) for v in s["from"]], "to": [round(v, 2) for v in s["to"]],
                      "power_mw": round(s["power_mw"], 6)} for s in segments],
        "received_mw": {k: round(v, 6) for k, v in received.items()},
        "stray_beams": len(stray),
        "wavelength_nm": lasers[0].wavelength_nm if lasers and lasers[0].wavelength_nm else 633.0,
        "bench": bench.model_dump(mode="json"),
    }
