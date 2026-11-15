"""Tests for the standard-value tables in backend/netproof/parts.py.

P5.3 parts picker: E12 resistor/capacitor tables, LED catalog, diode
Vf, source lists, ratings, SI labels. Pure Python, no ngspice, no
network, no LLM key. Existing functions/values are not changed here,
so the parts.py sanity case (9 V + 470 ohm + red LED -> ~14.9 mA,
Vf 2.00 V) is re-asserted at the bottom as a regression guard.
"""

from __future__ import annotations

from netproof import parts


def test_e12_mantissas():
    assert parts.E12_MANTISSAS == [10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82]


def test_resistor_values_count_and_range():
    values = parts.standard_resistor_values()
    assert len(values) == 84  # 7 decades x 12
    assert values == sorted(values)
    assert values[0] == 1.0
    assert values[-1] == 8.2e6
    assert 470 in values
    assert 1000 in values
    assert 1 not in values[1:]  # no duplicates
    assert len(set(values)) == 84


def test_resistor_decades_spot_checks():
    values = parts.standard_resistor_values()
    for v in (1, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2,
              10, 100, 120, 470, 820, 1000, 4700, 10000, 82000, 100000,
              470000, 1000000, 8200000):
        assert v in values, f"{v} missing from resistor values"


def test_capacitor_values_count_and_range():
    values = parts.standard_capacitor_values()
    assert len(values) == 120  # 10 decades x 12 (1 pF .. 8.2 mF)
    assert values == sorted(values)
    assert values[0] == 1e-12
    assert values[-1] == 8.2e-3
    assert 1e-6 in values
    assert 100e-9 in values
    assert 1e-9 in values
    assert len(set(values)) == 120


def test_capacitor_decades_spot_checks():
    values = parts.standard_capacitor_values()
    for v in (1e-12, 2.2e-12, 1e-11, 1e-10, 1e-9, 2.2e-9, 1e-8, 1e-7,
              1.2e-6, 4.7e-6, 1e-5, 1e-4, 0.001, 0.0082):
        assert v in values, f"{v} missing from capacitor values"


def test_no_float_artifacts():
    # Naive float construction gives e.g. 2.1999999999999998e-11.
    # Every table value must round-trip through 12 significant digits.
    for v in parts.standard_resistor_values() + parts.standard_capacitor_values():
        assert v == float(f"{v:.12g}"), f"float artifact: {v!r}"


def test_led_catalog_matches_accessors():
    catalog = parts.led_catalog()
    assert [row["color"] for row in catalog] == [
        "red", "green", "blue", "white", "yellow",
    ]
    expected_vf = {"red": 2.0, "green": 2.1, "blue": 3.0, "white": 3.2, "yellow": 2.0}
    expected_model = {"red": "DRED", "green": "DGREEN", "blue": "DBLUE",
                      "white": "DWHITE", "yellow": "DYELLOW"}
    for row in catalog:
        color = row["color"]
        assert row["vf_v"] == parts.led_vf(color) == expected_vf[color]
        assert row["if_warn_a"] == 0.02
        assert row["if_max_a"] == 0.03
        assert row["if_min_a"] == 0.001
        assert row["model"] == parts.model_name(color) == expected_model[color]


def test_resistor_ratings():
    assert parts.resistor_ratings_w() == [0.125, 0.25, 0.5, 1, 2, 5]
    assert 0.25 in parts.resistor_ratings_w()
    assert parts.RESISTOR_DEFAULT_WATTAGE == 0.25


def test_diode_vf_defaults():
    assert parts.DIODE_VF_DEFAULT == 0.7
    assert parts.DIODE_VF_SCHOTTKY == 0.3


def test_source_values():
    assert parts.standard_voltage_values() == [1.5, 3.3, 5, 9, 12, 15, 24]
    assert parts.standard_current_values() == [0.001, 0.01, 0.02, 0.1, 0.5, 1]


def test_format_si_labels():
    assert parts.format_si(470, "\u03a9") == "470 \u03a9"
    assert parts.format_si(1000, "\u03a9") == "1 k\u03a9"
    assert parts.format_si(8200000, "\u03a9") == "8.2 M\u03a9"
    assert parts.format_si(1, "\u03a9") == "1 \u03a9"
    assert parts.format_si(1e-6, "F") == "1 \u00b5F"
    assert parts.format_si(100e-9, "F") == "100 nF"
    assert parts.format_si(1e-12, "F") == "1 pF"
    assert parts.format_si(0.0082, "F") == "8.2 mF"
    assert parts.format_si(0.01, "A") == "10 mA"
    assert parts.format_si(3.3, "V") == "3.3 V"


def test_parts_catalog_shape_and_labels():
    catalog = parts.parts_catalog()
    assert catalog["version"] == 1
    assert set(catalog) == {
        "version", "resistor", "capacitor", "led",
        "diode", "voltage_source", "current_source",
    }
    resistor = catalog["resistor"]
    assert resistor["unit"] == "ohm"
    assert resistor["series"] == "E12"
    assert len(resistor["values"]) == 84
    assert resistor["ratings_w"] == [0.125, 0.25, 0.5, 1, 2, 5]
    assert resistor["default_rating_w"] == 0.25
    labels = {e["value"]: e["label"] for e in resistor["values"]}
    assert labels[470] == "470 \u03a9"
    assert labels[1000] == "1 k\u03a9"
    assert labels[1000000] == "1 M\u03a9"

    capacitor = catalog["capacitor"]
    assert capacitor["unit"] == "farad"
    assert capacitor["series"] == "E12"
    assert len(capacitor["values"]) == 120
    clabels = {e["value"]: e["label"] for e in capacitor["values"]}
    assert clabels[1e-6] == "1 \u00b5F"
    assert clabels[100e-9] == "100 nF"
    assert clabels[1e-12] == "1 pF"
    assert clabels[0.0082] == "8.2 mF"

    assert catalog["led"]["unit"] == "volt"
    assert len(catalog["led"]["colors"]) == 5

    assert catalog["diode"] == {
        "unit": "volt",
        "values": [
            {"value": 0.7, "label": "0.7 V (Si)"},
            {"value": 0.3, "label": "0.3 V (Schottky)"},
        ],
    }

    assert catalog["voltage_source"]["source"] == "conventional"
    vlabels = {e["value"]: e["label"] for e in catalog["voltage_source"]["values"]}
    assert vlabels[3.3] == "3.3 V"
    assert vlabels[5] == "5 V"
    assert catalog["current_source"]["source"] == "conventional"
    alabels = {e["value"]: e["label"] for e in catalog["current_source"]["values"]}
    assert alabels[0.01] == "10 mA"
    assert alabels[1] == "1 A"


def test_parts_catalog_values_are_plain_numbers():
    # Every value in the payload is a number present in parts.py
    # (no string values, no second table to drift).
    catalog = parts.parts_catalog()
    resistor_values = set(parts.standard_resistor_values())
    capacitor_values = set(parts.standard_capacitor_values())
    for e in catalog["resistor"]["values"]:
        assert isinstance(e["value"], (int, float))
        assert e["value"] in resistor_values
    for e in catalog["capacitor"]["values"]:
        assert isinstance(e["value"], (int, float))
        assert e["value"] in capacitor_values


def test_sanity_case_unchanged():
    # 9 V + 470 ohm + red LED -> ~14.9 mA, Vf 2.00 V (parts.py comment).
    assert parts.led_vf("red") == 2.0
    current_a = (9.0 - parts.led_vf("red")) / 470.0
    assert abs(current_a - 0.0149) < 0.0001
    assert 470 in parts.standard_resistor_values()
