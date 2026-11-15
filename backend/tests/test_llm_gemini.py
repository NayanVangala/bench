"""Tests for the Gemini provider path in backend/netproof/llm.py.

Fake genai client only: no network, no API key, no real SDK call at
the transport layer. The real ``google.genai.types`` module IS used
when installed (so Part/Content/Config attribute names are verified
against the real SDK — see ``test_real_sdk_types_surface``); when it
is absent the tests use a small fake types module with the same call
surface, so the suite still runs with no SDK and no key.

Covered:
- get_provider: default gemini, env override, explicit arg wins,
  case-insensitive, unknown raises loudly
- _get_client dispatch: gemini default, anthropic via
  LLM_PROVIDER=anthropic, GEMINI_MODEL override, genai.Client()
  built with no args (the SDK
  reads GEMINI_API_KEY / GOOGLE_API_KEY itself)
- _GeminiClientAdapter: all messages collapse to one Content
  role=user, text -> Part.from_text, image -> Part.from_bytes with
  base64 decoded + MIME passed through, model substitution to
  gemini-4-argon, max_tokens -> max_output_tokens, JSON response
  mode, reply wrapped as .content[0].text, GIF rejected loudly
- injected clients always win (even with an unknown provider set),
  and a bare genai client passed as client= is detected by .models
  (no .messages) and wrapped
- repair works through the adapter in JSON response mode
"""

from __future__ import annotations

import base64
import inspect
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from netproof.ir import Finding, Netlist, Severity
from netproof.llm import (
    GEMINI_MODEL,
    MAX_TOKENS,
    MODEL,
    _GeminiClientAdapter,
    _gemini_response_text,
    design,
    get_provider,
    read_schematic,
    repair,
)

# Real SDK types when installed; None when the SDK is absent (the
# fake types module below stands in, with the same call surface).
try:
    from google.genai import types as _REAL_TYPES
except ImportError:  # pragma: no cover - SDK not installed
    _REAL_TYPES = None

VALID_NETLIST = {
    "components": [
        {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
        {"id": "R1", "type": "resistor", "value": 470.0, "nodes": ["vcc", "led"]},
        {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"], "color": "red"},
    ],
    "title": "9V LED circuit",
}

REPAIRED_NETLIST = {
    "components": [
        {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
        {"id": "R1", "type": "resistor", "value": 470.0, "nodes": ["vcc", "led"]},
        {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"], "color": "red"},
    ],
    "title": "9V LED circuit",
}

BROKEN_NETLIST = {
    "components": [
        {"id": "V1", "type": "voltage_source", "value": 9.0, "nodes": ["vcc", "0"]},
        {"id": "R1", "type": "resistor", "value": 10.0, "nodes": ["vcc", "led"]},
        {"id": "D1", "type": "led", "value": 2.0, "nodes": ["led", "0"], "color": "red"},
    ],
    "title": "9V LED circuit",
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GIF_BYTES = b"GIF89a" + b"\x00" * 16


def _finding() -> Finding:
    return Finding(
        severity=Severity.STOP,
        rule_id="resistor_power",
        component_ids=["R1"],
        message="R1 dissipates 5.00 W, over its 0.25 W rating",
    )


# ---------------------------------------------------------------------------
# Fake types module (used only when the real google-genai SDK is absent)
# ---------------------------------------------------------------------------


class _FakeInlineData:
    def __init__(self, data: bytes, mime_type: str) -> None:
        self.data = data
        self.mime_type = mime_type


class _FakePart:
    def __init__(self, text: str | None = None, inline_data=None) -> None:
        self.text = text
        self.inline_data = inline_data

    @classmethod
    def from_text(cls, *, text: str) -> "_FakePart":
        return cls(text=text)

    @classmethod
    def from_bytes(cls, *, data: bytes, mime_type: str) -> "_FakePart":
        return cls(inline_data=_FakeInlineData(data, mime_type))


class _FakeContent:
    def __init__(self, role: str, parts: list) -> None:
        self.role = role
        self.parts = list(parts)


class _FakeConfig:
    def __init__(
        self, max_output_tokens: int | None = None, response_mime_type: str | None = None
    ) -> None:
        self.max_output_tokens = max_output_tokens
        self.response_mime_type = response_mime_type


class _FakeTypes:
    Part = _FakePart
    Content = _FakeContent
    GenerateContentConfig = _FakeConfig


@pytest.fixture
def types_module():
    """Real google.genai.types when installed, else the fake module."""
    return _REAL_TYPES if _REAL_TYPES is not None else _FakeTypes


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


class _FakeGenaiResponse:
    """Mimics a Gemini response: ``.text`` is the reply."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, fake: "FakeGeminiClient") -> None:
        self._fake = fake

    def generate_content(self, **kwargs):
        self._fake.calls.append(kwargs)
        if not self._fake.replies:
            raise AssertionError("fake gemini client ran out of canned replies")
        return _FakeGenaiResponse(self._fake.replies.pop(0))


class FakeGeminiClient:
    """Stands in for google.genai.Client: .models.generate_content only.

    Deliberately has NO ``.messages`` attribute: that is how
    ``_get_client`` recognises a bare genai client and wraps it.
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.models = _FakeModels(self)


class _FakeGenaiModule:
    """Stands in for the ``google.genai`` module: ``Client()`` builds the fake."""

    def __init__(self, client: FakeGeminiClient, types_mod) -> None:
        self._client = client
        self.types = types_mod
        self.client_kwargs: dict | None = None

    def Client(self, **kwargs):
        self.client_kwargs = dict(kwargs)
        return self._client


class _FakeAnthropicModule:
    """Stands in for the ``anthropic`` module: ``Anthropic()`` builds the fake."""

    def __init__(self, client) -> None:
        self._client = client
        self.kwargs: dict | None = None

    def Anthropic(self, **kwargs):
        self.kwargs = dict(kwargs)
        return self._client


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AnthropicResponse:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _AnthropicMessages:
    def __init__(self, fake) -> None:
        self._fake = fake

    def create(self, **kwargs):
        self._fake.calls.append(kwargs)
        return _AnthropicResponse(self._fake.replies.pop(0))


class FakeAnthropicClient:
    """Minimal Anthropic-shaped stub (mirrors test_llm.FakeClient)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = _AnthropicMessages(self)


def _install_fake_genai(monkeypatch, module: _FakeGenaiModule) -> None:
    google_pkg = ModuleType("google")
    google_pkg.genai = module
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", module)


# ---------------------------------------------------------------------------
# get_provider
# ---------------------------------------------------------------------------


def test_get_provider_defaults_to_gemini(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert get_provider() == "gemini"
    assert get_provider(None) == "gemini"


def test_get_provider_env_and_arg(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert get_provider() == "gemini"
    # Explicit argument wins over the environment.
    assert get_provider("anthropic") == "anthropic"
    assert get_provider("gemini") == "gemini"


def test_get_provider_case_insensitive(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "  GEMINI  ")
    assert get_provider() == "gemini"
    assert get_provider("AnThRoPiC") == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "")
    assert get_provider() == "gemini"


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider("bogus-provider")
    with pytest.raises(ValueError, match="expected 'anthropic' or 'gemini'"):
        get_provider("openai")


# ---------------------------------------------------------------------------
# _get_client dispatch
# ---------------------------------------------------------------------------


def test_provider_dispatch_anthropic_explicit(monkeypatch):
    fake = FakeAnthropicClient([json.dumps(VALID_NETLIST)])
    module = _FakeAnthropicModule(fake)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setitem(sys.modules, "anthropic", module)

    result = design("light a red LED", client=None)

    assert isinstance(result, Netlist)
    assert result.get("R1").value == 470.0
    # SDK built with no args: it reads ANTHROPIC_API_KEY itself.
    assert module.kwargs == {}
    assert fake.calls[0]["model"] == MODEL


def test_provider_dispatch_gemini_default(monkeypatch, types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    module = _FakeGenaiModule(genai_client, types_module)
    _install_fake_genai(monkeypatch, module)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    result = design("light a red LED", client=None)

    assert isinstance(result, Netlist)
    assert result.get("R1").value == 470.0
    # genai.Client() built with no args: the SDK reads GEMINI_API_KEY
    # (or GOOGLE_API_KEY) from the environment itself.
    assert module.client_kwargs == {}
    assert len(genai_client.calls) == 1
    assert genai_client.calls[0]["model"] == "gemini-4-argon"


def test_provider_dispatch_gemini(monkeypatch, types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    module = _FakeGenaiModule(genai_client, types_module)
    _install_fake_genai(monkeypatch, module)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    result = design("light a red LED from a 9V battery", client=None)

    assert isinstance(result, Netlist)
    assert result.get("R1").value == 470.0
    # genai.Client() built with no args: the SDK reads GEMINI_API_KEY
    # (or GOOGLE_API_KEY) from the environment itself.
    assert module.client_kwargs == {}
    assert len(genai_client.calls) == 1
    call = genai_client.calls[0]
    assert call["model"] == GEMINI_MODEL
    assert call["model"] == "gemini-4-argon"
    assert call["config"].max_output_tokens == MAX_TOKENS
    assert call["config"].response_mime_type == "application/json"
    assert len(call["contents"]) == 1
    assert call["contents"][0].role == "user"
    assert "light a red LED from a 9V battery" in call["contents"][0].parts[0].text


def test_provider_gemini_model_env_override(monkeypatch, types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    module = _FakeGenaiModule(genai_client, types_module)
    _install_fake_genai(monkeypatch, module)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")

    design("light a red LED", client=None)

    assert genai_client.calls[0]["model"] == "gemini-2.5-pro"


def test_unknown_provider_raises_without_calling_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    with pytest.raises(ValueError, match="unknown LLM provider"):
        design("light a red LED", client=None)


# ---------------------------------------------------------------------------
# _GeminiClientAdapter translation
# ---------------------------------------------------------------------------


def test_adapter_translates_text_and_image_parts(types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    adapter = _GeminiClientAdapter(genai_client, types_module=types_module)
    b64 = base64.b64encode(PNG_BYTES).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                },
                {"type": "text", "text": "Transcribe this schematic"},
            ],
        }
    ]

    response = adapter.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, messages=messages
    )

    # Reply wrapped as .content[0].text, Anthropic-style.
    assert response.content[0].text == json.dumps(VALID_NETLIST)
    call = genai_client.calls[0]
    # The Anthropic model name is substituted, never sent to Gemini.
    assert call["model"] == "gemini-4-argon"
    assert call["config"].max_output_tokens == MAX_TOKENS
    assert call["config"].response_mime_type == "application/json"
    contents = call["contents"]
    assert len(contents) == 1
    assert contents[0].role == "user"
    image_part, text_part = contents[0].parts
    # Image: base64 decoded to raw bytes, MIME passed through.
    assert image_part.inline_data.mime_type == "image/png"
    assert image_part.inline_data.data == PNG_BYTES
    assert text_part.text == "Transcribe this schematic"


def test_adapter_collapses_all_messages_to_one_user_content(types_module):
    genai_client = FakeGeminiClient(["{}"])
    adapter = _GeminiClientAdapter(genai_client, types_module=types_module)
    adapter.messages.create(
        model=MODEL,
        max_tokens=10,
        messages=[
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "third"},
        ],
    )

    contents = genai_client.calls[0]["contents"]
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert [p.text for p in contents[0].parts] == ["first", "second", "ack", "third"]


def test_adapter_rejects_gif_image(types_module):
    genai_client = FakeGeminiClient(["{}"])
    adapter = _GeminiClientAdapter(genai_client, types_module=types_module)
    b64 = base64.b64encode(GIF_BYTES).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/gif", "data": b64},
                },
                {"type": "text", "text": "Transcribe this schematic"},
            ],
        }
    ]
    with pytest.raises(ValueError, match="does not accept image/gif"):
        adapter.messages.create(model=MODEL, max_tokens=10, messages=messages)
    assert genai_client.calls == []


def test_adapter_rejects_unknown_block_type(types_module):
    genai_client = FakeGeminiClient(["{}"])
    adapter = _GeminiClientAdapter(genai_client, types_module=types_module)
    messages = [{"role": "user", "content": [{"type": "video", "text": "nope"}]}]
    with pytest.raises(ValueError, match="unsupported message block type"):
        adapter.messages.create(model=MODEL, max_tokens=10, messages=messages)
    assert genai_client.calls == []


def test_gemini_response_text_falls_back_to_candidates():
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text="hello")])
            )
        ]
    )
    assert _gemini_response_text(response) == "hello"


def test_gemini_response_text_empty_raises():
    with pytest.raises(ValueError, match="contained no text"):
        _gemini_response_text(SimpleNamespace(candidates=[]))


# ---------------------------------------------------------------------------
# End to end through the public functions, via the adapter
# ---------------------------------------------------------------------------


def test_read_schematic_via_gemini_adapter(types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    adapter = _GeminiClientAdapter(genai_client, types_module=types_module)

    result = read_schematic(PNG_BYTES, client=adapter)

    assert isinstance(result, Netlist)
    assert result.get("V1").value == 9.0
    parts = genai_client.calls[0]["contents"][0].parts
    assert parts[0].inline_data.mime_type == "image/png"
    assert parts[0].inline_data.data == PNG_BYTES
    assert "Transcribe" in parts[1].text


def test_repair_via_gemini_adapter_json_mode(types_module):
    reply = json.dumps(
        {
            "netlist": REPAIRED_NETLIST,
            "reasons": ["R1 changed from 10 ohms to 470 ohms to limit LED current."],
        }
    )
    genai_client = FakeGeminiClient([reply])
    adapter = _GeminiClientAdapter(genai_client, types_module=types_module)

    repaired, reasons = repair(BROKEN_NETLIST, [_finding()], client=adapter)

    assert isinstance(repaired, Netlist)
    assert repaired.get("R1").value == 470.0
    assert len(reasons) == 1
    assert "R1" in reasons[0]
    # JSON response mode was requested for the repair call too.
    assert genai_client.calls[0]["config"].response_mime_type == "application/json"


def test_design_via_explicit_provider_arg(monkeypatch, types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    module = _FakeGenaiModule(genai_client, types_module)
    _install_fake_genai(monkeypatch, module)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    result = design("light a red LED", client=None, provider="GEMINI")

    assert isinstance(result, Netlist)
    assert genai_client.calls[0]["model"] == "gemini-4-argon"


# ---------------------------------------------------------------------------
# Injected clients always win; bare genai client is wrapped
# ---------------------------------------------------------------------------


def test_injected_anthropic_fake_bypasses_gemini_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    client = FakeAnthropicClient([json.dumps(VALID_NETLIST)])
    result = design("light a red LED", client=client)
    assert isinstance(result, Netlist)
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == MODEL


def test_injected_client_bypasses_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    client = FakeAnthropicClient([json.dumps(VALID_NETLIST)])
    result = design("light a red LED", client=client)
    assert isinstance(result, Netlist)


def test_bare_genai_client_passed_as_client_is_wrapped(monkeypatch, types_module):
    genai_client = FakeGeminiClient([json.dumps(VALID_NETLIST)])
    assert not hasattr(genai_client, "messages")
    # The adapter lazy-imports google.genai.types on first use; the
    # fake module stands in for the SDK here.
    _install_fake_genai(monkeypatch, _FakeGenaiModule(genai_client, types_module))

    result = design("light a red LED", client=genai_client, provider=None)

    assert isinstance(result, Netlist)
    assert len(genai_client.calls) == 1
    assert genai_client.calls[0]["model"] == "gemini-4-argon"


# ---------------------------------------------------------------------------
# Real SDK surface (skipped when google-genai is not installed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_REAL_TYPES is None, reason="google-genai SDK not installed")
def test_real_sdk_types_surface():
    """Pin the adapter's calls against the installed google-genai SDK.

    Verified against google-genai 3.3.0: Part.from_text(*, text),
    Part.from_bytes(*, data, mime_type), Content(role, parts),
    GenerateContentConfig(max_output_tokens, response_mime_type).
    """
    assert list(inspect.signature(_REAL_TYPES.Part.from_text).parameters) == ["text"]
    params = list(inspect.signature(_REAL_TYPES.Part.from_bytes).parameters)
    assert params[:2] == ["data", "mime_type"]
    assert {"role", "parts"} <= set(_REAL_TYPES.Content.model_fields)
    fields = set(_REAL_TYPES.GenerateContentConfig.model_fields)
    assert {"max_output_tokens", "response_mime_type"} <= fields

    part = _REAL_TYPES.Part.from_text(text="hi")
    assert part.text == "hi"
    img = _REAL_TYPES.Part.from_bytes(data=PNG_BYTES, mime_type="image/png")
    assert img.inline_data.mime_type == "image/png"
    assert img.inline_data.data == PNG_BYTES
    content = _REAL_TYPES.Content(role="user", parts=[part, img])
    assert content.role == "user" and len(content.parts) == 2
    config = _REAL_TYPES.GenerateContentConfig(
        max_output_tokens=4096, response_mime_type="application/json"
    )
    assert config.max_output_tokens == 4096
    assert config.response_mime_type == "application/json"
