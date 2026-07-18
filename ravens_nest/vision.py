"""Photo identification via the Anthropic Messages API.

extract_fields() sends a JPEG to the vision model and returns a normalized
extraction: every field as {value, confidence}, plus targeted questions for
anything the model could not determine. The model is instructed never to
guess — unknowns come back as null with a question, and the normalizer
never substitutes defaults for nulls.

Failure ladder: API errors → blank card; malformed JSON → one retry with a
stricter reminder → blank card. The queue keeps working either way.
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import anthropic

from . import config

log = logging.getLogger(__name__)

FIELDS = (
    "name",
    "description",
    "part_number",
    "unit_type",
    "qty_visible",
    "manufacturer",
    "package_type",
)
CONFIDENCES = frozenset({"high", "medium", "low"})
UNIT_TYPES = frozenset({"each", "g", "mm", "mL"})

_ITEM_SCHEMA = """\
    {
      "name": {"value": <string or null>, "confidence": "high"|"medium"|"low"},
      "description": {"value": <string or null>, "confidence": ...},
      "part_number": {"value": <string or null>, "confidence": ...},
      "unit_type": {"value": "each"|"g"|"mm"|"mL" or null, "confidence": ...},
      "qty_visible": {"value": <number or null>, "confidence": ...},
      "manufacturer": {"value": <string or null>, "confidence": ...},
      "package_type": {"value": <string or null>, "confidence": ...},
      "photo_region": <string or null>,
      "questions": [{"field": <field name>, "question": <string>}]
    }"""

_SCHEMA = f"""\
{{
  "items": [
{_ITEM_SCHEMA}
  ]
}}"""

SYSTEM_PROMPT = f"""\
You catalog workshop inventory from a photo. The photo may show ONE item (or a batch of \
identical items), or SEVERAL distinct items (e.g. a drawer with different parts).

Return ONLY a JSON object — no markdown fences, no commentary before or after — with exactly this shape:

{_SCHEMA}

Rules:
- One entry in "items" per DISTINCT item you can confidently separate. A batch of identical \
parts is ONE entry (count them in qty_visible).
- NEVER split speculatively. If you cannot confidently tell whether things are distinct items, \
return ONE entry covering the photo and add a question saying the photo may contain multiple \
items and asking the user to confirm.
- NEVER guess field values. If a field cannot be determined from the image, set its value to \
null and add ONE targeted question for that field to that item's "questions". An educated \
guess is still a guess — use null.
- Questions must be specific and answerable in a few words, e.g. \
"Hex socket cap screw, M3, length unknown — what length?" One question per unknown field; \
no questions for fields you filled in.
- photo_region: a few words locating this item in the photo ("top-left", "the blue tray, \
centre"), so the user can tell which detection is which. null for a single-item photo.
- confidence: "high" = clearly visible or legible, "medium" = probable from strong visual \
evidence, "low" = weak evidence.
- unit_type is how the item is counted or measured: "each" for discrete items, "g" for weight, \
"mm" for length (wire, rod, filament), "mL" for volume. null if unclear.
- qty_visible is the quantity countable in the photo or readable from packaging, as a number. \
null if not determinable.
- description is a short physical description (what it is, size, material, color)."""

_STRICT_REMINDER = (
    "Your previous reply was not valid JSON. Respond again with ONLY the JSON object "
    "in the exact shape specified — no markdown fences, no explanation, nothing before "
    "the opening { or after the closing }."
)


def blank_extraction(error: str | None = None) -> dict[str, Any]:
    return {
        "fields": {f: {"value": None, "confidence": "low"} for f in FIELDS},
        "questions": [],
        "photo_region": None,
        "error": error,
    }


def _call_model(messages: list[dict[str, Any]]) -> str:
    """One Messages API call; returns the response text. Patched in tests."""
    config.load_env_file()
    # Audit H3: without a timeout the SDK default is 10 minutes — a slow
    # API would freeze the capture request. Extraction output is small;
    # 30s is generous, and failure degrades to a blank card anyway.
    client = anthropic.Anthropic(timeout=30.0)  # retries 429/5xx with backoff
    response = client.messages.create(
        model=config.vision_model(),
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request (stop_reason=refusal)")
    return next((b.text for b in response.content if b.type == "text"), "")


def extract_items(image_bytes: bytes) -> list[dict[str, Any]]:
    """Identify the item(s) in a JPEG — one extraction per distinct item
    the model can confidently separate. A single-item photo yields an
    array of one. Never raises; failures yield one blank extraction."""
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": "Identify the inventory item(s) in this photo."},
            ],
        }
    ]
    try:
        text = _call_model(messages)
    except Exception as exc:
        log.warning("vision call failed: %s", exc)
        return [blank_extraction(f"vision call failed: {type(exc).__name__}: {exc}")]

    parsed = _parse_json(text)
    if parsed is None:
        retry_messages = messages + [
            {"role": "assistant", "content": text or "(no output)"},
            {"role": "user", "content": _STRICT_REMINDER},
        ]
        try:
            text = _call_model(retry_messages)
        except Exception as exc:
            log.warning("vision retry failed: %s", exc)
            return [blank_extraction(f"vision retry failed: {type(exc).__name__}: {exc}")]
        parsed = _parse_json(text)
        if parsed is None:
            return [blank_extraction("model returned malformed JSON twice")]

    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else None
    if raw_items is None:
        raw_items = [parsed]  # tolerate the legacy single-object shape
    extractions = [_normalize(raw) for raw in raw_items if isinstance(raw, dict)]
    return extractions or [blank_extraction("model returned an empty item list")]


def extract_fields(image_bytes: bytes) -> dict[str, Any]:
    """Single-item convenience wrapper (first detection)."""
    return extract_items(image_bytes)[0]


def _parse_json(text: str) -> dict[str, Any] | None:
    """Parse the model's reply. Tolerates markdown fences and surrounding
    prose; returns None when no JSON object can be recovered."""
    candidates = [text.strip()]
    stripped = text.strip()
    if stripped.startswith("```"):
        inner = stripped.split("\n", 1)[-1]
        candidates.append(inner.rsplit("```", 1)[0].strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's output into the canonical shape. Values are
    validated but never invented — a null stays a null."""
    fields: dict[str, Any] = {}
    for name in FIELDS:
        entry = raw.get(name)
        if not isinstance(entry, dict):
            entry = {"value": entry, "confidence": "low"}
        value = entry.get("value")
        confidence = entry.get("confidence")
        if confidence not in CONFIDENCES:
            confidence = "low"
        if name == "unit_type" and value is not None and value not in UNIT_TYPES:
            value = None
            confidence = "low"
        if name == "qty_visible" and value is not None:
            try:
                value = str(Decimal(str(value)))
            except InvalidOperation:
                value = None
                confidence = "low"
        if value is not None and not isinstance(value, str):
            value = str(value)
        if isinstance(value, str) and not value.strip():
            value = None
        fields[name] = {"value": value, "confidence": confidence}

    questions: list[dict[str, Any]] = []
    for q in raw.get("questions") or []:
        if isinstance(q, str) and q.strip():
            questions.append({"field": None, "question": q.strip()})
        elif isinstance(q, dict) and str(q.get("question", "")).strip():
            field = q.get("field")
            questions.append(
                {
                    "field": field if field in FIELDS else None,
                    "question": str(q["question"]).strip(),
                }
            )

    region = raw.get("photo_region")
    if not isinstance(region, str) or not region.strip():
        region = None
    return {
        "fields": fields,
        "questions": questions,
        "photo_region": region.strip() if region else None,
        "error": None,
    }
