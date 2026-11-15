"""AI rate limit: off by default, 429 once a visitor's hourly budget is spent."""
from fastapi.testclient import TestClient

import netproof.server as server


def test_ai_rate_limit(monkeypatch):
    client = TestClient(server.app)
    server._AI_HITS.clear()
    server._AI_DAY.clear()
    # Off by default: no 429 no matter how many calls.
    for _ in range(3):
        assert client.post("/repair", json={}).status_code != 429
    monkeypatch.setenv("NETPROOF_AI_RATE_LIMIT", "2")
    server._AI_HITS.clear()
    codes = [client.post("/repair", json={}).status_code for _ in range(3)]
    assert codes[:2] != [429, 429] and codes[2] == 429
    # Mock runs never call the LLM, so they are never limited.
    assert client.post("/repair?mock=true", json={}).status_code != 429
    # Simulator-only routes are never limited.
    assert client.post("/check", json={"components": []}).status_code != 429
    server._AI_HITS.clear()
    server._AI_DAY.clear()
