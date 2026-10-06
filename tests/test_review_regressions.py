"""One test per bug confirmed by the fresh-eyes review (numbers match that report)."""
import re
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

import ai_nudge
import analytics
import common
import demo
import google_sync
import importer
import ledger_io
import milestones
import receipt_engine
import sidecar
from test_system import STATEMENTS, _build, _line, import_all, ledger_ok

D = Decimal


def balance(entries, account):
    return sum((p.units.number for t in ledger_io.transactions(entries) for p in t.postings
                if p.account == account and p.units and p.units.currency == "INR"), D(0))


def stmt(pf, name, password, lines):
    path = STATEMENTS(pf) / name
    path.write_bytes(demo.make_pdf(lines, password))
    return path


# 1 - transfers across runs pair whichever side comes first
def test_1_bank_side_first_then_card_closes_clearing(pf):
    import_all([STATEMENTS(pf) / "hdfc-acct-statement-sep26.pdf"])
    import_all([STATEMENTS(pf) / "hdfc-regalia-credit-sep26.pdf"])
    entries = ledger_ok()
    # only the gift to Mom (her statement not imported) is still waiting
    assert [i["amount"] for i in importer.open_clearing_items(entries, 2)] == [D(50000)]
    # the ₹45,000 card payment is not an expense: croma + traders + ATM only
    assert balance(entries, "Expenses:Self:Uncategorized") == D("85640.00") + D(30000)


def test_1_self_to_mom_across_runs_is_a_tagged_gift(pf):
    import_all([STATEMENTS(pf) / "hdfc-acct-statement-sep26.pdf"])
    import_all([STATEMENTS(pf) / "mom-sb-statement-sep26.pdf"])
    entries = ledger_ok()
    # only the card payment (card statement not imported) is still waiting
    assert [i["amount"] for i in importer.open_clearing_items(entries, 2)] == [D(45000)]
    closing = [t for t in ledger_io.transactions(entries) if "gift" in t.tags]
    assert len(closing) == 1 and "56(2)(x)" in closing[0].meta["tax_note"]


# 2 - fallback accounts exist for everyone; multi-file writes are all-or-nothing
def test_2_every_rule_account_is_open_for_every_entity(pf):
    cfg = common.load_config()
    entries = ledger_ok()
    opened = {e.account for e in entries if type(e).__name__ == "Open"}
    for ent in cfg["entities"]:
        label = cfg["entities"][ent]["label"]
        wanted = {common.fill_entity(r["account"], ent, cfg) for r in cfg["rules"]}
        wanted |= {f"Expenses:{label}:Uncategorized", f"Income:{label}:Uncategorized",
                   f"Assets:{label}:Clearing"}
        assert wanted <= opened, (ent, wanted - opened)


def test_2_unmatched_mom_credit_imports(pf):
    out, _ = _build([_line("Assets:Mom:Savings", "mom", date(2026, 9, 5), "700", "CASH DEPOSIT")], pf)
    importer.write(out, common.load_config(), dry_run=False, header="t")
    assert balance(ledger_ok(), "Income:Mom:Uncategorized") == D(-700)


def test_2_write_is_atomic_across_files(pf):
    cfg = common.load_config()
    self_before = (pf / "ledgers" / "self.beancount").read_text()
    good = _build([_line("Assets:Self:HDFC", "self", date(2026, 9, 5), "-100", "SWIGGY")], pf)[0]
    bad = ledger_io.Note(date(2026, 9, 5), "Assets:Mom:DoesNotExist", "x")
    with pytest.raises(ledger_io.LedgerError):
        importer.write(good + [("mom", bad)], cfg, dry_run=False, header="t")
    assert (pf / "ledgers" / "self.beancount").read_text() == self_before


# 3 - shop payments and refunds are not transfers
def test_3_upi_purchase_and_refund_do_not_pair(pf):
    out, stats = _build([
        _line("Assets:Self:HDFC", "self", date(2026, 9, 5), "-499", "UPI-RAJU MEDICALS"),
        _line("Liabilities:Self:CC:SBI-Cashback", "self", date(2026, 9, 5), "499", "REFUND MYNTRA", "card"),
        _line("Assets:Self:HDFC", "self", date(2026, 9, 6), "-1150", "UPI-KPN FRESH"),
        _line("Assets:Mom:Savings", "mom", date(2026, 9, 6), "1150", "UPI CR REFUND SWIGGY")], pf)
    assert stats["transfers paired"] == 0
    assert not any("gift" in t.tags for _, t in out)


# 4 - Clearing is only for transfers, nets per account, and is closed only by a transfer
def test_4a_purchase_does_not_close_clearing(pf):
    import_all([STATEMENTS(pf) / "sbicard-statement-sep26.pdf"])           # BBPS +3000 -> Clearing
    out, stats = _build([_line("Liabilities:Self:CC:ICICI-AmazonPay", "self", date(2026, 9, 20),
                               "-3000", "CROMA", "card")], pf)
    assert stats["transfers closed against Clearing"] == 0


def test_4b_card_refund_is_a_flagged_negative_expense(pf):
    out, _ = _build([_line("Liabilities:Self:CC:HDFC-Regalia", "self", date(2026, 9, 20),
                           "85000", "CROMA RETURN", "card")], pf)
    (_, txn), = out
    assert txn.flag == "!" and any(p.account == "Expenses:Self:Uncategorized" and p.amount == D(-85000)
                                   for p in txn.postings)


def test_4c_open_clearing_items_nets_per_account(pf):
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        '2026-09-10 * "x"\n  Assets:Self:Clearing  -3000.00 INR\n  Assets:Self:HDFC',
        '2026-09-10 * "y"\n  Assets:Mom:Clearing  3000.00 INR\n  Assets:Mom:Savings'])
    assert len(importer.open_clearing_items(ledger_ok(), 2)) == 2


# 5 - the same statement twice in one run
def test_5_duplicate_pdf_in_one_run_counts_once(pf):
    src = STATEMENTS(pf) / "sbicard-statement-sep26.pdf"
    copy = STATEMENTS(pf) / "sbicard-statement-sep26 (1).pdf"
    copy.write_bytes(src.read_bytes())
    _, stats = import_all([src, copy])
    assert stats["duplicate within this run"] == 4
    assert balance(ledger_ok(), "Liabilities:Self:CC:SBI-Cashback") == D("220.00")


# 6 - receipt AFTER its statement
def test_6_receipt_after_statement_is_not_double_counted(pf):
    import_all([STATEMENTS(pf) / "hdfc-regalia-credit-sep26.pdf"])
    r = pf / "data" / "raw_receipts"
    res = receipt_engine.ingest(r / "dmart-2026-09-14.png", from_json=r / "dmart-2026-09-14.json",
                                paid_with="Liabilities:Self:CC:HDFC-Regalia")
    entries = ledger_ok()
    assert res.attached
    assert balance(entries, "Expenses:Self:Food:Groceries") == D("1234.50")
    txn = next(t for t in ledger_io.transactions(entries) if t.meta.get("receipt_id") == res.receipt_id)
    assert f"receipt-{res.receipt_id}" in txn.links


# 7 - a hand entry stands in for ONE statement line, across runs
def test_7_hand_entry_is_consumed_once_across_runs(pf):
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        '2026-09-30 * "Cafe" "by hand"\n  Expenses:Self:Food:Dining  500.00 INR\n'
        '  Liabilities:Self:CC:HDFC-Regalia'])
    sep = stmt(pf, "hdfc-regalia-credit-sep-b.pdf", "ARJU1205", ["29/09/2026 CAFE COFFEE DAY 13 500.00"])
    octo = stmt(pf, "hdfc-regalia-credit-oct-b.pdf", "ARJU1205", ["01/10/2026 THIRD WAVE COFFEE 13 500.00"])
    import_all([sep])
    _, stats = import_all([octo])
    assert stats["already recorded by hand/receipt"] == 0
    assert balance(ledger_ok(), "Liabilities:Self:CC:HDFC-Regalia") == D(-45000 - 1000)  # opening + 2 coffees


# 8 - date-led lines are never silently dropped
def test_8_skip_words_inside_a_transaction_line(pf):
    src = common.sources()["hdfc-regalia"]
    lines = importer.parse_lines(["10/09/2026 TOTAL ENERGIES FUEL 3 1,500.00",
                                  "11/09/2026 CREDIT LIMIT ENHANCEMENT FEE 0 499.00",
                                  "12/09/2026 MYNTRA REFUND -1,299.00",
                                  "Total Amount Due 3,000.00"], src)
    assert [l.amount for l in lines] == [D(-1500), D(-499), D(1299)]


def test_8_unparsed_line_breaks_the_balance_chain(pf, caplog):
    src = common.sources()["self-hdfc-savings"]
    lines = importer.parse_lines(["Opening Balance 1,000.00",
                                  "01/09/26 SOMETHING WEIRD 1,000 1,500.00",
                                  "02/09/26 NEFT CR-ACME REFUND 500.00 1,000.00"], src)
    assert len(lines) == 1 and lines[0].flags            # guessed, and says so
    assert "did not match" in caplog.text


# 9 - a number in the merchant name is not reward points
def test_9_points_must_be_plausible():
    src = {"id": "h", "parser": "hdfc_cc", "account": "L", "entity": "self", "kind": "card"}
    a, b, c = importer.parse_lines(["02/09/2026 MCDONALDS 1023 850.00",
                                    "05/09/2026 PAYMENT RECEIVED NEFT 12345 45,000.00 Cr",
                                    "06/09/2026 SWIGGY 22 850.00"], src)
    assert (a.description, a.points) == ("MCDONALDS 1023", None)
    assert b.points is None and "12345" in b.description
    assert c.points == 22


# 10 - a locked database fails before the ledger is touched
def test_10_locked_db_leaves_ledger_untouched(pf):
    r = pf / "data" / "raw_receipts"
    before = (pf / "ledgers" / "self.beancount").read_text()
    sidecar.connect().close()
    locker = sqlite3.connect(common.sidecar_path())
    locker.execute("BEGIN EXCLUSIVE")
    conn = sqlite3.connect(common.sidecar_path(), timeout=0.2)
    conn.row_factory = sqlite3.Row
    with pytest.raises(common.PFError, match="sidecar.db"):
        receipt_engine.ingest(r / "kpn-2026-06-12.png", from_json=r / "kpn-2026-06-12.json", conn=conn)
    locker.rollback()
    assert (pf / "ledgers" / "self.beancount").read_text() == before


# 11 - card fees are not spend
def test_11_fees_do_not_count_toward_waiver(pf):
    cfg = common.load_config()
    base = {p.card: p.spent for p in milestones.milestone_progress(ledger_ok(), cfg, date(2026, 9, 30))}
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        '2026-09-01 * "ANNUAL FEE"\n  Expenses:Self:Fees:Bank  2950.00 INR\n  Liabilities:Self:CC:HDFC-Regalia'])
    after = {p.card: p.spent for p in milestones.milestone_progress(ledger_ok(), cfg, date(2026, 9, 30))}
    assert after == base


# 12 - ambiguity counts partners at any day gap
def test_12_ambiguity_across_day_gaps(pf):
    out, _ = _build([_line("Assets:Self:HDFC", "self", date(2026, 9, 10), "-10000", "NEFT TO X"),
                     _line("Assets:Mom:Savings", "mom", date(2026, 9, 10), "10000", "NEFT CR"),
                     _line("Assets:Wife:Savings", "wife", date(2026, 9, 11), "10000", "NEFT CR")], pf)
    transfer = next(t for _, t in out if (t.payee or "").startswith("Transfer"))
    assert transfer.flag == "!"


# 13 - Clearing reported per account
def test_13_nudge_reports_each_clearing_account(pf):
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        '2026-09-10 * "x"\n  Assets:Self:Clearing  -3000.00 INR\n  Assets:Self:HDFC',
        '2026-09-10 * "y"\n  Assets:Mom:Clearing  3000.00 INR\n  Assets:Mom:Savings'])
    text = ai_nudge.render_summary(ai_nudge.build_summary(ledger_ok(), common.load_config(),
                                                          date(2026, 9, 30)), date(2026, 9, 30))
    assert "Assets:Self:Clearing" in text and "Assets:Mom:Clearing" in text


# 14 - an impossible #due- date warns instead of crashing
def test_14_invalid_due_tag_does_not_crash(pf, capsys):
    ledger_io.append(pf / "ledgers" / "self.beancount", [
        '2026-09-01 * "x" #due-2026-02-30\n  Expenses:Self:Utilities  1.00 INR\n  Assets:Self:Cash'])
    google_sync.plan_reminders(ledger_ok(), common.load_config(), date(2026, 9, 30), 60)
    assert "not a real date" in capsys.readouterr().err


# 15 - the Drive query is escaped
def test_15_drive_folder_name_is_escaped(tmp_path):
    seen = []

    class Call:
        def __init__(self, result): self.result = result
        def execute(self): return self.result

    class Files:
        def list(self, q, **kw):
            seen.append(q)
            return Call({"files": [{"id": "F"}]} if len(seen) == 1 else {"files": []})
        def create(self, **kw): return Call({"id": "N"})

    class Svc:
        def files(self): return Files()

    f = tmp_path / "a.beancount"
    f.write_text("x")
    google_sync.push_backup([("ledgers/a.beancount", f)], "Gokul's backup", svc=Svc())
    assert "name = 'Gokul\\'s backup'" in seen[0]


def test_wallet_drag_rejects_empty_window():
    class A: days = 0; from_ = None; to = None; rate = None
    with pytest.raises(common.PFError):
        analytics.cmd_wallet_drag(A)
