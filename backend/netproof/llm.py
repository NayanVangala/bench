"""LLM access for Netproof — the ONLY module that talks to an LLM.

Default provider: Google Gemini via the official ``google-genai``
SDK, model ``gemini-4-argon`` (override with ``GEMINI_MODEL`` env).
Fallback provider: Anthropic official SDK, model
``claude-haiku-5-5``, selected per call by ``LLM_PROVIDER``
(``gemini`` by default, ``anthropic`` for Anthropic;
case-insensitive). No LangChain, no other
provider, no hardcoded netlists in any non-mock path. API keys come
from the environment and are read by the respective SDK itself
(``ANTHROPIC_API_KEY`` for Anthropic; ``GEMINI_API_KEY`` for Gemini,
falling back to ``GOOGLE_API_KEY`` — in google-genai 3.3.0 the SDK
prefers ``GOOGLE_API_KEY`` when both are set, so set only one key);
this module never
reads, prints or logs a key value.

Import safety
------------
Both SDKs are imported lazily inside :func:`_get_client` /
:func:`_get_gemini_client`, so importing this module — and running
tests with a fake client — works with no SDK installed and no key
set. Only a real call with ``client=None`` needs the SDK and the key
for the selected provider. ``google-genai`` is an optional
dependency (``uv sync --extra gemini``).

Injectable client (for tests)
-----------------------------
Every public function takes ``client=None`` plus ``provider=None``.
Pass a fake whose ``client.messages.create(model=..., max_tokens=...,
messages=[...])`` returns an object whose ``.content[0].text`` is the
model reply (a plain ``.text`` attribute or a plain string reply also
work). If ``client`` is ``None``, a real client is built for the
provider resolved by :func:`get_provider`. An injected client always
wins and bypasses provider selection entirely — even an unknown
``LLM_PROVIDER`` value cannot break a call that injects a client.
One exception: a bare ``google.genai`` client passed as ``client=``
has ``.models`` and no ``.messages``, so it is wrapped in a
:class:`_GeminiClientAdapter` to satisfy the Anthropic-shaped call.

Gemini adapter
--------------
:class:`_GeminiClientAdapter` wraps a ``google.genai.Client`` and
exposes the same ``.messages.create(model=..., max_tokens=...,
messages=...)`` shape the rest of this module uses, so
``_call_model`` is provider-agnostic. It collapses all messages into
one ``types.Content(role="user", ...)`` (text via
``Part.from_text``, image blocks via ``Part.from_bytes`` with the
base64 payload decoded and the MIME type passed through), substitutes
its own model name (the Anthropic model name passed in is meaningless
to Gemini), and maps ``max_tokens`` to ``max_output_tokens`` in a
``GenerateContentConfig`` with ``response_mime_type="application/json"``.
Gemini's documented image formats are PNG, JPEG, WEBP, HEIC and HEIF;
``image/gif`` is rejected loudly instead of failing as a cryptic API
400. The reply comes back through ``response.text`` and is wrapped as
``.content[0].text``.

Failure policy
--------------
Invalid JSON or a reply that fails Pydantic validation gets exactly
one retry (the error is fed back and valid JSON is demanded again).
If the retry also fails, a ``ValueError`` is raised loudly. There is
never a silent fallback, default, or guessed netlist. An unknown
provider value likewise raises ``ValueError`` loudly — never a silent
fallback to another provider.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .ir import ComponentType, Finding, Netlist

__all__ = [
    "MODEL",
    "GEMINI_MODEL",
    "MAX_TOKENS",
    "UnmodeledPart",
    "SchematicNetlist",
    "get_provider",
    "read_schematic",
    "design",
    "repair",
]

MODEL = "claude-haiku-5-5"
# Default Gemini model; override per call with the GEMINI_MODEL env var.
GEMINI_MODEL = "gemini-4-argon"
# Haiku 5.5 thinks by default and thinking counts toward max_tokens: leave
# room so a large design is never cut off mid-JSON.
MAX_TOKENS = 16000

# Allowed component types, from ir.ComponentType. Listed in prompts so
# the model only emits types the IR accepts.
_TYPE_NAMES = ", ".join(f'"{t.value}"' for t in ComponentType)

# The exact JSON shape the model must return. Field names match ir.py:
# rating is `rating_watts` (watts), LED colour is `color`. No concrete
# component values appear here — this is schema, not a netlist.
_SCHEMA_TEXT = f"""Return ONLY strict JSON matching this schema, with no
prose, no markdown, no code fences, no commentary before or after:

{{
  "components": [
    {{
      "id": "string, e.g. a reference designator",
      "type": one of {_TYPE_NAMES},
      "value": number in SI units (ohms for resistor, farads for capacitor,
                volts for voltage_source and for an LED forward voltage,
                amps for current_source; strictly positive for resistor,
                capacitor, led and diode),
      "nodes": ["exactly two distinct node names, e.g. anode then cathode
                for a diode/LED, + then - for a source; \\"0\\" is ground"],
      "rating_watts": number greater than 0, or null (omit unless known;
                     resistors default to 0.25 W),
      "color": "LED colour such as red, green, blue, white or yellow,
                or null (only for type \\"led\\"; defaults to red)"
    }}
  ],
  "title": "optional short string"
}}

Rules: every component needs exactly two distinct nodes; component ids
must be unique; include ground node "0" somewhere in the circuit; use
SI numbers only, never strings like "1k" or "10uF" for value."""

_RETRY_TEXT = (
    "Your previous reply was not valid. {error} "
    "Return ONLY strict JSON matching the schema above, with no prose, "
    "no markdown, no code fences, and no commentary."
)


class UnmodeledPart(BaseModel):
    """One part seen on a schematic that the circuit model cannot
    simulate, so it is listed instead of silently dropped.

    ``id`` is the part's reference designator (e.g. ``"U1"``);
    ``type`` is what kind of part it is (e.g. ``"microcontroller"``,
    ``"transistor"``, ``"crystal"``, ``"connector"``); ``note`` is a
    short plain-English reason it cannot be simulated.
    """

    model_config = {"frozen": True}

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    note: str = ""

    @field_validator("id", "type", "note", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class SchematicNetlist(Netlist):
    """A :class:`Netlist` transcribed from a schematic image, plus
    the parts on that schematic the circuit model cannot simulate.

    A plain :class:`Netlist` in every other respect (``isinstance``
    checks, validation, ``model_dump`` all behave as before), so
    existing callers keep working; ``unmodeled`` is empty when every
    part on the schematic was modeled.
    """

    unmodeled: list[UnmodeledPart] = Field(default_factory=list)


# Extra schema text for read_schematic only (design/repair never see
# it): the model must list the parts it cannot model instead of
# silently dropping them.
_UNMODELED_TEXT = """Also list every part on the schematic that the netlist above
cannot represent — integrated circuits, transistors, crystals,
connectors, and anything else with no simulatable model — in an
"unmodeled" array:

  "unmodeled": [
    {"id": "U1", "type": "microcontroller",
     "note": "ATmega328P; no SPICE model in the circuit model"},
    {"id": "Q1", "type": "transistor",
     "note": "2N2222 NPN; transistors are not modelable"}
  ]

Each entry needs "id" (its reference designator) and "type" (what kind
of part it is); "note" is a short plain-English reason it cannot be
simulated. Never silently drop such a part: if it is on the schematic
and not in "components", it must appear in "unmodeled". Omit the
"unmodeled" key, or use an empty array, when every part is modeled."""


# ---------------------------------------------------------------------------
# Client handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Provider selection + Gemini adapter
# ---------------------------------------------------------------------------


def get_provider(provider: str | None = None) -> str:
    """Resolve the LLM provider name: ``"anthropic"`` or ``"gemini"``.

    An explicit argument wins; otherwise ``LLM_PROVIDER`` from the
    environment; otherwise ``"gemini"``. Matching is
    case-insensitive and surrounding whitespace is ignored. Any other
    value raises ``ValueError`` loudly — never a silent fallback to
    another provider.
    """
    raw = provider if provider is not None else os.environ.get("LLM_PROVIDER")
    resolved = (raw or "gemini").strip().lower()
    if resolved not in ("anthropic", "gemini"):
        raise ValueError(
            f"unknown LLM provider: {resolved!r} "
            "(expected 'anthropic' or 'gemini'; "
            "set LLM_PROVIDER to one of those)"
        )
    return resolved


def _resolve_gemini_model() -> str:
    """Gemini model name: ``GEMINI_MODEL`` env, else ``GEMINI_MODEL``."""
    return (os.environ.get("GEMINI_MODEL") or GEMINI_MODEL).strip() or GEMINI_MODEL


class _AdapterTextBlock:
    """Minimal text block: ``.text`` is the reply text."""

    def __init__(self, text: str) -> None:
        self.text = text


class _AdapterResponse:
    """Anthropic-shaped response shim: ``.content[0].text`` is the reply."""

    def __init__(self, text: str) -> None:
        self.content = [_AdapterTextBlock(text)]


def _gemini_response_text(response: Any) -> str:
    """Extract reply text from a Gemini GenerateContentResponse.

    Prefers the SDK's ``response.text`` accessor; falls back to
    walking ``candidates[*].content.parts``. Raises ValueError when
    the response holds no text.
    """
    text: Any = None
    try:
        text = response.text
    except (AttributeError, ValueError):
        text = None
    if isinstance(text, str) and text:
        return text
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text:
                return part_text
    raise ValueError("Gemini response contained no text")


class _GeminiClientAdapter:
    """Anthropic-Messages-shaped wrapper around a ``google.genai`` client.

    The rest of this module talks to ``client.messages.create(model=...,
    max_tokens=..., messages=...)``; this adapter is that namespace
    (``self.messages is self``) and translates each call to
    ``client.models.generate_content(...)``.

    Translation — all messages collapse to one Content
    -------------------------------------------------
    Every Anthropic message, whatever its role, is folded into a
    single ``types.Content(role="user", parts=[...])``. Netproof only
    ever sends user-role messages (design / read_schematic / repair,
    plus the user-role retry), so collapsing loses nothing; the retry
    message simply becomes a later part of the same user turn. Text
    becomes ``types.Part.from_text``; an Anthropic image block becomes
    ``types.Part.from_bytes`` with the base64 payload decoded to raw
    bytes and the MIME type passed through. Gemini's documented image
    formats are PNG, JPEG, WEBP, HEIC and HEIF — ``image/gif`` is
    rejected loudly here instead of failing as a cryptic API 400.

    Model / tokens
    --------------
    The ``model=`` argument to ``create`` is accepted for signature
    compatibility and ignored: Gemini always uses this adapter's model
    (``GEMINI_MODEL`` env, else ``"gemini-4-argon"``) — the Anthropic
    model name is meaningless to Gemini. ``max_tokens`` becomes
    ``max_output_tokens`` in a ``GenerateContentConfig`` that also sets
    ``response_mime_type="application/json"``: every Netproof prompt
    demands strict JSON, so JSON mode is correct for design,
    read_schematic and repair alike.

    The reply comes back through ``response.text`` and is wrapped as
    ``.content[0].text`` so ``_response_text`` works unchanged.

    ``types_module`` is injectable so tests can pass a fake types
    module; when omitted it is imported lazily from ``google.genai``,
    so importing this module never requires the SDK.
    """

    def __init__(
        self,
        genai_client: Any,
        model: str | None = None,
        types_module: Any = None,
    ) -> None:
        self._genai_client = genai_client
        self._model = (model or "").strip() or _resolve_gemini_model()
        self._types_module = types_module
        # _call_model talks to ``client.messages.create(...)``.
        self.messages = self

    def _types(self) -> Any:
        if self._types_module is None:
            from google.genai import types

            self._types_module = types
        return self._types_module

    def _translate_parts(
        self, types: Any, messages: list[dict[str, Any]]
    ) -> list[Any]:
        parts: list[Any] = []
        for message in messages:
            content = message.get("content", "")
            blocks = [content] if isinstance(content, str) else list(content or [])
            for block in blocks:
                if isinstance(block, str):
                    if block:
                        parts.append(types.Part.from_text(text=block))
                elif isinstance(block, dict):
                    parts.append(self._translate_block(types, block))
                else:
                    raise ValueError(
                        f"unsupported message block for Gemini: {block!r}"
                    )
        if not parts:
            raise ValueError("no message content to send to Gemini")
        return parts

    @staticmethod
    def _translate_block(types: Any, block: dict[str, Any]) -> Any:
        block_type = block.get("type")
        if block_type == "text":
            return types.Part.from_text(text=block.get("text", ""))
        if block_type == "image":
            source = block.get("source") or {}
            if source.get("type") != "base64":
                raise ValueError(
                    "Gemini image blocks need a base64 source, "
                    f"got {source.get('type')!r}"
                )
            data = source.get("data", "")
            media_type = source.get("media_type") or _detect_media_type(
                base64.b64decode(data)
            )
            if media_type == "image/gif":
                raise ValueError(
                    "Gemini does not accept image/gif inline data "
                    "(supported: PNG, JPEG, WEBP, HEIC, HEIF); "
                    "convert the schematic to PNG first"
                )
            return types.Part.from_bytes(
                data=base64.b64decode(data),
                mime_type=media_type,
            )
        raise ValueError(
            f"unsupported message block type for Gemini: {block_type!r}"
        )

    def create(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Anthropic-shaped ``messages.create`` backed by Gemini.

        ``model`` is ignored (see the class docstring): the adapter's
        own model is always used.
        """
        types = self._types()
        parts = self._translate_parts(types, messages or [])
        contents = [types.Content(role="user", parts=parts)]
        config = types.GenerateContentConfig(
            max_output_tokens=max_tokens if max_tokens is not None else MAX_TOKENS,
            response_mime_type="application/json",
        )
        response = self._genai_client.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )
        return _AdapterResponse(_gemini_response_text(response))


def _get_gemini_client() -> Any:
    """Build a Gemini adapter around a real ``google.genai`` client.

    ``genai.Client()`` is constructed with no arguments: the SDK reads
    the key from the environment itself (``GEMINI_API_KEY``, falling
    back to ``GOOGLE_API_KEY`` — in google-genai 3.3.0 the SDK prefers
    ``GOOGLE_API_KEY`` when both are set, so set only one key; this
    module never reads, prints or logs the key value). The model comes
    from ``GEMINI_MODEL`` env, default ``"gemini-4-argon"``.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ImportError(
            "LLM_PROVIDER=gemini needs the google-genai SDK, which is "
            "not installed (install the optional dependency: "
            "`uv sync --extra gemini`)"
        ) from exc
    return _GeminiClientAdapter(genai.Client(), types_module=types)


def _get_client(client: Any = None, provider: str | None = None) -> Any:
    """Return the given client, or build a real client for the provider.

    An injected client always wins and bypasses provider selection
    entirely — even an unknown ``LLM_PROVIDER`` value cannot break a
    call that injects a client. One exception: a bare ``google.genai``
    client passed as ``client=`` has ``.models`` and no ``.messages``,
    so it is wrapped in a ``_GeminiClientAdapter`` to satisfy the
    Anthropic-shaped ``_call_model``.

    Otherwise the provider comes from ``get_provider(provider)``:
    ``anthropic`` builds ``anthropic.Anthropic()`` (reads
    ``ANTHROPIC_API_KEY`` itself); ``gemini`` builds a Gemini adapter
    around ``genai.Client()`` (reads ``GEMINI_API_KEY`` /
    ``GOOGLE_API_KEY`` itself). Both SDK imports are lazy, so
    importing this module needs neither SDK nor any key.
    """
    if client is not None:
        if hasattr(client, "models") and not hasattr(client, "messages"):
            return _GeminiClientAdapter(client)
        return client
    resolved = get_provider(provider)
    if resolved == "anthropic":
        import anthropic

        return anthropic.Anthropic()
    return _get_gemini_client()


def _response_text(response: Any) -> str:
    """Extract the text of a Messages API response (real or fake)."""
    if getattr(response, "stop_reason", None) == "refusal":
        raise ValueError("The AI declined this request (safety filter). Try rephrasing it.")
    content = getattr(response, "content", None)
    if isinstance(content, (list, tuple)) and content:
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                return text
        raise ValueError("LLM response contained no text block")
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(response, str):
        return response
    raise ValueError(f"cannot extract text from LLM response: {response!r}")


def _call_model(client: Any, messages: list[dict[str, Any]]) -> str:
    """One messages.create call; returns the reply text."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=messages,
    )
    return _response_text(response)


# ---------------------------------------------------------------------------
# JSON parsing / validation (one retry, then raise loudly)
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> Any:
    """Parse JSON out of a reply, tolerating markdown fences.

    Raises ValueError (never returns a guessed object) when the reply
    holds no parseable JSON object/array.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("LLM reply was empty")
    text = raw.strip()
    if text.startswith("```"):
        # Strip a ```json ... ``` or ``` ... ``` fence if present.
        lines = text.splitlines()
        lines = lines[1:]  # opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        # A bare top-level array is the only other accepted shape.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM reply contained no JSON object: {raw[:500]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM reply was not valid JSON: {exc}: {raw[:500]!r}") from exc


def _parse_netlist(obj: Any) -> Netlist:
    """Validate a parsed JSON value as a Netlist (Pydantic)."""
    if isinstance(obj, list):
        obj = {"components": obj}
    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object for a netlist, got {type(obj).__name__}")
    try:
        return Netlist.model_validate(obj)
    except ValidationError as exc:
        raise ValueError(f"LLM netlist failed validation: {exc}") from exc


def _parse_schematic_reply(obj: Any) -> SchematicNetlist:
    """Validate a schematic-read reply: a netlist plus the parts on
    the schematic the circuit model cannot simulate.

    ``"unmodeled"`` is optional (absent means every part was modeled).
    Each entry needs a non-empty ``id`` and ``type``; ``note`` defaults
    to ``""``. Entries whose id collides with a modeled component, or
    that repeat an earlier unmodeled id, are dropped deterministically
    (first occurrence wins) — the model contradicting itself about a
    part never surfaces the same id twice.
    """
    if not isinstance(obj, dict):
        raise ValueError(
            "expected a JSON object for a schematic read, "
            f"got {type(obj).__name__}"
        )
    netlist = _parse_netlist(obj)
    raw_unmodeled = obj.get("unmodeled", [])
    if raw_unmodeled is None:
        raw_unmodeled = []
    if not isinstance(raw_unmodeled, list):
        raise ValueError("schematic 'unmodeled' must be an array")
    modeled_ids = {c.id for c in netlist.components}
    seen: set[str] = set()
    parts: list[UnmodeledPart] = []
    for entry in raw_unmodeled:
        if not isinstance(entry, dict):
            raise ValueError(
                "schematic 'unmodeled' entries must be objects, "
                f"got {entry!r}"
            )
        try:
            part = UnmodeledPart.model_validate(entry)
        except ValidationError as exc:
            raise ValueError(
                f"schematic 'unmodeled' entry failed validation: {exc}"
            ) from exc
        if part.id in modeled_ids or part.id in seen:
            continue
        seen.add(part.id)
        parts.append(part)
    return SchematicNetlist(
        components=list(netlist.components),
        title=netlist.title,
        unmodeled=parts,
    )


def _request_schematic(
    client: Any, messages: list[dict[str, Any]]
) -> SchematicNetlist:
    """Call the model, validate the reply as a schematic read (netlist
    + unmodeled parts), one retry max. On the second failure a
    ``ValueError`` is raised — never a fallback netlist, and never a
    silently dropped part list.
    """
    first_error: str | None = None
    try:
        return _parse_schematic_reply(_extract_json(_call_model(client, messages)))
    except ValueError as exc:
        first_error = str(exc)
    retry_messages = messages + [
        {"role": "user", "content": _RETRY_TEXT.format(error=first_error)},
    ]
    try:
        return _parse_schematic_reply(
            _extract_json(_call_model(client, retry_messages))
        )
    except ValueError as exc:
        raise ValueError(
            "LLM failed to return a valid schematic read after one retry. "
            f"First error: {first_error}. Retry error: {exc}"
        ) from exc


def _request_netlist(client: Any, messages: list[dict[str, Any]]) -> Netlist:
    """Call the model, validate the reply as a Netlist, one retry max.

    On the first invalid reply (bad JSON or failed Pydantic validation)
    the error is fed back and the model gets one more attempt. If that
    also fails, ValueError is raised — never a fallback netlist.
    """
    first_error: str | None = None
    try:
        return _parse_netlist(_extract_json(_call_model(client, messages)))
    except ValueError as exc:
        first_error = str(exc)
    retry_messages = messages + [
        {"role": "user", "content": _RETRY_TEXT.format(error=first_error)},
    ]
    try:
        return _parse_netlist(_extract_json(_call_model(client, retry_messages)))
    except ValueError as exc:
        raise ValueError(
            "LLM failed to return a valid netlist after one retry. "
            f"First error: {first_error}. Retry error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# read_schematic
# ---------------------------------------------------------------------------

def _detect_media_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError(
        "unsupported image format: expected PNG, JPEG, GIF or WEBP bytes"
    )


def read_schematic(
    image_bytes: bytes, client: Any = None, provider: str | None = None
) -> SchematicNetlist:
    """Transcribe a schematic image into a Netlist.

    Returns a :class:`SchematicNetlist` — a plain Netlist plus an
    ``unmodeled`` list of ``{id, type, note}`` dicts for every part
    on the schematic the circuit model cannot simulate (ICs,
    transistors, crystals, connectors, ...), so such parts are listed
    instead of silently dropped. ``unmodeled`` is empty when every
    part was modeled. Invalid JSON (or an invalid unmodeled entry)
    gets one retry, then a loud ValueError — never a silent fallback.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("read_schematic requires non-empty image bytes")
    image_bytes = bytes(image_bytes)
    media_type = _detect_media_type(image_bytes)
    client = _get_client(client, provider)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Transcribe this schematic diagram into a netlist. "
                        "Read every component, its value, and the nodes it "
                        "connects, including ground node \"0\". If a value "
                        "is unreadable, use the most plausible standard "
                        "value and note it only by choosing that value — "
                        "do not add prose. "
                        + _SCHEMA_TEXT
                        + "\n\n"
                        + _UNMODELED_TEXT
                    ),
                },
            ],
        }
    ]
    return _request_schematic(client, messages)


# ---------------------------------------------------------------------------
# design
# ---------------------------------------------------------------------------


def design(goal: str, client: Any = None, provider: str | None = None) -> Netlist:
    """Design a circuit for a plain-English goal, returning a Netlist."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("design requires a non-empty goal string")
    client = _get_client(client, provider)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Design a circuit that achieves this goal:\n\n"
                f"{goal.strip()}\n\n"
                "Choose standard, real component values. "
                "Keep the circuit as simple as will meet the goal. "
                + _SCHEMA_TEXT
            ),
        }
    ]
    return _request_netlist(client, messages)


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def _finding_text(finding: Any) -> str:
    """Render one finding (Finding, dict, or plain string) as text."""
    if isinstance(finding, Finding):
        return (
            f"[{finding.severity.value}] {finding.rule_id} "
            f"(components: {', '.join(finding.component_ids) or 'none'}): "
            f"{finding.message}"
        )
    if isinstance(finding, dict):
        try:
            return _finding_text(Finding.model_validate(finding))
        except ValidationError:
            return str(finding.get("message", finding))
    return str(finding)


def _changed_component_ids(before: Netlist, after: Netlist) -> list[str]:
    """Ids of components added, removed, or field-changed by a repair."""
    before_map = {c.id: c for c in before.components}
    after_map = {c.id: c for c in after.components}
    changed: list[str] = []
    for comp_id in sorted(set(before_map) | set(after_map)):
        if comp_id not in before_map or comp_id not in after_map:
            changed.append(comp_id)
        elif before_map[comp_id].model_dump() != after_map[comp_id].model_dump():
            changed.append(comp_id)
    return changed


def repair(
    netlist: Netlist | dict[str, Any],
    findings: list[Finding | dict[str, Any] | str],
    client: Any = None,
    provider: str | None = None,
) -> tuple[Netlist, list[str]]:
    """Repair a netlist against findings.

    Returns ``(repaired_netlist, reasons)`` with exactly one reason per
    changed component (added, removed, or field-changed). A reply whose
    reason count does not match the number of changed components, or
    whose netlist fails validation, gets one retry and then raises
    ValueError loudly. An unchanged netlist returns an empty reason list.
    """
    if isinstance(netlist, dict):
        try:
            netlist = Netlist.model_validate(netlist)
        except ValidationError as exc:
            raise ValueError(f"repair input netlist failed validation: {exc}") from exc
    if not isinstance(netlist, Netlist):
        raise ValueError("repair requires a Netlist (or a netlist dict)")
    if not isinstance(findings, (list, tuple)) or not findings:
        raise ValueError("repair requires a non-empty list of findings")
    findings_text = "\n".join(f"- {_finding_text(f)}" for f in findings)
    client = _get_client(client, provider)

    prompt = (
        "Repair this netlist so every finding below is resolved. Change "
        "only what the findings require; keep every other component, "
        "id, value, and node exactly as it is.\n\n"
        f"Current netlist:\n{netlist.model_dump_json(indent=2)}\n\n"
        f"Findings:\n{findings_text}\n\n"
        "Return ONLY strict JSON with this shape, no prose, no markdown, "
        "no code fences:\n"
        "{\n"
        '  "netlist": { ... the full repaired netlist, same schema as the '
        "input: components with id, type, value, nodes, rating_watts, "
        'color, plus optional title ... },\n'
        '  "reasons": ["one plain-English reason per changed component, '
        "in the same order as the changed components, each naming the "
        "component id and what changed and why. If nothing needed "
        "changing, an empty list.\"]\n"
        "}\n"
        "The number of reasons must equal the number of components you "
        "added, removed, or changed."
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    first_error: str | None = None
    for attempt in range(2):
        try:
            raw = _call_model(client, messages)
            obj = _extract_json(raw)
            if not isinstance(obj, dict) or "netlist" not in obj:
                raise ValueError(
                    "repair reply must be a JSON object with a 'netlist' key"
                )
            repaired = _parse_netlist(obj["netlist"])
            reasons_raw = obj.get("reasons", obj.get("changes"))
            if isinstance(reasons_raw, list) and reasons_raw and isinstance(
                reasons_raw[0], dict
            ):
                # Accept [{"id": ..., "reason": ...}] as well.
                reasons = [str(c.get("reason", "")) for c in reasons_raw]
            elif isinstance(reasons_raw, list):
                reasons = [str(r) for r in reasons_raw]
            else:
                raise ValueError(
                    "repair reply must include a 'reasons' list of strings"
                )
            if any(not r.strip() for r in reasons):
                raise ValueError("repair reasons must be non-empty strings")
            reasons = [r.strip() for r in reasons]
            changed = _changed_component_ids(netlist, repaired)
            if len(reasons) != len(changed):
                raise ValueError(
                    f"repair gave {len(reasons)} reason(s) for "
                    f"{len(changed)} changed component(s) "
                    f"({', '.join(changed) or 'none'}); "
                    "one reason per change is required"
                )
            return repaired, reasons
        except ValueError as exc:
            first_error = str(exc)
            if attempt == 0:
                messages = messages + [
                    {
                        "role": "user",
                        "content": _RETRY_TEXT.format(error=first_error),
                    },
                ]
    raise ValueError(
        "LLM failed to return a valid repair after one retry. "
        f"Last error: {first_error}"
    )
