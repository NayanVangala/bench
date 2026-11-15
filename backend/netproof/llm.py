"""LLM access for Netproof — the ONLY module that talks to an LLM.

Single provider: Anthropic official SDK, model ``claude-sonnet-5-5``.
No LangChain, no other provider, no hardcoded netlists in any non-mock
path. The API key comes from the environment (``ANTHROPIC_API_KEY``),
read by ``anthropic.Anthropic()`` itself; this module never reads the
key value.

Import safety
------------
``anthropic`` is imported lazily inside :func:`_get_client`, so importing
this module — and running tests with a fake client — works with no SDK
installed and no key set. Only a real call with ``client=None`` needs
the SDK and the key.

Injectable client (for tests)
-----------------------------
Every public function takes ``client=None``. Pass a fake whose
``client.messages.create(model=..., max_tokens=..., messages=[...])``
returns an object whose ``.content[0].text`` is the model reply
(a plain ``.text`` attribute or a plain string reply also work).
If ``client`` is ``None``, a real ``anthropic.Anthropic()`` is built.

Failure policy
--------------
Invalid JSON or a reply that fails Pydantic validation gets exactly
one retry (the error is fed back and valid JSON is demanded again).
If the retry also fails, a ``ValueError`` is raised loudly. There is
never a silent fallback, default, or guessed netlist.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from pydantic import ValidationError

from .ir import ComponentType, Finding, Netlist

__all__ = [
    "MODEL",
    "MAX_TOKENS",
    "read_schematic",
    "design",
    "repair",
]

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4096

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


# ---------------------------------------------------------------------------
# Client handling
# ---------------------------------------------------------------------------


def _get_client(client: Any) -> Any:
    """Return the given client, or build a real Anthropic client.

    The ``anthropic`` import lives here so importing this module never
    requires the SDK or a key. ``anthropic.Anthropic()`` reads
    ``ANTHROPIC_API_KEY`` from the environment.
    """
    if client is not None:
        return client
    import anthropic

    return anthropic.Anthropic()


def _response_text(response: Any) -> str:
    """Extract the text of a Messages API response (real or fake)."""
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


def read_schematic(image_bytes: bytes, client: Any = None) -> Netlist:
    """Transcribe a schematic image into a Netlist.

    Vision prompt asks for JSON netlist only. Invalid JSON gets one
    retry, then a loud ValueError — never a silent fallback.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("read_schematic requires non-empty image bytes")
    image_bytes = bytes(image_bytes)
    media_type = _detect_media_type(image_bytes)
    client = _get_client(client)
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
                    ),
                },
            ],
        }
    ]
    return _request_netlist(client, messages)


# ---------------------------------------------------------------------------
# design
# ---------------------------------------------------------------------------


def design(goal: str, client: Any = None) -> Netlist:
    """Design a circuit for a plain-English goal, returning a Netlist."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("design requires a non-empty goal string")
    client = _get_client(client)
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
    client = _get_client(client)

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
