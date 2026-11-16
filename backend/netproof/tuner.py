"""Deterministic tuners: make a checked design better for the user's preferences.

Modelled on BEAMTEAM's alignment tuner (coordinate search over physical
knobs, a step is kept only if it strictly improves the objective, and the
objective penalises errors so the search never "dims the light" to hide a
problem). No LLM: every candidate is re-checked by the same physics as the
verdict (ngspice + rules, the fiber power budget, the optics ray trace).

Each ``tune_*`` takes the design, a preferences dict and an ``emit(event)``
callback for live progress, and returns
``{"design", "report", "changes", "score_before", "score_after"}``.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Callable

from . import fiber as fiber_mod
from . import optics as optics_mod
from .parts import format_si

Emit = Callable[[dict], None]

E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
RESISTOR_VALUES = [round(m * d, 6) for d in (10, 100, 1000, 10000) for m in E12] + [100000.0]
RATINGS_W = [0.25, 0.5, 1.0, 2.0, 5.0]


def _counts(report: dict) -> tuple[int, int]:
    f = report.get("findings") or []
    return (sum(1 for x in f if x.get("severity") == "STOP"), sum(1 for x in f if x.get("severity") == "WARN"))


# ---------------------------------------------------------------------------
# Circuit: pick E12 resistor values (and ratings) for a target LED current
# ---------------------------------------------------------------------------

CIRCUIT_TARGET_MA = {"balanced": 12.0, "bright": 18.0, "battery": 5.0, "cheap": 10.0}


def _led_currents(report: dict) -> list[float]:
    return [abs(r["current"]) for r in (report.get("netlist") or [])
            if str(r.get("type")).lower() in ("led", "diode") and r.get("current") is not None]


def _circuit_score(report: dict, target_a: float) -> float:
    stops, warns = _counts(report)
    leds = _led_currents(report)
    miss = sum(abs(i - target_a) / target_a for i in leds) / len(leds) if leds else 0.0
    return round(95 - 25 * stops - 5 * warns - 40 * miss, 2)


def tune_circuit(netlist: dict, prefs: dict, check: Callable[[dict], dict], emit: Emit) -> dict:
    objective = prefs.get("objective") or "balanced"
    target_a = float(prefs.get("target_ma") or CIRCUIT_TARGET_MA.get(objective, 12.0)) / 1000
    best = copy.deepcopy(netlist)
    best_report = check(best)
    start = best_score = _circuit_score(best_report, target_a)
    emit({"stage": "tune", "message": f"Start: score {start:.1f}, LED target {target_a * 1000:g} mA ({objective})", "score": start})
    # Only resistors that share a node with an LED/diode set its current;
    # simulating every value for the rest would change nothing.
    led_nodes = {n for c in best["components"] if c["type"] in ("led", "diode") for n in c["nodes"]}
    resistors = [c for c in best["components"] if c["type"] == "resistor" and led_nodes & set(c["nodes"])]
    if not resistors:
        emit({"stage": "tune", "message": "No LED or diode current to tune; checking resistor power ratings only."})
    tried = 0
    for c in resistors:
        idx = next(i for i, x in enumerate(best["components"]) if x["id"] == c["id"])
        for value in RESISTOR_VALUES:
            cand = copy.deepcopy(best)
            cand["components"][idx]["value"] = value
            rep = check(cand)
            tried += 1
            s = _circuit_score(rep, target_a)
            if s > best_score + 1e-9:
                best, best_report, best_score = cand, rep, s
                emit({"stage": "tune", "message": f"{c['id']} = {format_si(value, chr(0x3a9))}: score {s:.1f}", "score": s, "step": tried})
            elif tried % 12 == 0:
                emit({"stage": "tune", "message": f"Tried {tried} values…", "score": best_score, "step": tried, "tick": True})
    # Rating: smallest standard rating that keeps each resistor under 50 % load.
    power = {k.lower(): v for k, v in (best_report.get("power") or {}).items()}
    for comp in best["components"]:
        if comp["type"] != "resistor":
            continue
        p = abs(power.get(comp["id"].lower(), 0.0))
        rating = next((r for r in RATINGS_W if r >= 2 * p), RATINGS_W[-1])
        if rating != (comp.get("rating_watts") or 0.25):
            comp["rating_watts"] = rating
    final = check(best)
    changes = []
    before = {c["id"]: c for c in netlist["components"]}
    for c in best["components"]:
        b = before.get(c["id"], {})
        if c["type"] == "resistor" and b.get("value") != c["value"]:
            changes.append(f"{c['id']}: {format_si(b.get('value', 0), chr(0x3a9))} → {format_si(c['value'], chr(0x3a9))}")
        if c["type"] == "resistor" and (b.get("rating_watts") or 0.25) != (c.get("rating_watts") or 0.25):
            changes.append(f"{c['id']} rating: {(b.get('rating_watts') or 0.25):g} W → {c['rating_watts']:g} W")
    end = _circuit_score(final, target_a)
    emit({"stage": "tune", "message": f"Done: score {start:.1f} → {end:.1f} after {tried} simulations", "score": end})
    return {"design": best, "report": final, "changes": changes, "score_before": start, "score_after": end}


# ---------------------------------------------------------------------------
# Fiber: fix connector polish and size an attenuator for the target power
# ---------------------------------------------------------------------------

ATTENUATIONS_DB = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30]


def _fiber_target(link: dict, objective: str, target_dbm: Any) -> float:
    if target_dbm is not None and target_dbm != "":
        return float(target_dbm)
    det = fiber_mod._detector(fiber_mod.FiberElement.model_validate(link["elements"][-1])) \
        if link["elements"] and link["elements"][-1]["type"] == "detector" else None
    sens = det.sensitivity_dbm if det else -40.0
    mx = det.max_dbm if det else 3.0
    if objective == "max_margin":
        return mx - 3.0
    return (sens + 3.0 + mx - 3.0) / 2  # middle of the safe window


def _fiber_score(report: dict, target: float, n_atten: int, objective: str) -> float:
    stops, warns = _counts(report)
    rx = report.get("received_dbm")
    miss = abs(rx - target) if rx is not None else 50.0
    cost = (2.0 if objective == "cheap" else 0.3) * n_atten
    return round(95 - 25 * stops - 5 * warns - 1.5 * miss - cost, 2)


def tune_fiber(link: dict, prefs: dict, emit: Emit) -> dict:
    objective = prefs.get("objective") or "balanced"
    target = _fiber_target(link, objective, prefs.get("target_dbm"))
    base = copy.deepcopy(link)
    changes = []
    for e in base["elements"]:
        if e["type"] == "connector" and e.get("polish_a") and e.get("polish_b") and (e["polish_a"] == "APC") != (e["polish_b"] == "APC"):
            changes.append(f"{e['id']}: {e['polish_a']}/{e['polish_b']} → {e['polish_a']}/{e['polish_a']} (matched polish)")
            e["polish_b"] = e["polish_a"]
            emit({"stage": "tune", "message": f"Matched connector polish at {e['id']}"})
    start_report = fiber_mod.check_link(link)
    start = _fiber_score(start_report, target, sum(1 for e in link["elements"] if e["type"] == "attenuator"), objective)
    emit({"stage": "tune", "message": f"Start: score {start:.1f}, received {start_report['received_dbm']:.1f} dBm, target {target:.1f} dBm ({objective})", "score": start})
    stripped = copy.deepcopy(base)
    stripped["elements"] = [e for e in stripped["elements"] if e["type"] != "attenuator"]
    best, best_report, best_score = None, None, -1e9
    for db in ATTENUATIONS_DB:
        cand = copy.deepcopy(stripped)
        if db:
            used = {e["id"] for e in cand["elements"]}
            aid = next(f"A{k}" for k in range(1, 99) if f"A{k}" not in used)
            cand["elements"].insert(len(cand["elements"]) - 1, {"id": aid, "type": "attenuator", "loss_db": db})
        rep = fiber_mod.check_link(cand)
        s = _fiber_score(rep, target, 1 if db else 0, objective)
        emit({"stage": "tune", "message": f"{db:g} dB attenuation → {rep['received_dbm']:.1f} dBm, score {s:.1f}", "score": max(s, best_score), "tick": s <= best_score})
        if s > best_score + 1e-9:
            best, best_report, best_score = cand, rep, s
    before_att = sum(e.get("loss_db") or 10 for e in link["elements"] if e["type"] == "attenuator")
    after_att = sum(e.get("loss_db") or 0 for e in best["elements"] if e["type"] == "attenuator")
    if before_att != after_att:
        changes.append(f"Attenuation: {before_att:g} dB → {after_att:g} dB")
    emit({"stage": "tune", "message": f"Done: score {start:.1f} → {best_score:.1f}, received {best_report['received_dbm']:.1f} dBm", "score": best_score})
    return {"design": best, "report": best_report, "changes": changes, "score_before": start, "score_after": best_score}


# ---------------------------------------------------------------------------
# Optics: autofix stray beams, ND for overloads, then coordinate search
# ---------------------------------------------------------------------------

def _optics_score(report: dict, bench: dict, objective: str) -> float:
    stops, warns = _counts(report)
    p_in = sum((e.get("power_mw") or 1.0) for e in bench["elements"] if e["type"] == "laser") or 1.0
    got = list((report.get("received_mw") or {}).values())
    frac = sum(got) / p_in
    # BEAMTEAM-style 0-100 score; "max_power" weights delivered power more.
    s = (75 + 25 * frac if objective == "max_power" else 95 + 5 * frac) - 25 * stops - 5 * warns
    if objective == "balanced_arms" and len(got) > 1:
        s -= 20 * (max(got) - min(got)) / (max(got) or 1)
    elif objective == "fewest_parts":
        s -= 2 * sum(1 for e in bench["elements"] if e["type"] in ("dump", "filter"))
    return round(max(0.0, min(100.0, s)), 2)


def _new_id(bench: dict, prefix: str) -> str:
    used = {e["id"] for e in bench["elements"]}
    return next(f"{prefix}{k}" for k in range(1, 999) if f"{prefix}{k}" not in used)


def autofix_optics(bench: dict, emit: Emit) -> tuple[dict, list[str]]:
    """End stray beams on dumps just inside the table edge; ND filters for overloads."""
    b = copy.deepcopy(bench)
    changes = []
    for _ in range(6):
        rep = optics_mod.trace_bench(b)
        fixed = False
        for seg in rep["segments"]:
            if seg["end"] != "edge" or seg["power_mw"] < 0.05:
                continue
            (x0, y0), (x1, y1) = seg["from"], seg["to"]
            length = math.hypot(x1 - x0, y1 - y0)
            if length < 30:
                continue
            ux, uy = (x1 - x0) / length, (y1 - y0) / length
            did = _new_id(b, "BD")
            dump = {"id": did, "type": "dump", "x_mm": round(x1 - 20 * ux, 1), "y_mm": round(y1 - 20 * uy, 1),
                    "angle_deg": round(math.degrees(math.atan2(uy, ux)), 1)}
            trial = copy.deepcopy(b)
            trial["elements"].append(dump)
            if _counts(optics_mod.trace_bench(trial))[0] <= _counts(rep)[0]:
                b = trial
                changes.append(f"Added beam dump {did} for the stray beam from {seg['source']}")
                emit({"stage": "autofix", "message": f"Stray {seg['power_mw']:.2g} mW beam from {seg['source']}: added dump {did}"})
                fixed = True
                break
        if not fixed:
            break
    rep = optics_mod.trace_bench(b)
    for f in rep["findings"]:
        if f["rule_id"] != "DETECTOR_OVERLOAD":
            continue
        did = f["component_ids"][0]
        det = next(e for e in b["elements"] if e["id"] == did)
        seg = next((s for s in rep["segments"] if s["end"] == did), None)
        if not seg:
            continue
        (x0, y0), (x1, y1) = seg["from"], seg["to"]
        length = math.hypot(x1 - x0, y1 - y0) or 1
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        need = rep["received_mw"][did] / ((det.get("max_mw") or 10) / 2)
        od = next((o for o in (0.5, 1.0, 2.0, 3.0, 4.0) if 10 ** o >= need), 4.0)
        nid = _new_id(b, "ND")
        b["elements"].append({"id": nid, "type": "filter", "x_mm": round(x1 - min(40, length / 2) * ux, 1),
                              "y_mm": round(y1 - min(40, length / 2) * uy, 1), "angle_deg": round(math.degrees(math.atan2(uy, ux)), 1), "od": od})
        changes.append(f"Added ND filter {nid} (OD {od:g}) before {did}")
        emit({"stage": "autofix", "message": f"{did} overloaded: added ND filter {nid}, OD {od:g}"})
    return b, changes


def tune_optics(bench: dict, prefs: dict, emit: Emit) -> dict:
    objective = prefs.get("objective") or "max_power"
    start_report = optics_mod.trace_bench(bench)
    start = _optics_score(start_report, bench, objective)
    emit({"stage": "tune", "message": f"Start: score {start:.1f} ({objective})", "score": start})
    best, changes = autofix_optics(bench, emit)
    best_report = optics_mod.trace_bench(best)
    best_score = _optics_score(best_report, best, objective)
    emit({"stage": "tune", "message": f"After autofix: score {best_score:.1f}", "score": best_score})
    # Knobs in beam order: mirror / beamsplitter angles (coarse then fine), splits.
    order = []
    for s in best_report["segments"]:
        e = next((x for x in best["elements"] if x["id"] == s["end"]), None)
        if e and e["type"] in ("mirror", "beamsplitter") and e["id"] not in order:
            order.append(e["id"])
    steps = 0
    for eid in order:
        for deltas in ([-4, -3, -2, -1, 1, 2, 3, 4], [-0.6, -0.3, 0.3, 0.6]):
            improved = True
            while improved:
                improved = False
                idx = next(i for i, e in enumerate(best["elements"]) if e["id"] == eid)
                for d in deltas:
                    cand = copy.deepcopy(best)
                    cand["elements"][idx]["angle_deg"] = round((cand["elements"][idx].get("angle_deg") or 0) + d, 2)
                    rep = optics_mod.trace_bench(cand)
                    s = _optics_score(rep, cand, objective)
                    steps += 1
                    if s > best_score + 1e-9:
                        best, best_report, best_score, improved = cand, rep, s, True
                        emit({"stage": "tune", "message": f"{eid} angle {cand['elements'][idx]['angle_deg']:g}°: score {s:.1f}", "score": s, "step": steps})
                        break
        e = next(x for x in best["elements"] if x["id"] == eid)
        if e["type"] == "beamsplitter" and objective == "balanced_arms":
            idx = best["elements"].index(e)
            for split in [0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7]:
                cand = copy.deepcopy(best)
                cand["elements"][idx]["split"] = split
                rep = optics_mod.trace_bench(cand)
                s = _optics_score(rep, cand, objective)
                steps += 1
                if s > best_score + 1e-9:
                    best, best_report, best_score = cand, rep, s
                    emit({"stage": "tune", "message": f"{eid} split {split:g}: score {s:.1f}", "score": s, "step": steps})
    before = {e["id"]: e for e in bench["elements"]}
    for e in best["elements"]:
        b = before.get(e["id"])
        if b and (b.get("angle_deg") or 0) != (e.get("angle_deg") or 0):
            changes.append(f"{e['id']} angle: {b.get('angle_deg') or 0:g}° → {e['angle_deg']:g}°")
        if b and b.get("split") != e.get("split") and e["type"] == "beamsplitter":
            changes.append(f"{e['id']} split: {b.get('split') or 0.5:g} → {e['split']:g}")
    emit({"stage": "tune", "message": f"Done: score {start:.1f} → {best_score:.1f} after {steps} traces", "score": best_score})
    return {"design": best, "report": best_report, "changes": changes, "score_before": start, "score_after": best_score}
