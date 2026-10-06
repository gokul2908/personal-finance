"""Grocery receipts -> line items in sidecar.db + one total in the ledger.

    receipt_engine.py ingest [PHOTO|PDF|DIR ...] [--paid-with ACCOUNT] [--dry-run]
                      [--from-json FILE]      # skip the model: use an extraction you typed/fixed
    receipt_engine.py review                  # items/receipts waiting for a human look
    receipt_engine.py map RAW --to NAME --category CAT --unit kg|l|pcs

Flow per receipt:
  1. The photo's SHA-256 gives the receipt id (R + 8 hex) - the same photo is never
     ingested twice.
  2. Gemma (via Ollama, local or cloud) reads the bill into JSON. The reply is parsed
     tolerantly (code fences, stray prose, trailing commas) and validated; when it is
     malformed or the lines do not add up to the printed total, the model is told what
     was wrong and asked again (max_retries). A receipt that still does not add up is
     recorded but flagged "!" for review rather than dropped.
  3. Every line is normalised to a canonical item: "Lion Dates 500g" x1 ->
     Dates / Dry Fruits / brand Lion / 0.5 kg / price per kg. Learned mappings
     (`map`) win over the regex rules in config/canonical_items.toml.
  4. Items go into sidecar.db and the bill total is appended to the ledger, tagged
     receipt_id: "R..." and linked ^receipt-R.... If the ledger append fails, the
     database rows are removed again. If the card statement was imported first, the
     receipt attaches to that line (matched_import_id) instead of adding the money twice.

Beancount tags cannot contain ':', so "#receipt_id:<id>" is written as metadata
`receipt_id:` plus the link `^receipt-<id>` (Fava can filter on either).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import ledger_io
import sidecar
from common import (OllamaError, PFError, data_dir, entity_label, entity_ledger, entity_of_account,
                    fill_entity, image_b64, load_config, log, money, ollama_json, parse_date,
                    root, run_cli, setup_logging, to_decimal)
from ledger_io import Posting, Txn

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".pdf"}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "store": {"type": "string"},
        "date": {"type": "string"},
        "bill_no": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "quantity": {"type": "number"},
                           "unit_price": {"type": "number"}, "total": {"type": "number"}},
            "required": ["name", "quantity", "total"]}},
        "total": {"type": "number"},
    },
    "required": ["store", "date", "items", "total"],
}

PROMPT = """You are reading a photo of an Indian grocery bill (stores such as DMart, KPN, \
Smart Point, Army CSD canteen). Return ONE JSON object:
- store: the store name as printed
- date: the bill date as YYYY-MM-DD
- bill_no: the bill/invoice number, or "" if none
- items: one entry per purchased line, with
    name: exactly as printed, including brand and pack size (e.g. "LION DATES 500G")
    quantity: number of packs, or the weight in kg for loose items sold by weight
    unit_price: price per pack or per kg as printed
    total: the line amount actually charged
- total: the final amount payable (Net Amount / Bill Total, after discounts)
Rules: plain numbers only - no currency symbols or thousands separators. Skip GST summary \
rows, "you saved" rows, payment/tender rows and loyalty rows. Never invent a line you \
cannot read."""


# --------------------------------------------------------------------------- canonical items

SIZE_RX = re.compile(r"(?:(\d+)\s*[x×*]\s*)?(\d+(?:\.\d+)?)\s*"
                     r"(kgs?|gms?|grams?|gr|g|ltrs?|litres?|liters?|lt|l|ml)\b", re.IGNORECASE)
PCS_RX = re.compile(r"\b(?:pack of|pk of)\s*(\d+)\b|\b(\d+)\s*(?:pcs|pc|nos|pieces|units|n)\b",
                    re.IGNORECASE)
UNIT_OF = {"kg": ("kg", Decimal(1)), "kgs": ("kg", Decimal(1)),
           "g": ("kg", Decimal("0.001")), "gm": ("kg", Decimal("0.001")), "gms": ("kg", Decimal("0.001")),
           "gr": ("kg", Decimal("0.001")), "gram": ("kg", Decimal("0.001")), "grams": ("kg", Decimal("0.001")),
           "l": ("l", Decimal(1)), "lt": ("l", Decimal(1)), "ltr": ("l", Decimal(1)), "ltrs": ("l", Decimal(1)),
           "litre": ("l", Decimal(1)), "litres": ("l", Decimal(1)), "liter": ("l", Decimal(1)),
           "liters": ("l", Decimal(1)), "ml": ("l", Decimal("0.001"))}


@dataclass
class Item:
    raw_name: str
    canonical_name: str
    category: str
    brand: str | None
    quantity: Decimal
    unit: str
    unit_price: Decimal | None
    total_price: Decimal
    needs_review: bool = False

    @property
    def price_per_unit(self) -> Decimal | None:
        return (self.total_price / self.quantity).quantize(Decimal("0.01")) if self.quantity else None


def raw_key(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", raw.lower())).strip()


class Canonicalizer:
    def __init__(self, rules_path: Path | None = None, conn=None):
        rules_path = rules_path or (root() / "config" / "canonical_items.toml")
        with open(rules_path, "rb") as fh:
            rules = tomllib.load(fh)
        self.items = [(it["name"], it["category"], it["unit"],
                       [re.compile(p, re.IGNORECASE) for p in it["patterns"]])
                      for it in rules.get("item", [])]
        self.brands = sorted(rules.get("brands", {}).get("known", []), key=len, reverse=True)
        self.stores = {name: [re.compile(p, re.IGNORECASE) for p in pats]
                       for name, pats in rules.get("stores", {}).items()}
        self.conn = conn

    def store(self, raw: str) -> str:
        for name, pats in self.stores.items():
            if any(p.search(raw or "") for p in pats):
                return name
        return (raw or "Unknown store").strip().title()

    def _learned(self, key: str):
        if self.conn is None:
            return None
        return self.conn.execute("SELECT canonical_name, category, unit FROM canonical_map "
                                 "WHERE raw_key = ?", (key,)).fetchone()

    def normalize(self, raw: str, qty: Decimal, unit_price: Decimal | None, total: Decimal) -> Item:
        key = raw_key(raw)
        brand = next((b for b in self.brands if re.search(rf"\b{re.escape(b)}\b", raw, re.IGNORECASE)), None)
        name = category = unit = None
        learned = self._learned(key)
        if learned:
            name, category, unit = learned["canonical_name"], learned["category"], learned["unit"]
        else:
            for n, c, u, pats in self.items:
                if any(p.search(raw) for p in pats):
                    name, category, unit = n, c, u
                    break
        review = name is None
        qty = qty if qty and qty > 0 else Decimal(1)

        size = SIZE_RX.search(raw)
        if size:
            count = Decimal(size.group(1) or 1)
            size_unit, factor = UNIT_OF[size.group(3).lower()]
            quantity = qty * count * Decimal(size.group(2)) * factor
            unit = size_unit if unit != size_unit else unit
        elif unit in ("kg", "l"):
            quantity = qty                    # loose item: the printed quantity is the weight
        else:
            pcs = PCS_RX.search(raw)
            quantity = qty * Decimal((pcs.group(1) or pcs.group(2)) if pcs else 1)
            unit = unit or "pcs"
        if name is None:
            name = self._fallback_name(raw)
            category = "Uncategorized"
        return Item(raw, name, category, brand, quantity.normalize(), unit, unit_price, total, review)

    @staticmethod
    def _fallback_name(raw: str) -> str:
        # The brand stays in: "SAFFOLA GOLD 2x1 L" -> "Saffola Gold", not a bare "Gold",
        # because an unmapped line is waiting for a person to recognise it.
        text = SIZE_RX.sub(" ", raw)
        text = PCS_RX.sub(" ", text)
        text = re.sub(r"[^A-Za-z ]", " ", text)
        return re.sub(r"\s+", " ", text).strip().title() or raw.strip()


# --------------------------------------------------------------------------- extraction

@dataclass
class Extraction:
    store: str
    date: date
    bill_no: str
    total: Decimal
    lines: list[dict] = field(default_factory=list)   # name, quantity, unit_price, total (Decimals)
    problems: list[str] = field(default_factory=list)

    @property
    def items_total(self) -> Decimal:
        return money(sum((ln["total"] for ln in self.lines), Decimal(0)))


def coerce(value, tolerance: Decimal) -> tuple[Extraction | None, list[str]]:
    """Validate a model reply. Returns (extraction or None, problems)."""
    problems: list[str] = []
    if not isinstance(value, dict):
        return None, ["reply is not a JSON object"]
    store = str(value.get("store") or "").strip()
    if not store:
        problems.append("store is missing")
    try:
        d = parse_date(value.get("date"))
    except ValueError:
        return None, problems + [f"date {value.get('date')!r} is not YYYY-MM-DD"]
    try:
        total = money(to_decimal(value.get("total")))
    except ValueError as ex:
        return None, problems + [f"total: {ex}"]
    items = value.get("items")
    if not isinstance(items, list) or not items:
        return None, problems + ["items must be a non-empty list"]
    lines = []
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict) or not str(it.get("name") or "").strip():
            problems.append(f"item {n} has no name")
            continue
        try:
            line_total = money(to_decimal(it.get("total")))
        except ValueError as ex:
            problems.append(f"item {n} ({it.get('name')}): total {ex}")
            continue
        try:
            qty = to_decimal(it.get("quantity", 1))
        except ValueError:
            qty = Decimal(1)
        try:
            unit_price = money(to_decimal(it["unit_price"])) if it.get("unit_price") not in (None, "") else None
        except ValueError:
            unit_price = None
        lines.append({"name": str(it["name"]).strip(), "quantity": qty,
                      "unit_price": unit_price, "total": line_total})
    if not lines:
        return None, problems + ["no readable item lines"]
    ex = Extraction(store, d, str(value.get("bill_no") or "").strip(), total, lines)
    gap = ex.items_total - total
    if abs(gap) > tolerance:
        problems.append(f"the item totals add up to {ex.items_total} but the bill total is {total} "
                        f"(off by {gap}); re-read the line amounts and the final total")
    ex.problems = problems
    return ex, problems


def load_images(path: Path, max_side: int) -> list[bytes]:
    """JPEG bytes per page. PDFs are rendered page by page; HEIC goes through `sips`."""
    from PIL import Image

    if path.suffix.lower() == ".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = [p.to_image(resolution=200).original for p in pdf.pages]
    else:
        src = path
        if path.suffix.lower() in (".heic", ".heif"):
            tmp = Path(tempfile.mkdtemp()) / (path.stem + ".jpg")
            res = subprocess.run(["sips", "-s", "format", "jpeg", str(path), "--out", str(tmp)],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise PFError(f"{path.name}: could not convert HEIC ({res.stderr.strip()})")
            src = tmp
        try:
            pages = [Image.open(src)]
        except OSError as ex:
            raise PFError(f"{path.name}: not an image Pillow can read ({ex})")
    out = []
    for img in pages:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        out.append(buf.getvalue())
    return out


def extract_with_model(path: Path, cfg: dict, chat=None) -> Extraction:
    rcfg = cfg.get("receipts", {})
    tolerance = to_decimal(rcfg.get("total_tolerance", 2))
    images = [image_b64(b) for b in load_images(path, int(rcfg.get("max_image_side", 1600)))]
    model = cfg.get("ollama", {}).get("vision_model", "gemma3:4b")
    value, _ = ollama_json([{"role": "user", "content": PROMPT, "images": images}],
                           model, schema=EXTRACT_SCHEMA, validate=lambda v: coerce(v, tolerance)[1],
                           cfg=cfg, chat=chat)
    ex, problems = coerce(value, tolerance)
    if ex is None:
        raise OllamaError(f"{path.name}: the model's reading is unusable: " + "; ".join(problems))
    return ex


# --------------------------------------------------------------------------- ingest

@dataclass
class Result:
    receipt_id: str
    status: str            # ingested | duplicate | dry-run
    store: str = ""
    total: Decimal = Decimal(0)
    items: int = 0
    review: bool = False
    attached: bool = False   # joined an already-imported statement line


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def matching_statement_line(entries, paid_with: str, total: Decimal, when: date,
                            window: int) -> tuple[str, str] | None:
    """(import_id, counter account) of an already-imported statement line that is this
    bill - same paying account, same amount, within the window, not yet claimed by
    another receipt. Lets a receipt ingested AFTER its statement attach to that line
    instead of booking the money a second time."""
    claimed = ledger_io.meta_values(entries, "matched_import_id")
    for t in ledger_io.transactions(entries):
        iid = t.meta.get("import_id")
        if not iid or iid in claimed or abs((t.date - when).days) > window:
            continue
        legs = [p for p in t.postings if p.account == paid_with and p.units
                and p.units.currency == "INR" and money(p.units.number) == -total]
        others = [p.account for p in t.postings if p.account != paid_with]
        if legs and len(others) == 1:
            return iid, others[0]
    return None


def build_txn(rid: str, ex: Extraction, store: str, n_items: int, paid_with: str,
              entity: str, cfg: dict, matched: tuple[str, str] | None = None) -> Txn:
    """The receipt's ledger entry. Normally expense <- paying account. When the statement
    line is already in the ledger, it instead moves that line's amount from whatever
    the importer categorised it as into the grocery account (a zero-sum entry when the
    importer already chose groceries), so the bill is tagged without being counted twice."""
    expense = fill_entity(cfg.get("receipts", {}).get("expense_account",
                                                       "Expenses:{entity}:Food:Groceries"), entity, cfg)
    meta = {"receipt_id": rid}
    if ex.bill_no:
        meta["bill_no"] = ex.bill_no
    source = paid_with
    if matched:
        meta["matched_import_id"], source = matched
    if ex.problems:
        meta["review"] = "; ".join(ex.problems)
    return Txn(ex.date, store, f"Groceries - {n_items} item{'s' if n_items != 1 else ''}",
               [Posting(expense, ex.total), Posting(source, -ex.total)],
               flag="!" if ex.problems else "*", tags=["groceries"], links=[f"receipt-{rid}"],
               meta=meta)


def ingest(path: Path, *, paid_with: str | None = None, entity: str | None = None,
           from_json: Path | None = None, dry_run: bool = False, cfg: dict | None = None,
           chat=None, conn=None) -> Result:
    cfg = cfg or load_config()
    own_conn = conn is None
    conn = conn or sidecar.connect()
    try:
        sha = file_sha256(path)
        rid = "R" + sha[:8]
        row = conn.execute("SELECT receipt_id FROM receipts WHERE image_sha256 = ?", (sha,)).fetchone()
        if row:
            return Result(row["receipt_id"], "duplicate")

        rcfg = cfg.get("receipts", {})
        paid_with = paid_with or rcfg.get("default_paid_with", "Assets:Self:Cash")
        entity = entity or entity_of_account(paid_with, cfg) or cfg["general"]["default_entity"]
        entity_label(entity, cfg)  # fail early on a bad entity

        if from_json:
            tolerance = to_decimal(rcfg.get("total_tolerance", 2))
            ex, problems = coerce(json.loads(Path(from_json).read_text()), tolerance)
            if ex is None:
                raise PFError(f"{from_json}: " + "; ".join(problems))
        else:
            ex = extract_with_model(path, cfg, chat=chat)

        canon = Canonicalizer(conn=conn)
        store = canon.store(ex.store)
        items = [canon.normalize(ln["name"], ln["quantity"], ln["unit_price"], ln["total"])
                 for ln in ex.lines]
        needs_review = bool(ex.problems) or any(i.needs_review for i in items)
        window = int(cfg.get("transfers", {}).get("window_days", 2))
        matched = matching_statement_line(ledger_io.load()[0], paid_with, ex.total, ex.date, window)
        txn = build_txn(rid, ex, store, len(items), paid_with, entity, cfg, matched)
        txn.check_balance()

        # The database is committed first and its rows removed again if the ledger append
        # fails: a locked database then fails BEFORE the ledger is touched, and the two
        # never disagree about whether a receipt exists.
        with conn:
            conn.execute(
                "INSERT INTO receipts (receipt_id, store, date, bill_no, total, items_total, "
                "paid_with, entity, image_path, image_sha256, needs_review) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, store, ex.date.isoformat(), ex.bill_no, float(ex.total), float(ex.items_total),
                 paid_with, entity, str(path), sha, int(needs_review)))
            conn.executemany(
                "INSERT INTO receipt_items (receipt_id, canonical_name, brand, quantity, unit, unit_price, "
                "total_price, price_per_unit, category, raw_name, store, date, needs_review) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, i.canonical_name, i.brand, float(i.quantity), i.unit,
                  float(i.unit_price) if i.unit_price is not None else None, float(i.total_price),
                  float(i.price_per_unit) if i.price_per_unit is not None else None,
                  i.category, i.raw_name, store, ex.date.isoformat(), int(i.needs_review))
                 for i in items])
            if dry_run:
                print(f"\n; ===== {path.name} -> would append to {entity}'s ledger =====")
                print(txn.render())
                for i in items:
                    print(f";   {i.raw_name:<34} -> {i.canonical_name} / {i.category} / "
                          f"{i.brand or '-'} / {i.quantity} {i.unit} / {i.total_price}"
                          + ("   [review]" if i.needs_review else ""))
                raise _DryRun()
        try:
            ledger_io.append(entity_ledger(entity, cfg), [txn.render()], header=f"receipt {rid}")
        except BaseException:
            with conn:
                conn.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (rid,))
                conn.execute("DELETE FROM receipts WHERE receipt_id = ?", (rid,))
            raise
        return Result(rid, "ingested", store, ex.total, len(items), needs_review, matched is not None)
    except _DryRun:
        return Result(rid, "dry-run", store, ex.total, len(items), needs_review, matched is not None)
    except sqlite3.Error as dbex:   # e.g. locked by Fava/analytics mid-query
        raise PFError(f"sidecar.db: {dbex} - nothing was written; close other readers and retry")
    finally:
        if own_conn:
            conn.close()


class _DryRun(Exception):
    """Raised inside the DB transaction so a dry run rolls back its inserts."""


# --------------------------------------------------------------------------- CLI

def _receipt_paths(args_paths: list[str]) -> list[Path]:
    paths = [Path(p) for p in args_paths] or [data_dir() / "raw_receipts"]
    out = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES)
        elif p.exists():
            out.append(p)
        else:
            raise PFError(f"no such file: {p}")
    return out


def cmd_ingest(args) -> int:
    cfg = load_config()
    paths = _receipt_paths(args.paths)
    if args.from_json and len(paths) != 1:
        raise PFError("--from-json needs exactly one receipt file (it is the photo's extraction)")
    failures = 0
    for path in paths:
        try:
            r = ingest(path, paid_with=args.paid_with, entity=args.entity,
                       from_json=args.from_json, dry_run=args.dry_run, cfg=cfg)
        except PFError as ex:
            print(f"FAIL  {path.name}: {ex}", file=sys.stderr)
            failures += 1
            continue
        if r.status == "duplicate":
            print(f"skip  {path.name}: already ingested as {r.receipt_id}")
        else:
            print(f"{r.status:<8} {path.name}: {r.receipt_id} {r.store} ₹{r.total} "
                  f"{r.items} items" + ("  [needs review]" if r.review else "")
                  + ("  [attached to the imported statement line]" if r.attached else ""))
    return 1 if failures else 0


def cmd_review(_args) -> int:
    conn = sidecar.connect()
    rows = conn.execute("SELECT receipt_id, store, date, total, items_total FROM receipts "
                        "WHERE needs_review = 1 AND ABS(total - items_total) > 0.005 ORDER BY date").fetchall()
    for r in rows:
        print(f"receipt {r['receipt_id']} {r['date']} {r['store']}: bill total {r['total']} but the "
              f"items add up to {r['items_total']} - check the photo, then fix with --from-json")
    items = conn.execute("SELECT raw_name, COUNT(*) n FROM receipt_items WHERE needs_review = 1 "
                         "GROUP BY raw_name ORDER BY n DESC").fetchall()
    for it in items:
        print(f"unmapped  {it['raw_name']!r} (x{it['n']})  -> receipt_engine.py map "
              f"{json.dumps(it['raw_name'])} --to NAME --category CAT --unit kg|l|pcs")
    if not rows and not items:
        print("nothing to review")
    return 0


def cmd_map(args) -> int:
    conn = sidecar.connect()
    key = raw_key(args.raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO canonical_map VALUES (?,?,?,?)",
                     (key, args.to, args.category, args.unit))
        # Re-label rows already stored under this raw text.
        rows = conn.execute("SELECT id, raw_name, quantity, unit, total_price FROM receipt_items").fetchall()
        fixed = 0
        canon = Canonicalizer(conn=conn)
        for r in rows:
            if raw_key(r["raw_name"] or "") != key:
                continue
            it = canon.normalize(r["raw_name"], Decimal(1), None, Decimal(str(r["total_price"])))
            conn.execute("UPDATE receipt_items SET canonical_name=?, category=?, unit=?, needs_review=0 "
                         "WHERE id=?", (args.to, args.category, it.unit, r["id"]))
            fixed += 1
        # A receipt stays flagged only while its totals disagree or an item is still unmapped.
        conn.execute("UPDATE receipts SET needs_review = (ABS(total - items_total) > 0.005 OR EXISTS "
                     "(SELECT 1 FROM receipt_items i WHERE i.receipt_id = receipts.receipt_id "
                     "AND i.needs_review = 1))")
    print(f"mapped {args.raw!r} -> {args.to} ({args.category}, {args.unit}); updated {fixed} stored line(s)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("paths", nargs="*")
    i.add_argument("--paid-with", help="ledger account that paid (default [receipts].default_paid_with)")
    i.add_argument("--entity", help="entity whose ledger gets the expense (default: from --paid-with)")
    i.add_argument("--from-json", help="use this extraction JSON instead of calling the model")
    i.add_argument("--dry-run", action="store_true")
    sub.add_parser("review")
    m = sub.add_parser("map")
    m.add_argument("raw")
    m.add_argument("--to", required=True)
    m.add_argument("--category", required=True)
    m.add_argument("--unit", required=True, choices=["kg", "l", "pcs"])
    args = p.parse_args()
    setup_logging(args.verbose)
    return {"ingest": cmd_ingest, "review": cmd_review, "map": cmd_map}[args.cmd](args)


if __name__ == "__main__":
    run_cli(main)
