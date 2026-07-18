import json

from ravens_nest import vision

JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg body"

GOOD = {
    "name": {"value": "M3 hex bolt", "confidence": "high"},
    "description": {"value": "Hex socket cap screw, M3, steel", "confidence": "medium"},
    "part_number": {"value": None, "confidence": "low"},
    "unit_type": {"value": "each", "confidence": "high"},
    "qty_visible": {"value": 25, "confidence": "medium"},
    "manufacturer": {"value": None, "confidence": "low"},
    "package_type": {"value": "loose", "confidence": "medium"},
    "questions": [
        {"field": "part_number", "question": "Hex socket cap screw, M3, length unknown — what length?"},
        {"field": "manufacturer", "question": "No branding visible — who makes these?"},
    ],
}


def _reply_with(payload):
    return lambda messages: json.dumps(payload)


def test_unreadable_fields_are_null_with_targeted_questions(monkeypatch):
    monkeypatch.setattr(vision, "_call_model", _reply_with(GOOD))
    result = vision.extract_fields(JPEG)
    assert result["error"] is None
    assert result["fields"]["part_number"]["value"] is None
    assert result["fields"]["manufacturer"]["value"] is None
    question_fields = {q["field"] for q in result["questions"]}
    assert question_fields == {"part_number", "manufacturer"}
    # Questions are specific, not generic.
    assert "length" in result["questions"][0]["question"]


def test_no_invention_nulls_stay_null(monkeypatch):
    all_null = {f: {"value": None, "confidence": "low"} for f in vision.FIELDS}
    all_null["questions"] = []
    monkeypatch.setattr(vision, "_call_model", _reply_with(all_null))
    result = vision.extract_fields(JPEG)
    for field in vision.FIELDS:
        assert result["fields"][field]["value"] is None, field
    assert result["questions"] == []  # the pipeline never invents questions either


def test_extracted_values_survive_normalization(monkeypatch):
    monkeypatch.setattr(vision, "_call_model", _reply_with(GOOD))
    result = vision.extract_fields(JPEG)
    assert result["fields"]["name"] == {"value": "M3 hex bolt", "confidence": "high"}
    assert result["fields"]["qty_visible"]["value"] == "25"  # canonical decimal string
    assert result["fields"]["unit_type"]["value"] == "each"


def test_invalid_unit_type_becomes_null(monkeypatch):
    payload = json.loads(json.dumps(GOOD))
    payload["unit_type"] = {"value": "boxes", "confidence": "high"}
    monkeypatch.setattr(vision, "_call_model", _reply_with(payload))
    result = vision.extract_fields(JPEG)
    assert result["fields"]["unit_type"] == {"value": None, "confidence": "low"}


def test_fenced_json_is_tolerated_without_retry(monkeypatch):
    calls = []

    def fake(messages):
        calls.append(messages)
        return "```json\n" + json.dumps(GOOD) + "\n```"

    monkeypatch.setattr(vision, "_call_model", fake)
    result = vision.extract_fields(JPEG)
    assert len(calls) == 1
    assert result["fields"]["name"]["value"] == "M3 hex bolt"


def test_malformed_json_retries_once_with_stricter_reminder(monkeypatch):
    calls = []

    def fake(messages):
        calls.append(messages)
        if len(calls) == 1:
            return "Sure! The item appears to be a bolt."
        return json.dumps(GOOD)

    monkeypatch.setattr(vision, "_call_model", fake)
    result = vision.extract_fields(JPEG)
    assert len(calls) == 2
    retry_tail = calls[1][-1]
    assert retry_tail["role"] == "user"
    assert "JSON" in retry_tail["content"]
    assert result["error"] is None
    assert result["fields"]["name"]["value"] == "M3 hex bolt"


def test_malformed_json_twice_falls_back_to_blank_card(monkeypatch):
    calls = []

    def fake(messages):
        calls.append(messages)
        return "definitely not json"

    monkeypatch.setattr(vision, "_call_model", fake)
    result = vision.extract_fields(JPEG)
    assert len(calls) == 2  # exactly one retry
    assert result["error"] is not None
    for field in vision.FIELDS:
        assert result["fields"][field]["value"] is None


def test_api_error_falls_back_to_blank_card(monkeypatch):
    def fake(messages):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(vision, "_call_model", fake)
    result = vision.extract_fields(JPEG)
    assert "RuntimeError" in result["error"]
    for field in vision.FIELDS:
        assert result["fields"][field]["value"] is None
