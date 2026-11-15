"""Tests for netproof.bom.netlist_to_bom_csv.

Pure-function tests only: no network, no API key, no ngspice, no I/O
beyond reading the checked-in good-LED fixture.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from netproof.bom import netlist_to_bom_csv
from netproof.ir import Component, Netlist

FIXTURES = Path(__file__).parent / "fixtures"

HEADER = ["id", "type", "value", "rating", "qty"]


def _rows(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


def _resistor(comp_id: str, value: float, rating: float | None = None) -> Component:
    kwargs: dict = {"id": comp_id, "type": "resistor", "value": value,
                    "nodes": [f"n_{comp_id}_a", f"n_{comp_id}_b"]}
    if rating is not None:
        kwargs["rating_watts"] = rating
    return Component(**kwargs)


def _good_led_netlist() -> Netlist:
    with open(FIXTURES / "good_led.json", encoding="utf-8") as fh:
        return Netlist.model_validate(json.load(fh))


def test_header_is_id_type_value_rating_qty():
    assert netlist_to_bom_csv(Netlist(components=[])) == (
        "id,type,value,rating,qty\n"
    )


def test_empty_netlist_yields_header_only():
    rows = _rows(netlist_to_bom_csv(Netlist(components=[])))
    assert rows == [HEADER]


def test_good_led_fixture_gives_three_rows_plus_header():
    rows = _rows(netlist_to_bom_csv(_good_led_netlist()))
    assert rows[0] == HEADER
    assert len(rows) == 4  # header + V1 + R1 + D1


def test_good_led_rows_have_expected_content_in_first_seen_order():
    rows = _rows(netlist_to_bom_csv(_good_led_netlist()))
    assert rows[1:] == [
        ["V1", "voltage_source", "9", "", "1"],
        ["R1", "resistor", "470", "0.25", "1"],
        ["D1", "led", "2", "", "1"],
    ]


def test_good_led_fixture_accepted_as_plain_dict():
    with open(FIXTURES / "good_led.json", encoding="utf-8") as fh:
        raw = json.load(fh)
    assert netlist_to_bom_csv(raw) == netlist_to_bom_csv(_good_led_netlist())


def test_identical_type_and_value_combine_into_one_row():
    netlist = Netlist(components=[
        _resistor("R1", 470.0),
        _resistor("R2", 470.0),
    ])
    rows = _rows(netlist_to_bom_csv(netlist))
    assert rows == [
        HEADER,
        ["R1", "resistor", "470", "0.25", "2"],
    ]


def test_three_identical_parts_count_qty_three():
    netlist = Netlist(components=[
        _resistor("R1", 1000.0),
        _resistor("R2", 1000.0),
        _resistor("R3", 1000.0),
    ])
    rows = _rows(netlist_to_bom_csv(netlist))
    assert rows == [
        HEADER,
        ["R1", "resistor", "1000", "0.25", "3"],
    ]


def test_different_values_do_not_combine():
    netlist = Netlist(components=[
        _resistor("R1", 470.0),
        _resistor("R2", 1000.0),
    ])
    rows = _rows(netlist_to_bom_csv(netlist))
    assert rows == [
        HEADER,
        ["R1", "resistor", "470", "0.25", "1"],
        ["R2", "resistor", "1000", "0.25", "1"],
    ]


def test_same_value_different_type_does_not_combine():
    netlist = Netlist(components=[
        _resistor("R1", 2.0),
        Component(id="D1", type="led", value=2.0,
                  nodes=["led_a", "led_b"]),
    ])
    rows = _rows(netlist_to_bom_csv(netlist))
    assert rows == [
        HEADER,
        ["R1", "resistor", "2", "0.25", "1"],
        ["D1", "led", "2", "", "1"],
    ]


def test_grouped_rating_uses_first_seen_rating():
    netlist = Netlist(components=[
        _resistor("R1", 470.0, rating=0.25),
        _resistor("R2", 470.0, rating=0.5),
    ])
    rows = _rows(netlist_to_bom_csv(netlist))
    assert rows == [
        HEADER,
        ["R1", "resistor", "470", "0.25", "2"],
    ]


def test_rows_preserve_first_seen_order():
    netlist = Netlist(components=[
        Component(id="D1", type="led", value=2.0,
                  nodes=["led_a", "led_b"]),
        _resistor("R1", 470.0),
        Component(id="V1", type="voltage_source", value=9.0,
                  nodes=["vcc", "0"]),
    ])
    rows = _rows(netlist_to_bom_csv(netlist))
    assert [row[0] for row in rows[1:]] == ["D1", "R1", "V1"]
