"""Bench backlog #9 (1/3): POST /optics/design/stream emits Judge + Physics events.

Fake LLM only: ``netproof.llm._get_client`` is monkeypatched to return the
keyword-routing fake client from ``test_design_ai.py`` (SeqClient), so no
real Anthropic/Gemini call is ever made. The real ``design_ai.propose_team``
team runs (4 designers in parallel, best verified design wins), the real
server SSE adapter (``_run_streamed``) streams it, and the real
``optics.trace_bench`` physics check runs on the winner.

Pinned events:
* at least one event with ``agent == "Judge"`` (emitted by propose_team
  when it picks the winning designer),
* at least one event with ``agent == "Physics"`` (emitted by the server's
  ``design_stream`` work function around the ray trace),
* a terminal ``stage == "done"`` event carrying ``design`` + ``report``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netproof import llm as llm_module
from netproof import server as server_module

FIX = Path(__file__).parent / "fixtures"


class SeqClient:
    """Thread-safe fake, copied from tests/test_design_ai.py: replies by
    keyword in the prompt (analyst vs designer strategy)."""

    def __init__(self, designs):
        import threading

        self.designs, self.lock, self.calls, self.messages = (
            designs,
            threading.Lock(),
            [],
            self,
        )

    def create(self, **kw):
        content = kw["messages"][-1]["content"]
        text = (
            content
            if isinstance(content, str)
            else " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        )
        with self.lock:
            self.calls.append(text)
        for key, reply in self.designs.items():
            if key in text:
                return reply
        if "analyst" in text:
            return "notes"
        return "not json"


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(line[5:])
        for line in text.splitlines()
        if line.startswith("data:")
    ]


def test_optics_design_stream_emits_judge_and_physics(monkeypatch):
    """POST /optics/design/stream streams Judge + Physics events, then done."""
    good = (FIX / "optics_good.json").read_text()
    stray = (FIX / "optics_stray.json").read_text()
    fake = SeqClient(
        {
            "faithful": stray,
            "fewest parts": good,
            "safety first": good,
            "as much power": stray,
        }
    )
    # Never build a real client: _get_client is the only place a real
    # Anthropic/Gemini client is constructed. Return the fake (or the
    # already-injected fake) for every call.
    monkeypatch.setattr(
        llm_module,
        "_get_client",
        lambda client=None, provider=None: client or fake,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    resp = TestClient(server_module.app).post(
        "/optics/design/stream", data={"goal": "periscope"}
    )
    assert resp.status_code == 200, resp.text[:500]
    events = _sse_events(resp.text)
    assert events, "expected SSE data: frames from /optics/design/stream"

    judge = [e for e in events if e.get("agent") == "Judge"]
    assert judge, f"no Judge event in stream: {events!r}"
    assert judge[0].get("stage") == "verify"
    assert "Picked Designer" in judge[0].get("message", "")

    physics = [e for e in events if e.get("agent") == "Physics"]
    assert len(physics) >= 1, f"no Physics event in stream: {events!r}"
    assert all(e.get("stage") == "verify" for e in physics)
    # Server emits Physics twice: working, then done with the verdict.
    assert {e.get("state") for e in physics} == {"working", "done"}
    assert any("Verdict" in e.get("message", "") for e in physics)

    assert not any(e.get("stage") == "error" for e in events), events
    done = [e for e in events if e.get("stage") == "done"]
    assert len(done) == 1, f"expected exactly one done event, got {events!r}"
    assert done[0]["design"]["title"].startswith("Periscope")
    assert done[0]["report"]["verdict"] == "PASS"
    # The fake client really was used (4 designers, no real API call).
    assert len(fake.calls) == 4, f"expected 4 designer calls, got {len(fake.calls)}"
