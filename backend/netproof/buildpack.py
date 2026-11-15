"""Build pack: everything a user needs once a circuit has passed its check.

Pure functions over a netlist (``ir.Netlist`` dump: components with id,
type, value, nodes, rating_watts, color) and the server's check report
(verdict, findings, node_voltages, branch_currents, power). No LLM.

* :func:`line_items` / :func:`tool_items` -- bill of materials with
  typical single-unit prices (USD, estimates as of ``PRICES_AS_OF``) and
  distributor search links. Prices are a curated table, never generated.
* :func:`build_steps` -- breadboard steps derived from the netlist
  (each node becomes a breadboard row or rail).
* :func:`test_steps` -- multimeter checks using the simulated values.
* :func:`pcb_footprints` -- KiCad footprints for ordering a real PCB.
* :func:`render_pdf` -- all of the above as one PDF (fpdf2).

The pack's verdict always comes from the server's own ngspice check
(see server.py ``/build-pack``), never from the client.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

from .parts import format_si

PRICES_AS_OF = "November 2026"
GROUND_NAMES = {"0", "gnd"}

# ---------------------------------------------------------------------------
# Catalog: typical single-unit hobby prices (USD). Estimates, not quotes.
# ---------------------------------------------------------------------------

_RESISTOR_PRICES = [(0.25, 0.10), (0.5, 0.15), (1.0, 0.30), (2.0, 0.50), (5.0, 1.20)]

_TOOLS = [
    # (name, spec, unit_usd, search query, note)
    ("Solderless breadboard", "830 tie-points", 6.00, "830 point solderless breadboard", ""),
    ("Jumper wire kit", "male-to-male, assorted", 6.00, "breadboard jumper wire kit male to male", ""),
    ("Digital multimeter", "DC V + DC mA, for section 04", 15.00, "digital multimeter", ""),
]


def buy_links(query: str, fiber: bool = False) -> list[tuple[str, str]]:
    q = quote_plus(query)
    if fiber:  # fiber parts: optics vendors instead of electronics distributors
        return [
            ("Thorlabs", f"https://www.thorlabs.com/search/thorsearch.cfm?search={q}"),
            ("FS.com", f"https://www.fs.com/search_result?keyword={q}"),
            ("Amazon", f"https://www.amazon.com/s?k={q}"),
        ]
    return [
        ("DigiKey", f"https://www.digikey.com/en/products/result?keywords={q}"),
        ("Mouser", f"https://www.mouser.com/c/?q={q}"),
        ("Amazon", f"https://www.amazon.com/s?k={q}"),
    ]


def _is_ground(node: str) -> bool:
    return str(node).strip().lower() in GROUND_NAMES


def _rating(comp: dict) -> float:
    r = comp.get("rating_watts")
    return float(r) if r else 0.25


def _resistor_item(comp: dict) -> dict:
    need = _rating(comp)
    watts, price = next(((w, p) for w, p in _RESISTOR_PRICES if w >= need), _RESISTOR_PRICES[-1])
    value = format_si(comp["value"], "Ω")
    watt_label = {0.25: "1/4 W", 0.5: "1/2 W"}.get(watts, f"{watts:g} W")
    return {
        "name": "Resistor", "spec": f"{value}, {watt_label}, through-hole",
        "unit_usd": price, "query": f"{value.replace(chr(0x3a9), 'ohm')} {watt_label} resistor through hole",
    }


def _source_items(comp: dict) -> list[dict]:
    v = float(comp["value"])
    vl = format_si(v, "V")
    if abs(v - 9) < 0.05:
        return [
            {"name": "9 V battery", "spec": "PP3 alkaline", "unit_usd": 2.50, "query": "9V alkaline battery"},
            {"name": "9 V battery snap", "spec": "with red/black leads", "unit_usd": 0.75, "query": "9V battery snap connector leads"},
        ]
    for cells, volts in ((1, 1.5), (2, 3.0), (3, 4.5), (4, 6.0)):
        if abs(v - volts) < 0.05:
            return [
                {"name": "AA battery", "spec": "alkaline 1.5 V", "unit_usd": 0.75, "query": "AA alkaline battery", "qty": cells},
                {"name": f"{cells}xAA battery holder", "spec": "with leads", "unit_usd": 1.00 + 0.25 * cells, "query": f"{cells} AA battery holder with leads"},
            ]
    if abs(v - 5) < 0.05 or abs(v - 3.3) < 0.05:
        return [{"name": "Breadboard power module", "spec": f"USB, set jumper to {vl}", "unit_usd": 3.50, "query": "breadboard power supply module 3.3V 5V"}]
    if abs(v - 12) < 0.05:
        return [
            {"name": "12 V DC adapter", "spec": "1 A, barrel plug", "unit_usd": 9.00, "query": "12V 1A DC power adapter"},
            {"name": "DC barrel jack", "spec": "breadboard-friendly", "unit_usd": 1.00, "query": "DC barrel jack breadboard adapter"},
        ]
    return [{"name": "Adjustable DC supply", "spec": f"set to {vl}", "unit_usd": 45.00, "query": "adjustable DC bench power supply"}]


def _component_items(comp: dict) -> list[dict]:
    t = comp["type"]
    if t == "resistor":
        return [_resistor_item(comp)]
    if t == "led":
        color = (comp.get("color") or "red").lower()
        return [{"name": "LED", "spec": f"5 mm {color}", "unit_usd": 0.25, "query": f"5mm {color} LED through hole"}]
    if t == "capacitor":
        value = float(comp["value"])
        label = format_si(value, "F")
        if value < 1e-6:
            return [{"name": "Ceramic capacitor", "spec": label, "unit_usd": 0.20, "query": f"{label.replace(chr(0xb5), 'u')} ceramic capacitor through hole"}]
        return [{"name": "Electrolytic capacitor", "spec": f"{label}, 25 V or more", "unit_usd": 0.35, "query": f"{label.replace(chr(0xb5), 'u')} electrolytic capacitor 25V"}]
    if t == "diode":
        if float(comp["value"]) < 0.45:
            return [{"name": "Schottky diode", "spec": "1N5817", "unit_usd": 0.25, "query": "1N5817 schottky diode"}]
        return [{"name": "Signal diode", "spec": "1N4148", "unit_usd": 0.10, "query": "1N4148 diode"}]
    if t == "voltage_source":
        return _source_items(comp)
    if t == "current_source":
        return [{"name": "Constant-current source", "spec": "LM317 + set resistor", "unit_usd": 1.50, "query": "LM317 regulator TO-220"}]
    return []


def line_items(netlist: dict) -> list[dict]:
    """Parts grouped by (name, spec): qty, refs, unit/total price, links."""
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for comp in netlist.get("components", []):
        for item in _component_items(comp):
            key = (item["name"], item["spec"])
            g = groups.setdefault(key, {**item, "qty": 0, "refs": []})
            g["qty"] += item.get("qty", 1)
            if comp["id"] not in g["refs"]:
                g["refs"].append(comp["id"])
    out = []
    for g in groups.values():
        g["total_usd"] = round(g["unit_usd"] * g["qty"], 2)
        g["links"] = buy_links(g["query"])
        out.append(g)
    return out


def tool_items(table: list | None = None) -> list[dict]:
    fiber = table is not None
    return [
        {"name": n, "spec": s, "unit_usd": p, "qty": 1, "total_usd": p, "note": note, "links": buy_links(q, fiber=fiber), "refs": []}
        for n, s, p, q, note in (table if table is not None else _TOOLS)
    ]


# ---------------------------------------------------------------------------
# Breadboard guide
# ---------------------------------------------------------------------------

def _locations(netlist: dict) -> dict[str, str]:
    """Node -> breadboard location: '+ rail', '- rail' or 'row N'."""
    comps = netlist.get("components", [])
    loc: dict[str, str] = {}
    for c in comps:
        for n in c["nodes"]:
            if _is_ground(n):
                loc[n] = "the − rail"
    for c in comps:
        if c["type"] == "voltage_source" and c["nodes"][0] not in loc and "the + rail" not in loc.values():
            loc[c["nodes"][0]] = "the + rail"
    row = 10
    for c in comps:
        for n in c["nodes"]:
            if n not in loc:
                loc[n] = f"row {row}"
                row += 5
    return loc


def build_steps(netlist: dict, report: dict) -> list[str]:
    comps = netlist.get("components", [])
    loc = _locations(netlist)
    volts = report.get("node_voltages") or {}
    steps = [
        "Lay out every part from the bill of materials. Do not connect the power source until the last step.",
        "Orient the breadboard with the red (+) rail at the top and the blue (−) rail at the bottom. "
        "Each numbered row is one connection: legs in the same row are joined.",
    ]
    for c in comps:
        a, b = (loc[n] for n in c["nodes"])
        cid, t = c["id"], c["type"]
        if t == "resistor":
            steps.append(f"Place {cid} ({format_si(c['value'], chr(0x3a9))} resistor) from {a} to {b}. Resistors have no polarity.")
        elif t == "led":
            color = (c.get("color") or "red").lower()
            steps.append(f"Place {cid} ({color} LED): long leg (anode, +) into {a}, short leg (cathode, flat edge of the rim) into {b}. An LED put in backwards stays dark.")
        elif t == "diode":
            steps.append(f"Place {cid} (diode): the end with the band (cathode) into {b}, the other end into {a}.")
        elif t == "capacitor":
            low = min(c["nodes"], key=lambda n: volts.get(n, 0.0) if not _is_ground(n) else 0.0)
            steps.append(f"Place {cid} ({format_si(c['value'], 'F')} capacitor) between {a} and {b}. "
                         f"If it is electrolytic, the leg next to the stripe (−) goes to {loc[low]}.")
        elif t == "current_source":
            steps.append(f"Build {cid} as an LM317 constant-current stage and connect its output to {a} and its return to {b}.")
    for c in comps:
        if c["type"] != "voltage_source":
            continue
        a, b = (loc[n] for n in c["nodes"])
        if abs(float(c["value"]) - 9) < 0.05:
            steps.append(f"Last: connect {c['id']} (9 V battery) with the snap: red lead into {a}, black lead into {b}, then clip on the battery.")
        else:
            steps.append(f"Last: connect {c['id']} ({format_si(c['value'], 'V')}): + to {a}, − to {b}, then switch it on.")
    return steps


def test_steps(netlist: dict, report: dict) -> list[str]:
    loc = _locations(netlist)
    volts = report.get("node_voltages") or {}
    steps = ["Set the multimeter to DC volts and hold the black probe on the − rail."]
    for node, v in volts.items():
        if _is_ground(node) or node not in loc:
            continue
        steps.append(f"Red probe on {loc[node]} (node '{node}'): expect about {v:.2f} V.")
    for branch, amps in (report.get("branch_currents") or {}).items():
        steps.append(f"Supply current (through {branch.upper()}): break the + lead, put the meter in series on DC mA, "
                     f"expect about {abs(float(amps)) * 1000:.1f} mA.")
    steps.append("Readings within about 10 % match the simulation. Far off? Disconnect power and re-check the build steps.")
    return steps


def pcb_footprints(netlist: dict) -> list[dict]:
    fp = {
        "resistor": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
        "led": "LED_THT:LED_D5.0mm",
        "diode": "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal",
        "voltage_source": "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
        "current_source": "Package_TO_SOT_THT:TO-220-3_Vertical",
    }
    rows = []
    for c in netlist.get("components", []):
        if c["type"] == "capacitor":
            f = ("Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm" if float(c["value"]) < 1e-6
                 else "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm")
        else:
            f = fp.get(c["type"], "")
        rows.append({"ref": c["id"], "type": c["type"], "footprint": f})
    return rows


PCB_STEPS = [
    "Draw the schematic in KiCad (free) with the same parts and connections as this netlist.",
    "Assign the footprints in the table above (Tools > Assign Footprints).",
    "Lay out the board, route the traces, and run the Design Rules Check until it is clean.",
    "Export Gerbers and the drill file (File > Fabrication Outputs), then zip them.",
    "Upload the zip to a board house such as JLCPCB, PCBWay or OSH Park and check the quote it shows.",
    "Solder the same parts from the bill of materials; the multimeter checks in section 04 still apply.",
]


# ---------------------------------------------------------------------------
# Fiber link packs (report from fiber.check_link)
# ---------------------------------------------------------------------------

_FIBER_TOOLS = [
    ("Fiber cleaning kit", "one-click cleaner + wipes", 20.00, "fiber optic connector cleaner one click", ""),
    ("Optical power meter", "InGaAs, -50 to +3 dBm", 40.00, "optical power meter fiber -50 dBm", ""),
    ("Visual fault locator", "650 nm, finds breaks", 15.00, "visual fault locator 650nm fiber", ""),
]


def _fiber_items(e: dict, wl: float) -> list[dict]:
    t = e["type"]
    band = f"{wl:g} nm"
    if t == "laser":
        return [{"name": "Fiber laser source", "spec": f"{band}, {e.get('power_dbm', 0):g} dBm, FC connector",
                 "unit_usd": 60.00, "query": f"{wl:g}nm fiber laser light source FC"}]
    if t == "fiber":
        kind = "single-mode OS2" if (e.get("fiber_type") or "smf") == "smf" else "multimode OM3"
        length = float(e.get("length_m") or 1)
        if length > 100:
            return [{"name": "Fiber cable", "spec": f"{kind}, {length / 1000:g} km, duplex", "unit_usd": round(0.15 * length, 2),
                     "query": f"{kind} fiber cable {length / 1000:g} km"}]
        return [{"name": f"Patch cord, {length:g} m", "spec": f"{kind}, FC/UPC", "unit_usd": 12.00,
                 "query": f"{kind} fiber patch cord FC UPC {length:g}m"}]
    if t == "connector":
        pol = e.get("polish_a") or "UPC"
        return [{"name": "Mating sleeve (adapter)", "spec": f"FC/{pol}, single-mode", "unit_usd": 5.00,
                 "query": f"FC {pol} fiber mating sleeve adapter single mode"}]
    if t == "splice":
        if (e.get("splice_kind") or "fusion") == "mechanical":
            return [{"name": "Mechanical splice", "spec": "single-mode", "unit_usd": 6.00, "query": "mechanical fiber splice single mode"}]
        return [{"name": "Splice protection sleeve", "spec": "60 mm, for fusion splices", "unit_usd": 0.20, "query": "fusion splice protection sleeve 60mm"}]
    if t == "splitter":
        n = int(e.get("ports") or 2)
        return [{"name": f"1x{n} splitter", "spec": f"PLC/FBT, equal split, {band}", "unit_usd": 15.00 if n <= 4 else 30.00,
                 "query": f"1x{n} fiber splitter PLC FC UPC"}]
    if t == "attenuator":
        db = e.get("loss_db") if e.get("loss_db") is not None else 10
        return [{"name": "In-line attenuator", "spec": f"{db:g} dB, FC/UPC", "unit_usd": 8.00, "query": f"{db:g}dB fiber attenuator FC UPC"}]
    if t == "isolator":
        return [{"name": "Fiber isolator", "spec": f"{band}, polarization-independent", "unit_usd": 60.00,
                 "query": f"{wl:g}nm fiber optic isolator FC"}]
    if t == "detector":
        return [{"name": "Photodetector", "spec": "InGaAs, fiber-coupled (FC)", "unit_usd": 150.00,
                 "query": "InGaAs fiber coupled photodetector FC"}]
    return []


def fiber_line_items(report: dict) -> list[dict]:
    wl = float(report.get("wavelength_nm") or 1550)
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for e in report["link"]["elements"]:
        for item in _fiber_items(e, wl):
            g = groups.setdefault((item["name"], item["spec"]), {**item, "qty": 0, "refs": []})
            g["qty"] += 1
            g["refs"].append(e["id"])
    out = []
    for g in groups.values():
        g["total_usd"] = round(g["unit_usd"] * g["qty"], 2)
        g["links"] = buy_links(g["query"], fiber=True)
        out.append(g)
    return out


def fiber_build_steps(report: dict) -> list[str]:
    els = report["link"]["elements"]
    wl = report.get("wavelength_nm", 1550)
    steps = [
        f"Laser safety first: keep the laser OFF until the last step, never look into a fiber end, and wear glasses rated for {wl:g} nm.",
        "Clean and inspect every connector end face before each mating (one-click cleaner, then inspect). Dirt is the most common cause of extra loss.",
    ]
    for prev, e, nxt in zip([None] + els[:-1], els, els[1:] + [None]):
        t, i = e["type"], e["id"]
        if t == "fiber":
            steps.append(f"Route {i} ({e.get('length_m') or 1:g} m). Keep bends wider than about 30 mm across: tight bends leak light.")
        elif t == "connector":
            steps.append(f"Join the two fibers at {i} with an FC/{e.get('polish_a') or 'UPC'} mating sleeve: line up the key, push in, tighten finger-tight.")
        elif t == "splice":
            steps.append(f"Make splice {i} ({e.get('splice_kind') or 'fusion'}), then protect it with a sleeve.")
        elif t == "splitter":
            steps.append(f"Connect {i} (1x{e.get('ports') or 2} splitter): input from {prev['id'] if prev else 'the source'}, "
                         f"output 1 toward {nxt['id'] if nxt else 'the detector'}. Cap the unused outputs.")
        elif t == "attenuator":
            steps.append(f"Insert {i} ({e.get('loss_db') if e.get('loss_db') is not None else 10:g} dB attenuator) in line.")
        elif t == "isolator":
            steps.append(f"Insert {i} (isolator) with its arrow pointing away from the laser.")
        elif t == "detector":
            steps.append(f"Connect the last fiber to {i} (detector).")
    steps.append(f"Last: switch on the laser ({report.get('launch_dbm', 0):g} dBm).")
    return steps


def fiber_test_steps(report: dict) -> list[str]:
    steps = [f"Set the optical power meter to {report.get('wavelength_nm', 1550):g} nm. Measure by disconnecting at each point below "
             "and plugging that fiber end into the meter (laser on, nothing pointed at your eyes)."]
    for row in report["trace"]:
        if row["type"] in ("connector", "splitter", "attenuator", "isolator", "splice"):
            mw = row["power_mw"]
            mw_txt = f"{mw:.3f} mW" if mw >= 0.001 else f"{mw * 1000:.2f} µW"
            steps.append(f"After {row['id']} ({row['type']}): expect about {row['power_dbm']:.1f} dBm ({mw_txt}).")
    steps.append("Within about 1 dB of these values is normal. More loss? Re-clean the connector just before the drop.")
    return steps


def fiber_pack(report: dict) -> dict:
    title = (report["link"].get("title") or "").strip() or "Your fiber link"
    rx = report.get("received_dbm")
    return {
        "title": title,
        "verdict": report["verdict"],
        "findings": report.get("findings") or [],
        "pass_text": "This link passed every rule in the optical power budget. The parts, prices and steps below are for exactly this link.",
        "summary": f"{report.get('wavelength_nm', 1550):g} nm · launch {report.get('launch_dbm', 0):g} dBm · "
                   f"loss {report.get('total_loss_db', 0):.2f} dB · received {rx:.1f} dBm",
        "items": fiber_line_items(report),
        "tools": tool_items(_FIBER_TOOLS),
        "sections": [
            {"title": "Assemble the link", "steps": fiber_build_steps(report)},
            {"title": "Check it with an optical power meter", "steps": fiber_test_steps(report)},
        ],
        "footer": "verdict from a deterministic optical power budget + fixed rules",
    }


# ---------------------------------------------------------------------------
# Free-space optics bench packs (report from optics.trace_bench)
# ---------------------------------------------------------------------------

HOLE_PITCH_MM = 25
_LENS_PN = {50: "LA1131-A", 75: "LA1608-A", 100: "LA1509-A", 150: "LA1433-A", 200: "LA1708-A", 250: "LA1461-A"}
_ND_PN = {0.5: "NE05A", 1.0: "NE10A", 2.0: "NE20A", 3.0: "NE30A", 4.0: "NE40A"}
_OPTICS_TOOLS = [
    ("Laser safety glasses", "rated for your laser wavelength", 90.00, "laser safety glasses", ""),
    ("Alignment card", "fluorescent target, visible + NIR", 25.00, "laser alignment card", ""),
    ("Hex key set", "imperial + metric balldrivers", 15.00, "balldriver hex key set imperial metric", ""),
]


def _optic(pn: str | None, name: str, spec: str, price: float, query: str | None = None) -> dict:
    return {"name": name, "spec": (f"{pn} \u00b7 " if pn else "") + spec, "unit_usd": price, "query": query or pn or spec}


def _optics_items(e: dict) -> list[dict]:
    t = e["type"]
    post = _optic(None, "Post assembly", "TR2 post + PH2 holder + BA1S base", 21.00, "TR2 PH2 BA1S post holder base")
    if t == "laser":
        wl = e.get("wavelength_nm") or 633
        pn = "CPS532" if abs(wl - 532) < 5 else "CPS635R" if 630 <= wl <= 640 else None
        return [_optic(pn, "Laser module", f"collimated, {wl:g} nm, {e.get('power_mw', 1):g} mW", 120.00,
                       pn or f"{wl:g}nm collimated laser diode module"), post]
    if t == "mirror":
        return [_optic("BB1-E02", "Mirror", "\u00d81\" dielectric, 400-750 nm", 80.00),
                _optic("KM100", "Kinematic mount", "\u00d81\" optics", 48.00), post]
    if t == "beamsplitter":
        return [_optic("BS013", "Beamsplitter cube", "10 mm, 50:50, 400-700 nm", 250.00),
                _optic("KM100CP", "Kinematic cube mount", "for 10 mm cubes", 70.00), post]
    if t == "lens":
        f = e.get("focal_mm") or 100
        pn = _LENS_PN.get(int(round(f)))
        return [_optic(pn, "Lens", f"\u00d81\" plano-convex, f = {f:g} mm", 35.00, pn or f"1 inch plano convex lens f={f:g}mm"),
                _optic("LMR1", "Lens mount", "\u00d81\" optics", 18.00), post]
    if t == "filter":
        od = float(e.get("od") if e.get("od") is not None else 1.0)
        pn = _ND_PN.get(od)
        return [_optic(pn, "ND filter", f"\u00d81\" mounted, OD {od:g}", 65.00, pn or f"OD {od:g} ND filter mounted 1 inch"),
                _optic("LMR1", "Lens mount", "\u00d81\" optics", 18.00), post]
    if t == "dump":
        return [_optic("LB1", "Beam block", "black anodized", 95.00), post]
    if t == "detector":
        return [_optic("DET36A2", "Photodetector", "Si, 350-1100 nm, biased", 150.00), post]
    return []


def optics_line_items(report: dict) -> list[dict]:
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    bench = report["bench"]
    w, d = bench.get("width_mm", 600), bench.get("depth_mm", 300)
    board = _optic("MB3060/M" if w <= 600 and d <= 300 else None, "Optical breadboard",
                   f"{w:g} x {d:g} mm, M6 holes on {HOLE_PITCH_MM} mm grid", 520.00, f"optical breadboard {w:g} x {d:g} mm")
    for item, ref in [(board, "bench")] + [(i, e["id"]) for e in bench["elements"] for i in _optics_items(e)]:
        g = groups.setdefault((item["name"], item["spec"]), {**item, "qty": 0, "refs": []})
        g["qty"] += 1
        g["refs"].append(ref)
    out = []
    for g in groups.values():
        g["total_usd"] = round(g["unit_usd"] * g["qty"], 2)
        g["links"] = buy_links(g["query"], fiber=True)
        out.append(g)
    return out


def _hole(mm: float) -> int:
    return int(round(mm / HOLE_PITCH_MM))


def optics_build_steps(report: dict) -> list[str]:
    bench = report["bench"]
    els = {e["id"]: e for e in bench["elements"]}
    wl = report.get("wavelength_nm", 633)
    steps = [
        f"Put on laser safety glasses rated for {wl:g} nm, take off watches and rings, and keep every beam at post height (about 75 mm), below eye level.",
        f"Bolt the breadboard down. Holes are counted from the lower-left corner: column = x / {HOLE_PITCH_MM} mm, row = y / {HOLE_PITCH_MM} mm.",
    ]
    for e in bench["elements"]:
        where = f"column {_hole(e['x_mm'])}, row {_hole(e['y_mm'])} (x {e['x_mm']:g} mm, y {e['y_mm']:g} mm)"
        verb = {"laser": "the laser, pointing", "mirror": "the mirror, face normal at", "beamsplitter": "the beamsplitter cube, face normal at",
                "lens": "the lens, axis along", "filter": "the ND filter, axis along", "dump": "the beam block, facing",
                "detector": "the detector, facing"}.get(e["type"], e["type"])
        steps.append(f"Mount {e['id']} ({verb} {e.get('angle_deg', 0):g}\u00b0) on its post at {where}.")
    order = []
    for s in report.get("segments", []):
        if s["end"] in els and s["end"] not in order:
            order.append(s["end"])
    steps.append("Turn the laser on at its lowest setting. Align in beam order, one element at a time, using the alignment card:")
    for i, eid in enumerate(order):
        nxt = order[i + 1] if i + 1 < len(order) else None
        e = els[eid]
        if e["type"] in ("mirror", "beamsplitter") and nxt:
            steps.append(f"Tilt {eid} with its two knobs until the beam lands on the centre of {nxt}.")
        elif e["type"] in ("lens", "filter"):
            steps.append(f"Centre {eid} on the beam: the spot after it should not move when you slide the card along the beam.")
    steps.append("Check that every beam ends on a beam block or detector, including weak reflections off lens and filter faces.")
    return steps


def optics_test_steps(report: dict) -> list[str]:
    steps = []
    for det, mw in (report.get("received_mw") or {}).items():
        steps.append(f"{det} should read about {mw:.3g} mW (from the ray trace, before real-world losses).")
    steps.append("Within about 20 % is normal for a first alignment. Much lower? Re-centre the beam on each optic in order.")
    return steps


def optics_pack(report: dict) -> dict:
    bench = report["bench"]
    return {
        "title": (bench.get("title") or "").strip() or "Your optics bench",
        "verdict": report["verdict"],
        "findings": report.get("findings") or [],
        "pass_text": "This bench passed every rule in the ray trace: every beam ends on a detector or beam block. The parts, prices and steps below are for exactly this layout.",
        "summary": f"{len(bench['elements'])} elements \u00b7 {bench.get('width_mm', 600):g} x {bench.get('depth_mm', 300):g} mm \u00b7 "
                   f"{report.get('wavelength_nm', 633):g} nm",
        "items": optics_line_items(report),
        "tools": tool_items(_OPTICS_TOOLS),
        "sections": [
            {"title": "Build and align the bench", "steps": optics_build_steps(report)},
            {"title": "Check the power at each detector", "steps": optics_test_steps(report)},
        ],
        "footer": "verdict from a deterministic 2D ray trace + fixed rules",
    }


def circuit_pack(netlist: dict, report: dict) -> dict:
    return {
        "title": (netlist.get("title") or "").strip() or "Your circuit",
        "verdict": str(report.get("verdict", "UNKNOWN")).upper(),
        "findings": report.get("findings") or [],
        "pass_text": "This circuit passed every rule in a real ngspice simulation. The parts, prices and steps below are for exactly this netlist.",
        "summary": f"{len(netlist.get('components', []))} parts",
        "items": line_items(netlist),
        "tools": tool_items(),
        "sections": [
            {"title": "Build it on a breadboard", "steps": build_steps(netlist, report)},
            {"title": "Test it with a multimeter", "steps": test_steps(netlist, report)},
            {"title": "Order a printed circuit board (optional)",
             "intro": "Once the breadboard works, the same circuit can go on a PCB. Suggested KiCad footprints:",
             "table": {"header": ["Ref", "Type", "KiCad footprint"], "widths": [16, 30, None],
                       "rows": [[r["ref"], r["type"].replace("_", " "), r["footprint"]] for r in pcb_footprints(netlist)]},
             "steps": PCB_STEPS,
             "note": "Board houses show a live quote when you upload; small two-layer boards are usually the cheapest option. "
                     "Netproof does not generate board layouts yet."},
        ],
        "footer": "verdict from an ngspice DC operating point + fixed rules",
    }


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_FONTS = Path(__file__).resolve().parent / "fonts"
_INK = (28, 29, 33)
_MUTED = (98, 102, 111)
_LINE = (225, 225, 222)
_ACCENT = (243, 154, 43)
_VERDICT_RGB = {"PASS": (26, 127, 55), "WARN": (180, 83, 9), "STOP": (200, 16, 46)}


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def render_pdf(pack: dict) -> bytes:
    """Render a pack (circuit_pack / fiber_pack) as a PDF."""
    from fpdf import FPDF  # local import: only this route needs it

    verdict = str(pack.get("verdict", "UNKNOWN")).upper()
    parts, tools = pack["items"], pack["tools"]
    parts_total = round(sum(p["total_usd"] for p in parts), 2)
    tools_total = round(sum(t["total_usd"] for t in tools), 2)
    footer_text = f"Netproof build pack · {pack['footer']} · prices: single-unit estimates, {PRICES_AS_OF}"

    class PDF(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font("Sans", "", 7.5)
            self.set_text_color(*_MUTED)
            self.cell(0, 5, f"{footer_text} · page {self.page_no()}", align="C")

    pdf = PDF(format="Letter")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("Sans", "", str(_FONTS / "DejaVuSans.ttf"))
    pdf.add_font("Sans", "B", str(_FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_font("Mono", "", str(_FONTS / "DejaVuSansMono.ttf"))
    pdf.add_page()
    W = pdf.epw

    def eyebrow(text):
        pdf.set_font("Mono", "", 8)
        pdf.set_text_color(*_MUTED)
        pdf.cell(0, 5, text.upper(), new_x="LMARGIN", new_y="NEXT")

    def section(num, text):
        pdf.ln(4)
        eyebrow(f"{num:02d}  {text}")
        pdf.set_draw_color(*_LINE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + W, pdf.get_y())
        pdf.ln(2)

    def para(text, size=9.5, color=_INK):
        pdf.set_font("Sans", "", size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")

    def numbered(items):
        pdf.set_font("Sans", "", 9.5)
        for i, s in enumerate(items, 1):
            pdf.set_text_color(*_ACCENT)
            pdf.cell(8, 5.4, f"{i}.")
            pdf.set_text_color(*_INK)
            pdf.multi_cell(W - 8, 5.4, s, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.6)

    def table(rows, widths, header, link_col=None):
        widths = [w if w is not None else W - sum(x for x in widths if x is not None) for w in widths]
        pdf.set_font("Sans", "B", 8)
        pdf.set_text_color(*_MUTED)
        for h, w in zip(header, widths):
            pdf.cell(w, 6, h, border="B")
        pdf.ln()
        pdf.set_font("Sans", "", 8.5)
        for row in rows:
            if pdf.get_y() > pdf.h - 30:
                pdf.add_page()
            pdf.set_text_color(*_INK)
            for i, (val, w) in enumerate(zip(row["cells"], widths)):
                if i == link_col:
                    pdf.set_text_color(*_ACCENT)
                    x = pdf.get_x()
                    for label, url in row["links"]:
                        pdf.cell(pdf.get_string_width(label) + 2.5, 6.5, label, link=url)
                    pdf.line(x, pdf.get_y() + 6.5, x + w, pdf.get_y() + 6.5)
                    pdf.set_x(x + w)
                    pdf.set_text_color(*_INK)
                else:
                    text = val
                    while text and pdf.get_string_width(text) > w - 2:
                        text = text[:-2] + "…"
                    pdf.cell(w, 6.5, text, border="B")
            pdf.ln()

    # Header band
    pdf.set_fill_color(*_INK)
    pdf.rect(0, 0, pdf.w, 34, style="F")
    pdf.set_xy(16, 9)
    pdf.set_font("Mono", "", 8)
    pdf.set_text_color(200, 203, 210)
    pdf.cell(0, 5, f"NETPROOF BUILD PACK · {date.today():%B %d, %Y}".upper())
    pdf.set_xy(16, 15)
    pdf.set_font("Sans", "B", 17)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(W - 30, 10, pack["title"])
    vr = _VERDICT_RGB.get(verdict, _MUTED)
    pdf.set_fill_color(*vr)
    pdf.set_xy(pdf.w - 16 - 26, 15)
    pdf.set_font("Sans", "B", 11)
    pdf.cell(26, 10, verdict, align="C", fill=True)
    pdf.set_y(40)

    num = 1
    section(num, "Summary")
    if verdict == "PASS":
        para(pack["pass_text"])
    else:
        para(f"Verdict {verdict}: no STOP, but these warnings still apply. Read them before you build:", color=vr)
        for f in pack["findings"]:
            para(f"• {f.get('rule_id', '')}: {f.get('message', '')}", size=9)
    pdf.set_font("Mono", "", 9)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 6, f"{pack['summary']} · parts {_usd(parts_total)} · tools {_usd(tools_total)} · "
                         f"total {_usd(parts_total + tools_total)}", new_x="LMARGIN", new_y="NEXT")

    num += 1
    section(num, "Bill of materials")
    widths = [10, 40, 52, 20, 20, None]
    table([{"cells": [str(p["qty"]), p["name"], p["spec"], _usd(p["unit_usd"]), _usd(p["total_usd"]), ""], "links": p["links"]}
           for p in parts], widths, ["Qty", "Part", "Spec", "Est. unit", "Est. total", "Buy"], link_col=5)
    pdf.ln(1)
    para("Refs: " + "; ".join(f"{p['name']} = {', '.join(p['refs'])}" for p in parts), size=8, color=_MUTED)
    pdf.ln(2)
    eyebrow("Tools and supplies (one-time)")
    table([{"cells": [str(t["qty"]), t["name"], t["spec"], _usd(t["unit_usd"]), _usd(t["total_usd"]), ""], "links": t["links"]}
           for t in tools], widths, ["Qty", "Item", "Spec", "Est. unit", "Est. total", "Buy"], link_col=5)
    pdf.ln(2)
    pdf.set_font("Sans", "B", 10)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 6, f"Estimated total: {_usd(parts_total + tools_total)}  (parts {_usd(parts_total)} + tools {_usd(tools_total)}, "
                   f"before tax and shipping)", new_x="LMARGIN", new_y="NEXT")

    for sec in pack["sections"]:
        num += 1
        section(num, sec["title"])
        if sec.get("intro"):
            para(sec["intro"])
            pdf.ln(1)
        if sec.get("table"):
            t = sec["table"]
            table([{"cells": r, "links": []} for r in t["rows"]], t["widths"], t["header"])
            pdf.ln(2)
        numbered(sec.get("steps", []))
        if sec.get("note"):
            para(sec["note"], size=8.5, color=_MUTED)

    return bytes(pdf.output())
