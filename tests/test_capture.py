import pytest
from fastapi.testclient import TestClient

from ravens_nest import config, db, ingest, store, vision
from ravens_nest.app import app

JPEG = b"\xff\xd8\xff\xe0" + b"fake image bytes"


@pytest.fixture
def mock_vision(monkeypatch):
    """Replace the API call with a canned extraction; returns the call log."""
    calls = []

    def fake(image_bytes):
        calls.append(image_bytes)
        fields = {f: {"value": None, "confidence": "low"} for f in vision.FIELDS}
        fields["name"] = {"value": "M3 screw", "confidence": "high"}
        fields["unit_type"] = {"value": "each", "confidence": "high"}
        fields["qty_visible"] = {"value": "25", "confidence": "medium"}
        return {
            "fields": fields,
            "questions": [{"field": "part_number", "question": "What length?"}],
            "error": None,
        }

    monkeypatch.setattr(vision, "extract_fields", fake)
    return calls


def test_photos_dedup_by_hash(data_dir, mock_vision):
    first = ingest.ingest_photo(JPEG)
    second = ingest.ingest_photo(JPEG)
    assert first["status"] == "new"
    assert second["status"] == "duplicate_pending"
    assert first["photo_hash"] == second["photo_hash"]
    assert len(mock_vision) == 1  # duplicate did not trigger a second API call
    assets = list(config.assets_dir().glob("*.jpg"))
    assert [a.stem for a in assets] == [first["photo_hash"]]
    assert len(ingest.list_cards()) == 1


def test_capture_endpoint_then_confirm_creates_item(data_dir, mock_vision):
    client = TestClient(app)
    response = client.post("/capture", files={"photo": ("item.jpg", JPEG, "image/jpeg")})
    assert response.status_code == 200
    photo_hash = response.json()["photo_hash"]
    assert response.json()["status"] == "new"

    queue_html = client.get("/queue").text
    assert "M3 screw" in queue_html
    assert "What length?" in queue_html  # question rendered inline on the card
    assert "conf-high" in queue_html and "conf-low" in queue_html

    response = client.post(
        f"/queue/{photo_hash}/confirm",
        data={
            "name": "M3 screw",
            "unit_type": "each",
            "qty": "25",
            "description": "Hex socket cap screw",
            "part_number": "M3x12",
            "manufacturer": "",
            "package_type": "",
            "location_id": "",
        },
    )
    assert response.status_code == 200

    conn = db.connect()
    row = conn.execute(
        "SELECT name, qty_on_hand, part_number, photo_hash FROM items"
    ).fetchone()
    conn.close()
    assert (row["name"], row["qty_on_hand"], row["part_number"]) == ("M3 screw", "25", "M3x12")
    assert row["photo_hash"] == photo_hash
    assert ingest.load_card(photo_hash) is None  # card cleared from the queue


def test_confirm_with_invalid_location_rerenders_card(data_dir, mock_vision):
    result = ingest.ingest_photo(JPEG)
    client = TestClient(app)
    response = client.post(
        f"/queue/{result['photo_hash']}/confirm",
        data={"name": "M3 screw", "unit_type": "each", "qty": "1", "location_id": "nope"},
    )
    assert response.status_code == 200
    assert "invalid location ID" in response.text
    assert ingest.load_card(result["photo_hash"]) is not None  # card kept, nothing created
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    conn.close()


def test_capture_rejects_non_jpeg(data_dir):
    client = TestClient(app)
    response = client.post("/capture", files={"photo": ("x.jpg", b"PNG...", "image/jpeg")})
    assert response.status_code == 400


def test_merge_adds_qty_and_attaches_photo(data_dir, mock_vision):
    item = store.create_item("M3 screw", "each", qty_on_hand=10)["payload"]["id"]
    result = ingest.ingest_photo(JPEG)

    client = TestClient(app)
    response = client.post(
        f"/queue/{result['photo_hash']}/merge",
        data={"item_id": item, "qty": "5"},
    )
    assert response.status_code == 200

    conn = db.connect()
    row = conn.execute(
        "SELECT qty_on_hand, photo_hash FROM items WHERE id = ?", (item,)
    ).fetchone()
    conn.close()
    assert row["qty_on_hand"] == "15"
    assert row["photo_hash"] == result["photo_hash"]
    assert ingest.load_card(result["photo_hash"]) is None


def test_already_cataloged_photo_is_not_requeued(data_dir, mock_vision):
    result = ingest.ingest_photo(JPEG)
    store.create_item("M3 screw", "each", photo_hash=result["photo_hash"])
    ingest.delete_card(result["photo_hash"])

    again = ingest.ingest_photo(JPEG)
    assert again["status"] == "already_cataloged"
    assert ingest.load_card(result["photo_hash"]) is None
    assert len(mock_vision) == 1


def test_inbox_scan_ingests_and_consumes_files(data_dir, mock_vision):
    inbox = config.inbox_dir()
    inbox.mkdir(parents=True)
    (inbox / "a.jpg").write_bytes(JPEG)
    (inbox / "b.jpg").write_bytes(JPEG + b" different photo")
    (inbox / "junk.jpg").write_bytes(b"not a jpeg at all")

    result = ingest.scan_inbox()
    assert result["ingested"] == 2
    assert result["duplicates"] == 0
    assert result["errors"] == ["junk.jpg: not a JPEG"]
    assert not (inbox / "a.jpg").exists()  # consumed
    assert (inbox / "junk.jpg").exists()  # left for inspection
    assert len(ingest.list_cards()) == 2

    # The same photo landing in the inbox again (e.g. folder re-sync) is
    # consumed as a duplicate without a new card or API call.
    (inbox / "a.jpg").write_bytes(JPEG)
    result = ingest.scan_inbox()
    assert result["duplicates"] == 1
    assert len(ingest.list_cards()) == 2
    assert len(mock_vision) == 2

    # POST /inbox/scan drives the same path on demand.
    (inbox / "c.jpg").write_bytes(JPEG + b" third photo")
    response = TestClient(app).post("/inbox/scan")
    assert response.status_code == 200
    assert response.json()["ingested"] == 1


def test_asset_endpoint_serves_photo(data_dir, mock_vision):
    result = ingest.ingest_photo(JPEG)
    client = TestClient(app)
    response = client.get(f"/assets/{result['photo_hash']}.jpg")
    assert response.status_code == 200
    assert response.content == JPEG
    assert client.get("/assets/" + "0" * 64 + ".jpg").status_code == 404
    assert client.get("/assets/not-a-hash.jpg").status_code == 404


def test_blank_card_flow_still_reviewable(data_dir, monkeypatch):
    # API completely unavailable: photo still queues with a blank card.
    monkeypatch.setattr(
        vision, "_call_model", lambda messages: (_ for _ in ()).throw(RuntimeError("no network"))
    )
    result = ingest.ingest_photo(JPEG)
    assert result["status"] == "new"
    assert result["card"]["error"] is not None
    page = TestClient(app).get("/queue").text
    assert "Automatic identification failed" in page
