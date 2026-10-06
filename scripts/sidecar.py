"""sidecar.db: item-level grocery data that is too fine-grained for the ledger.

A receipt's total lives in Beancount (tagged with receipt_id + link ^receipt-<id>);
its line items live here, joined on the same receipt_id.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from common import sidecar_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id    TEXT PRIMARY KEY,
    store         TEXT NOT NULL,
    date          TEXT NOT NULL,            -- YYYY-MM-DD
    bill_no       TEXT,
    total         REAL NOT NULL,            -- printed bill total
    items_total   REAL NOT NULL,            -- sum of parsed lines (differs => needs_review)
    paid_with     TEXT,                     -- ledger account that paid
    entity        TEXT,
    image_path    TEXT,
    image_sha256  TEXT UNIQUE,              -- the same photo is never ingested twice
    needs_review  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS receipt_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id      TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    canonical_name  TEXT NOT NULL,
    brand           TEXT,
    quantity        REAL,                   -- in `unit` (kg / l / pcs)
    unit            TEXT,
    unit_price      REAL,                   -- price per pack/line unit as printed
    total_price     REAL NOT NULL,
    price_per_unit  REAL,                   -- total_price / quantity: comparable across brands/packs
    category        TEXT,
    raw_name        TEXT,
    store           TEXT,
    date            TEXT NOT NULL,
    needs_review    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_items_name_date ON receipt_items(canonical_name, date);
CREATE INDEX IF NOT EXISTS ix_items_receipt ON receipt_items(receipt_id);

-- Mappings taught with `receipt_engine.py map`; consulted before the regex rules.
CREATE TABLE IF NOT EXISTS canonical_map (
    raw_key         TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    category        TEXT,
    unit            TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or sidecar_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)  # wait out a reader (Fava, analytics)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
