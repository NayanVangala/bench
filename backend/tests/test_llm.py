"""Tests for backend/netproof/llm.py — fake Anthropic client only.

No network, no API key, no real SDK call. Every test passes a stub
client whose ``client.messages.create(...)`` returns canned reply text
in the shape the real Messages API returns (``.content[0].text``).
The module under test is imported directly; ``anthropic`` is never
imported because ``client`` is always supplied (``_get_client`` only
builds a real client when ``client=None``).

Covered, per the llm.py contract:
- valid JSON netlist parsed (design, read_schematic)
- invalid JSON / invalid netlist: exactly one retry, then ValueError
- repair: edited netlist + exactly one reason per changed component
- design: returns a Netlist for a plain-English goal
- input validation that must raise before any model call
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netproof.ir import Finding, Netlist, Severity
from netproof.llm import MAX_TOKENS, MODEL, design, read_schematic, repair

FIXTURES = Path(__file__).parent / "fixtures"

# A minimal valid netlist payload, as the model would return it.
VALID_NETLIST = {
    "components": [
        {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
        {"id": "R1", "type": "resistor", "value": 470.0, "nodes": ["vcc", "led"]},
        {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"], "color": "red"},
    ],
    "title": "9V LED circuit",
}

# Same circuit with the resistor changed (the repair under test).
REPAIRED_NETLIST = {
    "components": [
        {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
        {"id": "R1", "type": "resistor", "value": 470.0, "nodes": ["vcc", "led"]},
        {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"], "color": "red"},
    ],
    "title": "9V LED circuit",
}

# Input netlist for repair tests: R1 is 10 ohms, far too small for 9V.
BROKEN_NETLIST = {
    "components": [
        {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
        {"id": "R1", "type": "resistor", "value": 10.0, "nodes": ["vcc", "led"]},
        {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"], "color": "red"},
    ],
    "title": "9V LED circuit",
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    """Mimics an Anthropic message: ``.content[0].text`` is the reply."""

    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _Messages:
    def __init__(self, fake: "FakeClient") -> None:
        self._fake = fake

    def create(self, **kwargs):
        self._fake.calls.append(kwargs)
        if not self._fake.replies:
            raise AssertionError("fake client ran out of canned replies")
        return _Response(self._fake.replies.pop(0))


class FakeClient:
    """Stub Anthropic client: replies are served in order, one per call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = _Messages(self)


class _StringMessages:
    """Variant fake whose create() returns a bare string reply."""

    def __init__(self, fake: "StringReplyClient") -> None:
        self._fake = fake

    def create(self, **kwargs):
        self._fake.calls.append(kwargs)
        return self._fake.replies.pop(0)


class StringReplyClient:
    """Fake client returning a plain string instead of a response object."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = _StringMessages(self)


def _finding() -> Finding:
    return Finding(
        severity=Severity.STOP,
        rule_id="resistor_power",
        component_ids=["R1"],
        message="R1 dissipates 5.00 W, over its 0.25 W rating",
    )


def _repair_reply(netlist: dict, reasons: list[str]) -> str:
    return json.dumps({"netlist": netlist, "reasons": reasons})


# ---------------------------------------------------------------------------
# design: valid JSON parsed
# ---------------------------------------------------------------------------


def test_design_valid_json_returns_netlist():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    result = design("light a red LED from a 9V battery", client=client)

    assert isinstance(result, Netlist)
    assert result.title == "9V LED circuit"
    assert [c.id for c in result.components] == ["V1", "R1", "D1"]
    assert result.get("R1").value == 470.0
    assert result.get("D1").type.value == "led"
    assert result.has_ground()
    assert len(client.calls) == 1


def test_design_calls_model_with_expected_params_and_goal():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    design("light a red LED from a 9V battery", client=client)

    call = client.calls[0]
    assert call["model"] == MODEL
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == MAX_TOKENS
    assert call["max_tokens"] == 4096
    assert len(call["messages"]) == 1
    assert "light a red LED from a 9V battery" in call["messages"][0]["content"]


def test_design_accepts_plain_string_reply():
    client = StringReplyClient([json.dumps(VALID_NETLIST)])
    result = design("light a red LED from a 9V battery", client=client)
    assert isinstance(result, Netlist)
    assert result.get("R1").value == 470.0


def test_design_accepts_fenced_json_reply():
    fenced = "```json\n" + json.dumps(VALID_NETLIST) + "\n```"
    client = FakeClient([fenced])
    result = design("light a red LED from a 9V battery", client=client)
    assert isinstance(result, Netlist)
    assert len(client.calls) == 1


def test_design_empty_goal_raises_without_calling_model():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    with pytest.raises(ValueError, match="non-empty goal"):
        design("   ", client=client)
    assert client.calls == []


# ---------------------------------------------------------------------------
# read_schematic: valid JSON parsed
# ---------------------------------------------------------------------------


def test_read_schematic_valid_json_returns_netlist():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    result = read_schematic(PNG_BYTES, client=client)

    assert isinstance(result, Netlist)
    assert result.get("V1").value == 9.0
    assert result.has_ground()
    assert len(client.calls) == 1


def test_read_schematic_sends_image_block_with_png_media_type():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    read_schematic(PNG_BYTES, client=client)

    content = client.calls[0]["messages"][0]["content"]
    image_block = content[0]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"]  # non-empty base64 payload
    assert "Transcribe" in content[1]["text"]


def test_read_schematic_empty_bytes_raises_without_calling_model():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    with pytest.raises(ValueError, match="non-empty image bytes"):
        read_schematic(b"", client=client)
    assert client.calls == []


def test_read_schematic_unsupported_format_raises_without_calling_model():
    client = FakeClient([json.dumps(VALID_NETLIST)])
    with pytest.raises(ValueError, match="unsupported image format"):
        read_schematic(b"not an image at all", client=client)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Invalid JSON / invalid netlist: exactly one retry, then raise
# ---------------------------------------------------------------------------


def test_design_invalid_json_retries_once_then_raises():
    client = FakeClient(["this is not json", "still not json {"])
    with pytest.raises(ValueError, match="after one retry"):
        design("light a red LED", client=client)
    assert len(client.calls) == 2


def test_design_invalid_json_then_valid_succeeds_on_retry():
    client = FakeClient(["not json", json.dumps(VALID_NETLIST)])
    result = design("light a red LED", client=client)

    assert isinstance(result, Netlist)
    assert result.get("R1").value == 470.0
    assert len(client.calls) == 2
    # The retry feeds the first error back to the model.
    retry_messages = client.calls[1]["messages"]
    assert len(retry_messages) == 2
    assert "was not valid" in retry_messages[1]["content"]


def test_design_valid_json_but_invalid_netlist_retries_once_then_raises():
    bad = {"components": [{"id": "R1", "type": "resistor", "value": -5.0, "nodes": ["a", "b"]}]}
    client = FakeClient([json.dumps(bad), json.dumps(bad)])
    with pytest.raises(ValueError, match="after one retry"):
        design("light a red LED", client=client)
    assert len(client.calls) == 2


def test_read_schematic_invalid_json_retries_once_then_raises():
    client = FakeClient(["garbage", "more garbage"])
    with pytest.raises(ValueError, match="after one retry"):
        read_schematic(PNG_BYTES, client=client)
    assert len(client.calls) == 2


def test_repair_invalid_json_retries_once_then_raises():
    client = FakeClient(["not json", "{ broken"])
    with pytest.raises(ValueError, match="after one retry"):
        repair(BROKEN_NETLIST, [_finding()], client=client)
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# repair: edited netlist + one reason per change
# ---------------------------------------------------------------------------


def test_repair_returns_edited_netlist_and_one_reason_per_change():
    reply = _repair_reply(
        REPAIRED_NETLIST,
        ["R1 changed from 10 ohms to 470 ohms to keep LED current under 20 mA."],
    )
    client = FakeClient([reply])
    repaired, reasons = repair(BROKEN_NETLIST, [_finding()], client=client)

    assert isinstance(repaired, Netlist)
    assert repaired.get("R1").value == 470.0
    # Untouched components are preserved exactly.
    assert repaired.get("V1").value == 9.0
    assert repaired.get("D1").value == 2.0
    assert len(reasons) == 1
    assert "R1" in reasons[0]
    assert len(client.calls) == 1


def test_repair_unchanged_netlist_returns_empty_reasons():
    reply = _repair_reply(BROKEN_NETLIST, [])
    client = FakeClient([reply])
    repaired, reasons = repair(BROKEN_NETLIST, [_finding()], client=client)

    assert repaired.get("R1").value == 10.0
    assert reasons == []


def test_repair_accepts_fixture_netlist_and_string_finding():
    fixture = json.loads((FIXTURES / "good_led.json").read_text())
    reply = _repair_reply(fixture, [])
    client = FakeClient([reply])
    repaired, reasons = repair(fixture, ["R1 looks fine"], client=client)

    assert isinstance(repaired, Netlist)
    assert repaired.title == "good LED 9V"
    assert reasons == []


def test_repair_accepts_dict_form_reasons():
    reply = json.dumps(
        {
            "netlist": REPAIRED_NETLIST,
            "reasons": [{"id": "R1", "reason": "R1 raised to 470 ohms to limit current."}],
        }
    )
    client = FakeClient([reply])
    _, reasons = repair(BROKEN_NETLIST, [_finding()], client=client)
    assert reasons == ["R1 raised to 470 ohms to limit current."]


def test_repair_reason_count_mismatch_then_fixed_on_retry():
    wrong = _repair_reply(REPAIRED_NETLIST, [])  # 1 change, 0 reasons
    right = _repair_reply(
        REPAIRED_NETLIST,
        ["R1 changed from 10 ohms to 470 ohms to keep LED current under 20 mA."],
    )
    client = FakeClient([wrong, right])
    repaired, reasons = repair(BROKEN_NETLIST, [_finding()], client=client)

    assert repaired.get("R1").value == 470.0
    assert len(reasons) == 1
    assert len(client.calls) == 2


def test_repair_reason_count_mismatch_twice_then_raises():
    wrong = _repair_reply(REPAIRED_NETLIST, [])  # 1 change, 0 reasons
    client = FakeClient([wrong, wrong])
    with pytest.raises(ValueError, match="after one retry"):
        repair(BROKEN_NETLIST, [_finding()], client=client)
    assert len(client.calls) == 2


def test_repair_empty_findings_raises_without_calling_model():
    client = FakeClient([_repair_reply(REPAIRED_NETLIST, ["x"])])
    with pytest.raises(ValueError, match="non-empty list of findings"):
        repair(BROKEN_NETLIST, [], client=client)
    assert client.calls == []


def test_repair_invalid_input_netlist_raises_without_calling_model():
    bad = {"components": [{"id": "R1", "type": "resistor", "value": -5.0, "nodes": ["a", "b"]}]}
    client = FakeClient([_repair_reply(REPAIRED_NETLIST, ["x"])])
    with pytest.raises(ValueError, match="failed validation"):
        repair(bad, [_finding()], client=client)
    assert client.calls == []
