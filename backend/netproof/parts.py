"""Part limits table for Netproof.

Pure-Python table of component limits. No dependencies, no LLM calls,
no ngspice calls, no hardcoded demo netlists.
"""

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
