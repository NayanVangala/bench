"""Part limits table for Netproof.

Pure-Python table of component limits. No dependencies, no LLM calls,
no ngspice calls, no hardcoded demo netlists.
"""

from decimal import Decimal

# ---------------------------------------------------------------------------
# Standard values (E12 series) — the parts picker in web/3d/edit-panel.js
# suggests these through GET /parts. This module is the single source of
# truth: no second copy of any table lives in JavaScript.
# ---------------------------------------------------------------------------

# E12 mantissas (integers, NOT floats): 10 % tolerance series.
E12_MANTISSAS = [10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82]

# Resistor decades: 1 ohm .. 1 Mohm base -> values 1 ohm .. 8.2 Mohm
# (7 decades x 12 = 84 values; capped at 10 Mohm, no sub-1 ohm).
_RESISTOR_DECADES = [
    Decimal("1"), Decimal("10"), Decimal("100"), Decimal("1000"),
    Decimal("10000"), Decimal("100000"), Decimal("1000000"),
]

# Capacitor decades: 1 pF .. 1 mF base -> values 1 pF .. 8.2 mF
# (10 decades x 12 = 120 values; capped at 10 mF).
_CAPACITOR_DECADES = [
    Decimal("1e-12"), Decimal("1e-11"), Decimal("1e-10"), Decimal("1e-9"),
    Decimal("1e-8"), Decimal("1e-7"), Decimal("1e-6"), Decimal("1e-5"),
    Decimal("1e-4"), Decimal("1e-3"),
]


def _e12_values(decades):
    """Build E12 values for the given base decades.

    Value = mantissa / 10 x decade, computed in Decimal so no float
    artifact leaks into the payload (naive ``mantissa * decade`` float
    multiplication gives e.g. ``2.1999999999999998e-11`` for
    capacitors, which breaks exact-equality checks like
    ``1e-6 in values``). ``float()`` of an exact short Decimal is the
    same double as the matching float literal.
    """
    return [
        float(Decimal(m) * d / Decimal(10))
        for d in decades
        for m in E12_MANTISSAS
    ]


STANDARD_RESISTOR_VALUES = _e12_values(_RESISTOR_DECADES)
STANDARD_CAPACITOR_VALUES = _e12_values(_CAPACITOR_DECADES)

# Resistor power ratings (W) offered by the picker; the default is the
# existing RESISTOR_DEFAULT_WATTAGE below, unchanged.
RESISTOR_RATINGS_W = [0.125, 0.25, 0.5, 1, 2, 5]

# Generic-diode forward voltages (V). Conventional defaults, NOT
# measured part limits: 0.7 V silicon, 0.3 V Schottky.
DIODE_VF_DEFAULT = 0.7
DIODE_VF_SCHOTTKY = 0.3

# Source values (V / A). UI conveniences, not part limits: sources are
# usually dictated by the circuit, so the lists stay short.
STANDARD_VOLTAGE_VALUES = [1.5, 3.3, 5, 9, 12, 15, 24]
STANDARD_CURRENT_VALUES = [0.001, 0.01, 0.02, 0.1, 0.5, 1]

# Bump this whenever a table above changes; the frontend cache-keys on it.
PARTS_CATALOG_VERSION = 1

# LED forward voltages (V) and current limits (A).
_LED_VF = {
    "red": 2.0,
    "green": 2.1,
    "blue": 3.0,
    "white": 3.2,
    "yellow": 2.0,
}

_LED_IF_MAX = 0.030   # 30 mA
_LED_IF_WARN = 0.020  # warn at 20 mA
_LED_IF_MIN = 0.001   # undercurrent below 1 mA

# SPICE diode-model parameters per LED color, tuned so the simulated
# forward voltage matches _LED_VF at the 15 mA reference current.
#
# Diode equation (Vt = 25.85 mV at ~300 K):
#     V(I) = N * Vt * ln(I / IS + 1) + I * RS
# For each color, N and RS were chosen first (N = 2.0 for the ~2 V
# colors, N = 2.5 for blue/white, whose wider bandgap needs a larger
# ideality factor; RS = 5 ohm for the ~2 V colors, 30 ohm for
# blue/white, reflecting their higher bulk resistance), then IS was
# solved from the target Vf at I = 15 mA:
#     IS = I / (exp((Vf - I*RS) / (N*Vt)) - 1)
# and rounded to one significant figure. IS values of 1e-18..1e-21 A
# are typical for LED SPICE models (the junction term alone must
# supply ~2-3 V, i.e. ln(I/IS) ~ 37-45). Predicted Vf at 15 mA
# (Python evaluation of the equation above):
#     red    2.001 V (target 2.0)   green  2.099 V (target 2.1)
#     blue   3.006 V (target 3.0)   white  3.200 V (target 3.2)
#     yellow 2.001 V (target 2.0)
# Sanity: 9 V + 470 ohm + red gives I = 14.9 mA, Vled = 2.00 V.
# The previous single model (IS=1e-14, N=1.5, RS=1) gave only
# Vf = 1.10 V at 15 mA, so the simulated LED voltage did not match
# the table. Live ngspice verification is still needed on a Mac
# (no ngspice binary in this VM).
_LED_DIODE_PARAMS = {
    # color:  (IS [A], N, RS [ohm])
    "red":    (1e-18,  2.0, 5),
    "green":  (1.5e-19, 2.0, 5),
    "blue":   (1e-19,  2.5, 30),
    "white":  (5e-21,  2.5, 30),
    "yellow": (1e-18,  2.0, 5),
}

RESISTOR_DEFAULT_WATTAGE = 0.25  # W


def _norm_color(color):
    if not isinstance(color, str):
        raise TypeError("color must be a string")
    key = color.strip().lower()
    if key not in _LED_VF:
        raise ValueError(f"unknown LED color: {color!r} (expected one of {sorted(_LED_VF)})")
    return key


def led_vf(color):
    """Forward voltage (V) for an LED of the given color."""
    return _LED_VF[_norm_color(color)]


def led_if_max(color):
    """Maximum forward current (A) for an LED of the given color (0.030)."""
    _norm_color(color)
    return _LED_IF_MAX


def led_if_warn(color):
    """Current (A) at which to warn for an LED of the given color (0.020)."""
    _norm_color(color)
    return _LED_IF_WARN


def led_if_min(color):
    """Minimum useful forward current (A); below 1 mA counts as undercurrent."""
    _norm_color(color)
    return _LED_IF_MIN


def resistor_rating_w(component):
    """Power rating (W) for a resistor component.

    Returns the component's explicit ``rating_watts`` when it is set
    and positive, otherwise the default 0.25 W. Accepts a Component
    object or a dict with a ``rating_watts`` key.
    """
    if isinstance(component, dict):
        rating = component.get("rating_watts")
    else:
        rating = getattr(component, "rating_watts", None)
    if rating is not None and rating > 0:
        return rating
    return RESISTOR_DEFAULT_WATTAGE


def model_name(color):
    """SPICE diode model name for an LED color, e.g. DRED for red."""
    return "D" + _norm_color(color).upper()


def diode_model_line(color):
    """SPICE .model line for an LED of the given color.

    Parameters come from ``_LED_DIODE_PARAMS``, tuned per color so the
    diode equation gives Vf matching ``led_vf(color)`` at 15 mA
    (see the table comment above ``_LED_DIODE_PARAMS``).
    """
    name = model_name(color)
    is_, n, rs = _LED_DIODE_PARAMS[_norm_color(color)]
    return (
        f".model {name} D(IS={is_:g} RS={rs:g} N={n:g} BV=5 IBV=1e-3)"
    )


# ---------------------------------------------------------------------------
# Standard-value accessors + SI labels (parts picker, GET /parts)
# ---------------------------------------------------------------------------

_SI_PREFIXES_UP = ["", "k", "M", "G"]       # >= 1, steps of 1e3
_SI_PREFIXES_DOWN = ["m", "\u00b5", "n", "p"]  # < 1, steps of 1e-3


def format_si(value, unit):
    """Compact SI label for a value, e.g. ``format_si(470, "\\u03a9")`` -> ``"470 \\u03a9"``.

    Backend formats labels so label and value can never disagree; the
    frontend displays them verbatim and never formats values itself.
    (Note: bom.py has no such formatter — only a private
    ``_format_number()`` with no unit suffixes — so it lives here.)
    """
    v = float(value)
    if v == 0:
        return "0 " + unit
    av = abs(v)
    if av >= 1:
        scale = 0
        while av >= 1000 and scale < len(_SI_PREFIXES_UP) - 1:
            av /= 1000
            scale += 1
        return f"{av:g} {_SI_PREFIXES_UP[scale]}{unit}"
    scale = 0
    while av < 1 and scale < len(_SI_PREFIXES_DOWN):
        av *= 1000
        scale += 1
    return f"{av:g} {_SI_PREFIXES_DOWN[scale - 1]}{unit}"


def standard_resistor_values():
    """E12 resistor values in ohms: 84 values, 1 ohm .. 8.2 Mohm."""
    return list(STANDARD_RESISTOR_VALUES)


def standard_capacitor_values():
    """E12 capacitor values in farads: 120 values, 1 pF .. 8.2 mF."""
    return list(STANDARD_CAPACITOR_VALUES)


def standard_voltage_values():
    """Conventional voltage-source values in volts (UI convenience)."""
    return list(STANDARD_VOLTAGE_VALUES)


def standard_current_values():
    """Conventional current-source values in amps (UI convenience)."""
    return list(STANDARD_CURRENT_VALUES)


def resistor_ratings_w():
    """Resistor power ratings in watts offered by the picker."""
    return list(RESISTOR_RATINGS_W)


def led_catalog():
    """One row per LED colour: Vf, current limits, SPICE model name."""
    return [
        {
            "color": color,
            "vf_v": led_vf(color),
            "if_warn_a": led_if_warn(color),
            "if_max_a": led_if_max(color),
            "if_min_a": led_if_min(color),
            "model": model_name(color),
        }
        for color in _LED_VF
    ]


def _labelled(values, unit):
    return [{"value": v, "label": format_si(v, unit)} for v in values]


def parts_catalog():
    """Whole parts catalog, exactly as GET /parts serialises it.

    The endpoint adds no data of its own. Labels are backend-formatted
    (see :func:`format_si`); values are always SI floats.
    """
    return {
        "version": PARTS_CATALOG_VERSION,
        "resistor": {
            "unit": "ohm",
            "series": "E12",
            "values": _labelled(STANDARD_RESISTOR_VALUES, "\u03a9"),
            "ratings_w": list(RESISTOR_RATINGS_W),
            "default_rating_w": RESISTOR_DEFAULT_WATTAGE,
        },
        "capacitor": {
            "unit": "farad",
            "series": "E12",
            "values": _labelled(STANDARD_CAPACITOR_VALUES, "F"),
        },
        "led": {
            "unit": "volt",
            "colors": led_catalog(),
        },
        "diode": {
            "unit": "volt",
            "values": [
                {"value": DIODE_VF_DEFAULT, "label": "0.7 V (Si)"},
                {"value": DIODE_VF_SCHOTTKY, "label": "0.3 V (Schottky)"},
            ],
        },
        "voltage_source": {
            "unit": "volt",
            "source": "conventional",
            "values": _labelled(STANDARD_VOLTAGE_VALUES, "V"),
        },
        "current_source": {
            "unit": "amp",
            "source": "conventional",
            "values": _labelled(STANDARD_CURRENT_VALUES, "A"),
        },
    }
