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


def buy_links(query: str) -> list[tuple[str, str]]:
    q = quote_plus(query)
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


def tool_items() -> list[dict]:
    return [
        {"name": n, "spec": s, "unit_usd": p, "qty": 1, "total_usd": p, "note": note, "links": buy_links(q), "refs": []}
        for n, s, p, q, note in _TOOLS
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


def render_pdf(netlist: dict, report: dict) -> bytes:
    from fpdf import FPDF  # local import: only this route needs it

    verdict = str(report.get("verdict", "UNKNOWN")).upper()
    title = (netlist.get("title") or "").strip() or "Your circuit"
    parts = line_items(netlist)
    tools = tool_items()
    parts_total = round(sum(p["total_usd"] for p in parts), 2)
    tools_total = round(sum(t["total_usd"] for t in tools), 2)

    class PDF(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font("Sans", "", 7.5)
            self.set_text_color(*_MUTED)
            self.cell(0, 5, f"Netproof build pack · verdict from an ngspice DC operating point + fixed rules · "
                            f"prices are typical single-unit estimates ({PRICES_AS_OF}); check the links · page {self.page_no()}",
                      align="C")

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
        eyebrow(f"{num}  {text}")
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
                        lw = pdf.get_string_width(label) + 2.5
                        pdf.cell(lw, 6.5, label, link=url)
                    pdf.line(x, pdf.get_y() + 6.5, x + w, pdf.get_y() + 6.5)
                    pdf.set_x(x + w)
                    pdf.set_text_color(*_INK)
                else:
                    pdf.cell(w, 6.5, val, border="B")
            pdf.ln()

    # Header
    pdf.set_fill_color(*_INK)
    pdf.rect(0, 0, pdf.w, 34, style="F")
    pdf.set_xy(16, 9)
    pdf.set_font("Mono", "", 8)
    pdf.set_text_color(200, 203, 210)
    pdf.cell(0, 5, f"NETPROOF BUILD PACK · {date.today():%B %d, %Y}".upper())
    pdf.set_xy(16, 15)
    pdf.set_font("Sans", "B", 17)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(W - 30, 10, title)
    vr = _VERDICT_RGB.get(verdict, _MUTED)
    pdf.set_fill_color(*vr)
    pdf.set_xy(pdf.w - 16 - 26, 15)
    pdf.set_font("Sans", "B", 11)
    pdf.cell(26, 10, verdict, align="C", fill=True)
    pdf.set_y(40)

    # 01 Summary
    section("01", "Summary")
    findings = report.get("findings") or []
    if verdict == "PASS":
        para("This circuit passed every rule in a real ngspice simulation. The parts, prices and steps below are for exactly this netlist.")
    else:
        para(f"Verdict {verdict}: it simulates without a STOP, but these warnings still apply. Read them before you build:", color=vr)
        for f in findings:
            para(f"• {f.get('rule_id', '')}: {f.get('message', '')}", size=9)
    counts = f"{len(netlist.get('components', []))} parts · parts {_usd(parts_total)} · tools {_usd(tools_total)} · total {_usd(parts_total + tools_total)}"
    pdf.set_font("Mono", "", 9)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 7, counts, new_x="LMARGIN", new_y="NEXT")

    # 02 Bill of materials
    section("02", "Bill of materials")
    widths = [10, 38, 50, 20, 20, W - 138]
    table(
        [{"cells": [str(p["qty"]), p["name"], p["spec"], _usd(p["unit_usd"]), _usd(p["total_usd"]), ""],
          "links": p["links"]} for p in parts],
        widths, ["Qty", "Part", "Spec", "Est. unit", "Est. total", "Buy"], link_col=5,
    )
    pdf.ln(1)
    para("Refs: " + "; ".join(f"{p['name']} = {', '.join(p['refs'])}" for p in parts), size=8, color=_MUTED)
    pdf.ln(2)
    eyebrow("Tools and supplies (one-time)")
    table(
        [{"cells": [str(t["qty"]), t["name"], t["spec"] + (f" ({t['note']})" if t["note"] else ""), _usd(t["unit_usd"]), _usd(t["total_usd"]), ""],
          "links": t["links"]} for t in tools],
        widths, ["Qty", "Item", "Spec", "Est. unit", "Est. total", "Buy"], link_col=5,
    )
    pdf.ln(2)
    pdf.set_font("Sans", "B", 10)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 6, f"Estimated total: {_usd(parts_total + tools_total)}  (parts {_usd(parts_total)} + tools {_usd(tools_total)}, before tax and shipping)",
             new_x="LMARGIN", new_y="NEXT")
    para("Buying single parts often costs more than a small assortment kit (resistor kits, LED kits) that covers future builds too.", size=8.5, color=_MUTED)

    # 03 Build
    section("03", "Build it on a breadboard")
    numbered(build_steps(netlist, report))

    # 04 Test
    section("04", "Test it with a multimeter")
    numbered(test_steps(netlist, report))

    # 05 PCB
    section("05", "Order a printed circuit board (optional)")
    para("Once the breadboard works, the same circuit can go on a PCB. Suggested KiCad footprints:")
    pdf.ln(1)
    table([{"cells": [r["ref"], r["type"].replace("_", " "), r["footprint"]], "links": []} for r in pcb_footprints(netlist)],
          [16, 30, W - 46], ["Ref", "Type", "KiCad footprint"])
    pdf.ln(2)
    numbered(PCB_STEPS)
    para("Board houses show a live quote when you upload; small two-layer boards are usually the cheapest option. Netproof does not generate board layouts yet.",
         size=8.5, color=_MUTED)

    return bytes(pdf.output())
