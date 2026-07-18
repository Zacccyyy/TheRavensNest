"""SQLite cache schema and connection helpers.

Quantities and prices are stored as canonical decimal strings and
handled with decimal.Decimal in Python — never floats — so arithmetic
is exact for fractional units (g, mm, mL) as well as counts.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    part_number   TEXT,
    unit_type     TEXT NOT NULL CHECK (unit_type IN ('each', 'g', 'mm', 'mL')),
    qty_on_hand   TEXT NOT NULL DEFAULT '0',
    min_qty       TEXT,
    location_id   TEXT,
    last_paid_aud TEXT,
    photo_hash    TEXT,
    created_ts    TEXT NOT NULL,
    updated_ts    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id          TEXT PRIMARY KEY,
    unit        TEXT NOT NULL,
    shelf       INTEGER NOT NULL,
    bin         INTEGER NOT NULL,
    section     TEXT,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS aliases (
    alias_text TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    PRIMARY KEY (alias_text, item_id)
);

CREATE TABLE IF NOT EXISTS events_applied (
    event_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_ts  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bom_lines (
    project_id            TEXT NOT NULL,
    line_no               INTEGER NOT NULL,
    part_number           TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    quantity              TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    reference_designators TEXT,
    notes                 TEXT,
    item_id               TEXT,
    match_method          TEXT,
    match_score           REAL,
    PRIMARY KEY (project_id, line_no)
);

CREATE TABLE IF NOT EXISTS reservations (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    qty         TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
    created_ts  TEXT NOT NULL,
    released_ts TEXT
);

CREATE TABLE IF NOT EXISTS builds (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('build', 'reversal')),
    count      INTEGER NOT NULL,
    ts         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id                          TEXT PRIMARY KEY,
    name                        TEXT NOT NULL,
    reliability                 INTEGER CHECK (reliability BETWEEN 1 AND 5),
    free_shipping_threshold_aud TEXT,
    typical_shipping_aud        TEXT,
    typical_lead_days           INTEGER
);

CREATE TABLE IF NOT EXISTS item_links (
    item_id         TEXT NOT NULL,
    supplier_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    sku             TEXT,
    pack_qty        TEXT NOT NULL DEFAULT '1',
    last_price_aud  TEXT,
    last_checked_ts TEXT,
    PRIMARY KEY (item_id, supplier_id)
);

CREATE TABLE IF NOT EXISTS basket_items (
    item_id  TEXT PRIMARY KEY,
    qty      TEXT NOT NULL,
    added_ts TEXT NOT NULL
);
"""


def connect(path=None) -> sqlite3.Connection:
    """Open the cache database, creating the schema if needed."""
    if path is None:
        path = config.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def qty_str(value: Decimal) -> str:
    """Canonical string form of a quantity: no exponent, no trailing zeros."""
    s = format(value.normalize(), "f")
    return "0" if s == "-0" else s


def parse_qty(raw) -> Decimal:
    """Parse a quantity from an event payload or DB cell, rejecting floats
    so binary rounding error can never leak into the exact arithmetic."""
    if isinstance(raw, float):
        raise TypeError(f"quantity must be a string or int, not float: {raw!r}")
    return Decimal(str(raw))
