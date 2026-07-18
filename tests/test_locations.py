import pytest

from ravens_nest.locations import InvalidLocationId, LocationId, is_valid_location_id, parse_location_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A-2-3b", LocationId("A", 2, 3, "b")),
        ("A-2-3", LocationId("A", 2, 3, None)),
        ("Z-10-12", LocationId("Z", 10, 12, None)),
        ("B-1-1a", LocationId("B", 1, 1, "a")),
        ("C-3-15f", LocationId("C", 3, 15, "f")),
    ],
)
def test_parse_valid(raw, expected):
    assert parse_location_id(raw) == expected
    assert str(parse_location_id(raw)) == raw
    assert is_valid_location_id(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "a-2-3",  # lowercase unit
        "AA-2-3",  # multi-letter unit
        "A-0-3",  # shelf numbering starts at 1
        "A-2-0",  # bin numbering starts at 1
        "A-02-3",  # leading zero
        "A-2-3B",  # uppercase section
        "A-2-3bb",  # multi-letter section
        "A-2",  # missing bin
        "A2-3",  # missing separator
        "A-2-3-b",  # section must not be dash-separated
        "A--2-3",
        " A-2-3",
        "A-2-3 ",
        "1-2-3",  # unit must be a letter
    ],
)
def test_parse_invalid(raw):
    with pytest.raises(InvalidLocationId):
        parse_location_id(raw)
    assert not is_valid_location_id(raw)


def test_parse_rejects_non_string():
    with pytest.raises(InvalidLocationId):
        parse_location_id(None)
