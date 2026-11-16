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


class SeqClient:
    """Thread-safe fake: replies by keyword in the prompt (analyst vs designer strategy)."""

    def __init__(self, designs):
        import threading
        self.designs, self.lock, self.calls, self.messages = designs, threading.Lock(), [], self

    def create(self, **kw):
        content = kw["messages"][-1]["content"]
        text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        with self.lock:
            self.calls.append(text)
        for key, reply in self.designs.items():  # designer prompts (they also quote the analyst notes)
            if key in text:
                return reply
        if "analyst" in text:
            return "notes"
        return "not json"


def test_team_picks_best_verified_design():
    good = (FIX / "optics_good.json").read_text()
    stray = (FIX / "optics_stray.json").read_text()
    client = SeqClient({"faithful": stray, "fewest parts": good, "safety first": good, "as much power": stray})
    events, meta = [], {}
    d = design_ai.propose_team("optics", "periscope", b"\x89PNG\r\n\x1a\n" + b"0" * 32, client=client, on_event=events.append, meta=meta)
    assert d["title"].startswith("Periscope")  # a PASS candidate won over the STOP ones
    assert len(meta["leaderboard"]) == 4 and meta["leaderboard"][0]["verdict"] == "PASS"
    assert sum("analyst for a" in c for c in client.calls) == 3
    assert any(e.get("agent") == "Judge" for e in events)


def test_team_survives_failing_designers():
    good = (FIX / "fiber_good.json").read_text()
    client = SeqClient({"safety first": good})  # the other three return junk twice
    d = design_ai.propose_team("fiber", "lab link", client=client)
    assert d["elements"][0]["type"] == "laser"
