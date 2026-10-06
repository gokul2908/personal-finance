"""End-to-end and edge-case tests. Every test runs against a fresh synthetic demo tree."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import ai_nudge
import analytics
import common
import google_sync
import importer
import ledger_io
import milestones
import prices
import receipt_engine
import sidecar
from importer import Line, PasswordError, StatementError

D = Decimal
STATEMENTS = lambda pf: pf / "data" / "raw_statements"  # noqa: E731


def import_all(paths, cfg=None):
    cfg = cfg or common.load_config()
    lines = []
    for p in paths:
        _, ls, _ = importer.read_statement(p, cfg)
        lines += ls
    entries, _, _ = ledger_io.load()
    built, stats = importer.build(lines, entries, cfg)
    importer.write(built, cfg, dry_run=False, header="test import")
    return built, stats


def ledger_ok():
    entries, errors, _ = ledger_io.load()
    assert not errors, [e.message for e in errors]
    return entries


# --------------------------------------------------------------------------- passwords

def test_each_issuer_unlocks_with_its_own_pattern(pf):
    cfg = common.load_config()
    want = {"hdfc-regalia-credit-sep26.pdf": "{name4_upper}{ddmm}",
            "sbicard-statement-sep26.pdf": "{ddmmyyyy}{last4}",
            "icici-amazonpay-sep26.pdf": "{name4_lower}{ddmm}",
            "mom-sb-statement-sep26.pdf": "{customer_id}"}
    for name, pattern in want.items():
        _, lines, how = importer.read_statement(STATEMENTS(pf) / name, cfg)
        assert how == pattern and lines


def test_wrong_password_names_patterns_never_the_password(pf, monkeypatch):
    monkeypatch.setenv("PF_SECRET_SELF_DOB", "1991-01-01")
    with pytest.raises(PasswordError) as ex:
        importer.read_statement(STATEMENTS(pf) / "hdfc-regalia-credit-sep26.pdf", common.load_config())
    msg = str(ex.value)
    assert "{name4_upper}{ddmm}" in msg and "ARJU0101" not in msg and "--ask-password" in msg


def test_missing_secret_is_named(pf, monkeypatch):
    monkeypatch.delenv("PF_SECRET_SELF_DOB")
    with pytest.raises(PasswordError) as ex:
        importer.read_statement(STATEMENTS(pf) / "sbicard-statement-sep26.pdf", common.load_config())
    assert "self.dob" in str(ex.value)


def test_damaged_pdf_is_reported_not_crashed(pf):
    bad = STATEMENTS(pf) / "hdfc-regalia-broken.pdf"
    bad.write_bytes(b"%PDF-1.7 this is not really a pdf")
    with pytest.raises(StatementError, match="not a readable PDF"):
        importer.read_statement(bad, common.load_config())


def test_unknown_filename_identified_by_last4(pf):
    src = STATEMENTS(pf) / "icici-amazonpay-sep26.pdf"
    renamed = STATEMENTS(pf) / "statement-download.pdf"
    src.rename(renamed)
    s, lines, _ = importer.read_statement(renamed, common.load_config())
    assert s["id"] == "icici-amazon" and len(lines) == 3


# --------------------------------------------------------------------------- parsers

def test_card_parsers_sign_points_and_refs(pf):
    cfg = common.load_config()
    _, hdfc, _ = importer.read_statement(STATEMENTS(pf) / "hdfc-regalia-credit-sep26.pdf", cfg)
    assert len(hdfc) == 7
    pay = next(l for l in hdfc if "PAYMENT" in l.description)
    assert pay.amount == D("45000.00")
    swiggy = next(l for l in hdfc if "SWIGGY" in l.description)
    assert (swiggy.amount, swiggy.points, swiggy.description) == (D("-850.00"), 22, "SWIGGY BANGALORE")
    _, sbi, _ = importer.read_statement(STATEMENTS(pf) / "sbicard-statement-sep26.pdf", cfg)
    assert [l.amount for l in sbi] == [D("-2499.00"), D("-780.00"), D("499.00"), D("3000.00")]
    _, icici, _ = importer.read_statement(STATEMENTS(pf) / "icici-amazonpay-sep26.pdf", cfg)
    assert icici[0].ref == "10234567891" and icici[0].points == 99 and icici[0].amount == D("-1999.00")


def test_balance_parser_reads_direction_from_balance(pf):
    _, lines, _ = importer.read_statement(STATEMENTS(pf) / "hdfc-acct-statement-sep26.pdf",
                                          common.load_config())
    assert [l.amount for l in lines] == [D("150000.00"), D("-45000.00"), D("-50000.00"),
                                         D("-30000.00"), D("812.00")]
    assert all(not l.flags for l in lines)
    assert lines[1].ref == "0000123456"          # value date dropped, reference kept


def test_balance_parser_flags_when_movement_disagrees():
    src = {"id": "x", "parser": "bank_balance", "account": "Assets:Self:HDFC", "entity": "self", "kind": "bank"}
    lines = importer.parse_lines(["Opening Balance 1,000.00",
                                  "01/09/26 SOMETHING 100.00 1,500.00"], src)
    assert lines[0].flags and "did not match" in lines[0].flags[0]


# --------------------------------------------------------------------------- transfers

def _line(account, entity, d, amount, desc, kind="bank"):
    src = {"id": account, "account": account, "entity": entity, "kind": kind, "parser": "bank_balance"}
    return Line(d, desc, D(amount), src)


def _build(lines, pf):
    entries, _, _ = ledger_io.load()
    return importer.build(lines, entries, common.load_config())


def test_transfer_pairs_within_window_with_tax_note(pf):
    out, stats = _build([_line("Assets:Self:HDFC", "self", date(2026, 9, 10), "-50000", "IMPS-TO MOM"),
                         _line("Assets:Mom:Savings", "mom", date(2026, 9, 12), "50000", "IMPS CR")], pf)
    assert stats["transfers paired"] == 1 and len(out) == 1
    entity, txn = out[0]
    assert entity == "self" and "gift" in txn.tags and "56(2)(x)" in txn.meta["tax_note"]
    assert {(p.account, p.amount) for p in txn.postings} == {("Assets:Mom:Savings", D(50000)),
                                                             ("Assets:Self:HDFC", D(-50000))}


def test_no_pair_beyond_window_or_without_keyword(pf):
    far = [_line("Assets:Self:HDFC", "self", date(2026, 9, 10), "-50000", "IMPS-TO MOM"),
           _line("Assets:Mom:Savings", "mom", date(2026, 9, 13), "50000", "IMPS CR")]
    assert _build(far, pf)[1]["transfers paired"] == 0
    nokw = [_line("Liabilities:Self:CC:HDFC-Regalia", "self", date(2026, 9, 10), "-500", "SHOP A", "card"),
            _line("Liabilities:Self:CC:SBI-Cashback", "self", date(2026, 9, 10), "500", "REFUND B", "card")]
    assert _build(nokw, pf)[1]["transfers paired"] == 0


def test_ambiguous_transfer_is_flagged(pf):
    out, _ = _build([_line("Assets:Self:HDFC", "self", date(2026, 9, 10), "-10000", "NEFT TO X"),
                     _line("Assets:Mom:Savings", "mom", date(2026, 9, 11), "10000", "NEFT CR"),
                     _line("Assets:Wife:Savings", "wife", date(2026, 9, 9), "10000", "NEFT CR")], pf)
    transfer = next(t for _, t in out if t.payee and t.payee.startswith("Transfer"))
    assert transfer.flag == "!" and "more than one" in transfer.meta["review"]


def test_cross_batch_transfer_closes_through_clearing(pf):
    # Self's side arrives first (unmatched NEFT -> no rule -> Uncategorized? no: card payment)
    import_all([STATEMENTS(pf) / "sbicard-statement-sep26.pdf"])
    later = [_line("Assets:Self:HDFC", "self", date(2026, 9, 21), "-3000", "BILLDESK SBI CARD")]
    out, stats = _build(later, pf)
    assert stats["transfers closed against Clearing"] == 1
    _, txn = out[0]
    assert any(p.account == "Assets:Self:Clearing" and p.amount == D(3000) for p in txn.postings)


def test_full_import_is_idempotent_and_skips_recorded(pf):
    r = pf / "data" / "raw_receipts"
    receipt_engine.ingest(r / "dmart-2026-09-14.png", from_json=r / "dmart-2026-09-14.json",
                          paid_with="Liabilities:Self:CC:HDFC-Regalia")
    pdfs = sorted(STATEMENTS(pf).glob("*.pdf"))
    _, first = import_all(pdfs)
    assert first["already recorded by hand/receipt"] == 1 and first["transfers paired"] == 2
    _, second = import_all(pdfs)
    assert second["already in ledger"] == 22 and "new" not in second   # incl. the matched line's note
    ledger_ok()


# --------------------------------------------------------------------------- receipts

@pytest.mark.parametrize("raw,qty,expect", [
    ("Lion Dates 500g", "1", ("Dates", "Dry Fruits", "Lion", D("0.5"), "kg")),
    ("AMUL BUTTER 500G", "2", ("Butter", "Dairy", "Amul", D("1"), "kg")),
    ("TOMATO", "1.25", ("Tomato", "Vegetables", None, D("1.25"), "kg")),
    ("FARM EGGS 6 PCS", "2", ("Eggs", "Dairy", None, D("12"), "pcs")),
    ("NANDINI GHEE 1L", "1", ("Ghee", "Dairy", "Nandini", D("1"), "l")),
    ("SAFFOLA GOLD 2x1 L", "1", ("Saffola Gold", "Uncategorized", "Saffola", D("2"), "l")),
])
def test_canonical_normalisation(pf, raw, qty, expect):
    it = receipt_engine.Canonicalizer().normalize(raw, D(qty), None, D("100"))
    assert (it.canonical_name, it.category, it.brand, it.quantity, it.unit) == expect
    assert it.needs_review == (expect[1] == "Uncategorized")


def test_learned_mapping_wins(pf):
    conn = sidecar.connect()
    conn.execute("INSERT INTO canonical_map VALUES (?,?,?,?)",
                 (receipt_engine.raw_key("SAFFOLA GOLD 2x1 L"), "Sunflower Oil", "Oils", "l"))
    it = receipt_engine.Canonicalizer(conn=conn).normalize("SAFFOLA GOLD 2x1 L", D(1), None, D(300))
    assert (it.canonical_name, it.quantity, it.needs_review) == ("Sunflower Oil", D(2), False)


@pytest.mark.parametrize("reply", [
    '{"a": 1}',
    'Sure! Here it is:\n```json\n{"a": 1}\n```\nHope that helps.',
    'The answer is {"a": 1, } as requested',
    '```\n{"a": 1,}\n```',
])
def test_extract_json_tolerates_model_noise(reply):
    assert common.extract_json(reply) == {"a": 1}


@pytest.mark.parametrize("reply", ["", "no json here", "{broken"])
def test_extract_json_rejects_garbage(reply):
    with pytest.raises(ValueError):
        common.extract_json(reply)


GOOD = {"store": "DMART", "date": "2026-09-14", "bill_no": "1",
        "items": [{"name": "LION DATES 500G", "quantity": 1, "unit_price": 180, "total": 180},
                  {"name": "TOMATO", "quantity": 1.0, "unit_price": 60, "total": 60}],
        "total": 240}


def fake_model(*replies):
    calls = []

    def chat(messages, model, fmt=None, cfg=None):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]
    return chat, calls


def test_ollama_json_retries_with_the_problem(pf):
    bad_sum = dict(GOOD, total=999)
    chat, calls = fake_model("not json at all", "```json\n" + json.dumps(bad_sum) + "\n```", json.dumps(GOOD))
    value, problems = common.ollama_json([{"role": "user", "content": "x"}], "m",
                                         validate=lambda v: receipt_engine.coerce(v, D(2))[1], chat=chat)
    assert value == GOOD and problems == [] and len(calls) == 3
    assert "add up to" in calls[2][-1]["content"]        # the model was told what was wrong


def test_ollama_json_gives_up_with_problems_or_raises(pf):
    chat, _ = fake_model(json.dumps(dict(GOOD, total=999)))
    value, problems = common.ollama_json([], "m", validate=lambda v: receipt_engine.coerce(v, D(2))[1],
                                         retries=1, chat=chat)
    assert value["total"] == 999 and problems
    chat, _ = fake_model("nope")
    with pytest.raises(common.OllamaError):
        common.ollama_json([], "m", retries=1, chat=chat)


def test_vision_ingest_writes_sidecar_and_tagged_ledger(pf):
    photo = pf / "data" / "raw_receipts" / "dmart-2026-09-14.png"
    chat, _ = fake_model(json.dumps(GOOD))
    res = receipt_engine.ingest(photo, chat=chat)
    assert res.status == "ingested" and res.items == 2
    entries = ledger_ok()
    txn = next(t for t in ledger_io.transactions(entries) if t.meta.get("receipt_id") == res.receipt_id)
    assert f"receipt-{res.receipt_id}" in txn.links and "groceries" in txn.tags
    rows = sidecar.connect().execute("SELECT canonical_name, quantity, price_per_unit FROM receipt_items "
                                     "WHERE receipt_id = ? ORDER BY id", (res.receipt_id,)).fetchall()
    assert [tuple(r) for r in rows] == [("Dates", 0.5, 360.0), ("Tomato", 1.0, 60.0)]
    assert receipt_engine.ingest(photo, chat=chat).status == "duplicate"


def test_mismatched_receipt_is_kept_but_flagged(pf):
    photo = pf / "data" / "raw_receipts" / "kpn-2026-06-12.png"
    chat, _ = fake_model(json.dumps(dict(GOOD, total=300)))
    res = receipt_engine.ingest(photo, chat=chat)
    txn = next(t for t in ledger_io.transactions(ledger_ok()) if t.meta.get("receipt_id") == res.receipt_id)
    assert res.review and txn.flag == "!" and "add up to" in txn.meta["review"]
    assert txn.postings[0].units.number == D(300)    # the ledger carries the printed total


def test_failed_ledger_append_rolls_back_sidecar(pf):
    photo = pf / "data" / "raw_receipts" / "kpn-2026-06-12.png"
    before = (pf / "ledgers" / "self.beancount").read_text()
    chat, _ = fake_model(json.dumps(GOOD))
    with pytest.raises(ledger_io.LedgerError):
        receipt_engine.ingest(photo, chat=chat, paid_with="Assets:Self:NotOpened")
    assert (pf / "ledgers" / "self.beancount").read_text() == before
    assert sidecar.connect().execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


# --------------------------------------------------------------------------- milestones / analytics

def test_card_year_boundaries():
    assert milestones.card_year("03-15", date(2026, 3, 14)) == (date(2025, 3, 15), date(2026, 3, 14))
    assert milestones.card_year("03-15", date(2026, 3, 15)) == (date(2026, 3, 15), date(2027, 3, 14))
    assert milestones.card_year("02-29", date(2027, 6, 1)) == (date(2027, 2, 28), date(2028, 2, 28))


def test_milestone_excludes_fuel_and_counts_wallet_loads(pf):
    cfg = common.load_config()
    for t in milestones.amazon_gc_txns(amount=D(5000), paid_with="Liabilities:Self:CC:HDFC-Regalia",
                                       wallet="Assets:Self:Wallet:AmazonPay", cashback=D(100),
                                       pending=True, when=date(2026, 9, 8), entity_label_="Self"):
        t.check_balance()
        ledger_io.append(pf / "ledgers" / "self.beancount", [t.render()])
    import_all([STATEMENTS(pf) / "hdfc-regalia-credit-sep26.pdf"])
    prog = {p.card: p for p in milestones.milestone_progress(ledger_ok(), cfg, date(2026, 9, 30))}
    # laptop 120000 + swiggy 850 + GC 5000 + dmart 1234.50 + croma 85000 + traders 640; fuel excluded
    assert prog["hdfc-regalia"].spent == D("212724.50")
    assert not prog["hdfc-regalia"].on_track


def test_reward_redeem_balances_in_both_currencies(pf):
    cfg = common.load_config()
    card = common.sources(cfg)["hdfc-regalia"]
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        milestones.earn_txn(card, D(5000), date(2026, 9, 1), None).render(),
        milestones.redeem_txn(card, D(4000), D(2000), card["account"], date(2026, 9, 2)).render()])
    pts = {p.card: p for p in milestones.points_status(ledger_ok(), cfg, date(2026, 9, 30))}
    assert pts["hdfc-regalia"].balance == D(1000)


def test_wallet_drag_math(pf):
    cfg = common.load_config()
    entries = ledger_ok()          # demo: 12,000 in Amazon Pay from 2026-06-01
    (drag,) = analytics.wallet_drag(entries, cfg, date(2026, 7, 1), date(2026, 7, 10))
    assert drag.avg_balance == D("12000.00")
    assert drag.lost_interest == (D(12000) * D("0.07") / 365 * 10).quantize(D("0.01"))


def test_price_timeline_quarters_and_change(pf):
    r = pf / "data" / "raw_receipts"
    for name in ("smartpoint-2026-03-10", "kpn-2026-06-12", "csd-2026-09-27"):
        receipt_engine.ingest(r / f"{name}.png", from_json=r / f"{name}.json")
    rows = analytics.price_timeline("tomato")
    assert [(x["quarter"], x["avg_price"], x["change_pct"]) for x in rows] == \
        [("2026-Q1", 30.0, None), ("2026-Q2", 42.0, 40.0), ("2026-Q3", 62.0, 47.6)]
    infl = analytics.grocery_inflation(None, common.load_config(), date(2026, 9, 30))
    assert infl[0]["item"] == "Tomato"


# --------------------------------------------------------------------------- google / nudges / misc

def test_reminders_are_stable_and_skip_the_past(pf):
    cfg = common.load_config()
    entries = ledger_ok()
    a = google_sync.plan_reminders(entries, cfg, date(2026, 9, 30), 60)
    b = google_sync.plan_reminders(entries, cfg, date(2026, 9, 30), 60)
    assert [r.event_id for r in a] == [r.event_id for r in b]
    assert all(r.on >= date(2026, 9, 30) for r in a)
    health = [r for r in a if "Health" in r.due.title]
    assert sorted(r.days_before for r in health) == [2, 7]
    assert google_sync.next_card_due(31, date(2027, 2, 10)) == date(2027, 2, 28)


def test_due_tag_form(pf):
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        '2026-09-01 * "LIC" "Premium reminder" #due-2026-10-20\n'
        "  Expenses:Self:Insurance:Term   1.00 INR\n  Assets:Self:Cash"])
    dues = google_sync.ledger_dues(ledger_ok())
    assert any(d.when == date(2026, 10, 20) and d.title == "Premium reminder" for d in dues)


def test_nudge_summary_and_guards(pf):
    summary = ai_nudge.build_summary(ledger_ok(), common.load_config(), date(2026, 9, 30))
    text = ai_nudge.render_summary(summary, date(2026, 9, 30))
    assert "Idle cash drag" in text and "Card fee-waiver" in text
    assert "Assets:Self:Wallet:AmazonPay: now ₹9,000.00" in text
    assert ai_nudge.validate({"nudges": [{"title": "t", "evidence": "e", "action": "a"}]}) == []
    assert ai_nudge.validate({"nudges": [{"title": "t"}]})
    assert ai_nudge.numbers_not_in("now ₹9,000.00 and ₹99,999", text) == ["99999"]


def test_split_must_add_up():
    with pytest.raises(common.PFError, match="difference 500.00"):
        importer.split_txn(when=date(2026, 9, 1), from_account="Assets:Self:HDFC", total=D(30000),
                           parts=[("Expenses:Self:Housing:Rent", D(26500)), ("Assets:Self:Cash", D(3000))],
                           payee=None, narration="x")


def test_append_rolls_back_on_ledger_error(pf):
    path = pf / "ledgers" / "self.beancount"
    before = path.read_text()
    with pytest.raises(ledger_io.LedgerError):
        ledger_io.append(path, ['2026-09-01 * "x"\n  Assets:Nowhere  1.00 INR\n  Assets:Self:Cash'])
    assert path.read_text() == before


def test_amfi_parser_handles_old_and_new_layouts():
    old = "Scheme Code;ISIN;ISIN;Scheme Name;Net Asset Value;Date\n122639;A;B;PPFAS;76.42;05-Sep-2026"
    new = "122639;INF879O01027;-;Parag Parikh Flexi Cap Fund;Direct Plan;Growth;88.7620;05-Oct-2026\n" \
          "999;X;-;Dead fund;Direct;Growth;N.A.;05-Oct-2026"
    assert prices.parse_amfi(old)["122639"] == (date(2026, 9, 5), D("76.42"))
    assert prices.parse_amfi(new) == {"122639": (date(2026, 10, 5), D("88.7620"))}
