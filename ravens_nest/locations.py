"""Location IDs: "A-2-3b" = Unit A, shelf 2 (from bottom), bin 3 (from
left), section b (back). Section is optional."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Shelf/bin are positive integers without leading zeros so each location
# has exactly one spelling ("A-2-3", never "A-02-3").
_LOCATION_RE = re.compile(r"^(?P<unit>[A-Z])-(?P<shelf>[1-9]\d*)-(?P<bin>[1-9]\d*)(?P<section>[a-z])?$")


class InvalidLocationId(ValueError):
    pass


@dataclass(frozen=True)
class LocationId:
    unit: str
    shelf: int
    bin: int
    section: str | None = None

    def __str__(self) -> str:
        return f"{self.unit}-{self.shelf}-{self.bin}{self.section or ''}"


def parse_location_id(raw: str) -> LocationId:
    """Parse and validate a location ID, raising InvalidLocationId on failure."""
    if not isinstance(raw, str):
        raise InvalidLocationId(f"location ID must be a string, got {type(raw).__name__}")
    m = _LOCATION_RE.match(raw)
    if m is None:
        raise InvalidLocationId(
            f"invalid location ID {raw!r}: expected UNIT-SHELF-BIN[section], e.g. 'A-2-3b'"
        )
    return LocationId(
        unit=m.group("unit"),
        shelf=int(m.group("shelf")),
        bin=int(m.group("bin")),
        section=m.group("section"),
    )


def is_valid_location_id(raw: str) -> bool:
    try:
        parse_location_id(raw)
    except InvalidLocationId:
        return False
    return True
