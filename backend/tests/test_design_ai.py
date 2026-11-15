"""AI design for fiber/optics: valid JSON passes through, invalid gets one retry, then a loud error."""
import json
from pathlib import Path

import pytest

from netproof import design_ai

FIX = Path(__file__).parent / "fixtures"


class FakeClient:
    """Mimics client.messages.create; replies with the queued texts in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def test_fiber_valid_first_try():
    good = (FIX / "fiber_good.json").read_text()
    c = FakeClient(good)
    d = design_ai.propose("fiber", "lab link", client=c)
    assert d["elements"][0]["type"] == "laser" and len(c.calls) == 1


def test_optics_retry_then_valid():
    good = (FIX / "optics_good.json").read_text()
    c = FakeClient("not json at all", "```json\n" + good + "\n```")
    d = design_ai.propose("optics", "periscope", client=c)
    assert d["elements"][1]["id"] == "M1" and len(c.calls) == 2


def test_invalid_twice_raises():
    bad = json.dumps({"elements": [{"id": "X", "type": "unicorn"}]})
    with pytest.raises(ValueError, match="after one retry"):
        design_ai.propose("fiber", "x", client=FakeClient(bad, bad))


def test_needs_goal_or_image():
    with pytest.raises(ValueError):
        design_ai.propose("optics", "  ", client=FakeClient())
