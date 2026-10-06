"""Build a self-contained demo copy of the system with synthetic data.

    python scripts/demo.py [DIR]          # default: ./demo
    export PF_ROOT=demo PF_SECRET_SELF_NAME="Arjun Kumar" PF_SECRET_SELF_DOB=1990-05-12 \
           PF_SECRET_MOM_NAME="Lakshmi Devi" PF_SECRET_MOM_DOB=1962-08-03 \
           PF_SECRET_MOM_CUSTOMER_ID=55512345
    (the script prints this export line for its own fake identities)

Creates DIR/{config,ledgers,data} from the real templates, then adds made-up September
2026 statements (encrypted exactly the way the issuers do it), grocery receipts with
hand-written extractions, and a few manual entries. Nothing here is real data; the
tests use the same builder so the demo and the tests can never drift apart.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

REPO = Path(__file__).resolve().parent.parent

IDENTITY = {"PF_SECRET_SELF_NAME": "Arjun Kumar", "PF_SECRET_SELF_DOB": "1990-05-12",
            "PF_SECRET_MOM_NAME": "Lakshmi Devi", "PF_SECRET_MOM_DOB": "1962-08-03",
            "PF_SECRET_MOM_CUSTOMER_ID": "55512345"}

# --------------------------------------------------------------------------- statements
# (filename, password, text lines) - layouts mirror the issuers' text extraction.

STATEMENTS = [
    ("hdfc-regalia-credit-sep26.pdf", "ARJU1205", [
        "HDFC Bank Credit Cards  -  Regalia",
        "Card No: XXXX XXXX XXXX 4321    Statement Date 30/09/2026",
        "Payment Due Date 05/10/2026    Total Amount Due 89,084.50",
        "Domestic Transactions",
        "Date Transaction Description Reward Points Amount (in Rs.)",
        "02/09/2026 SWIGGY BANGALORE 22 850.00",
        "05/09/2026 PAYMENT RECEIVED - THANK YOU 45,000.00 Cr",
        "08/09/2026 AMAZON PAY GIFT CARD 133 5,000.00",
        "12/09/2026 HPCL FUEL STATION 0 2,000.00",
        "14/09/2026 DMART AVENUE SUPERMARTS 32 1,234.50",
        "18/09/2026 CROMA ELECTRONICS 2266 85,000.00",
        "20/09/2026 SRI LAKSHMI TRADERS 17 640.00",
    ]),
    ("sbicard-statement-sep26.pdf", "120519908765", [
        "SBI Card  CASHBACK SBI Card",
        "Credit Card Number XXXX XXXX XXXX 8765",
        "Statement Date 30 Sep 26   Payment Due Date 20 Oct 26",
        "Date Transaction Details Amount ( ` )",
        "03 Sep 26 AMAZON PAY INDIA 2,499.00 D",
        "10 Sep 26 ZEPTO MARKETPLACE 780.00 D",
        "18 Sep 26 REFUND AMAZON SELLER SERVICES 499.00 C",
        "20 Sep 26 PAYMENT RECEIVED BBPS 3,000.00 C",
    ]),
    ("icici-amazonpay-sep26.pdf", "arju1205", [
        "ICICI Bank Amazon Pay Credit Card",
        "Card Number 4375 XXXX XXXX 1111",
        "Date SerNo. Transaction Details Reward Points Amount (in`)",
        "07/09/2026 10234567891 AMAZON RETAIL INDIA 99 1,999.00",
        "15/09/2026 10234567892 UBER INDIA SYSTEMS 3 340.00",
        "22/09/2026 10234567893 BESCOM ELECTRICITY BILL 0 1,820.50",
    ]),
    ("hdfc-acct-statement-sep26.pdf", "ARJU1205", [
        "HDFC BANK LTD   Statement of account   A/C 50100012345678",
        "Opening Balance 2,00,000.00",
        "Date Narration Chq./Ref.No. Value Dt Withdrawal Amt. Deposit Amt. Closing Balance",
        "01/09/26 SALARY ACME CORP SEP 01/09/26 1,50,000.00 3,50,000.00",
        "04/09/26 NEFT DR-HDFC CC PAYMENT 0000123456 04/09/26 45,000.00 3,05,000.00",
        "10/09/26 IMPS-TO LAKSHMI DEVI 626012345678 10/09/26 50,000.00 2,55,000.00",
        "15/09/26 ATM WDL KORAMANGALA 15/09/26 30,000.00 2,25,000.00",
        "25/09/26 INTEREST CREDIT 25/09/26 812.00 2,25,812.00",
    ]),
    ("mom-sb-statement-sep26.pdf", "55512345", [
        "Savings Account Statement - Lakshmi Devi",
        "Opening Balance 40,000.00",
        "Date Narration Ref Value Date Debit Credit Balance",
        "11/09/26 IMPS CR ARJUN KUMAR 626012345678 11/09/26 50,000.00 90,000.00",
        "18/09/26 UPI-KPN FRESH 418812345678 18/09/26 1,150.00 88,850.00",
        "28/09/26 FD INTEREST 28/09/26 2,400.00 91,250.00",
    ]),
]


def make_pdf(lines: list[str], password: str | None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Courier", 9)
    y = 800
    for line in lines:
        c.drawString(40, y, line)
        y -= 16
    c.save()
    if password is None:
        return buf.getvalue()
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(buf.getvalue())) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user=password, owner=password + "-owner", R=6))
    return out.getvalue()


# --------------------------------------------------------------------------- receipts

RECEIPTS = [
    ("smartpoint-2026-03-10.png", {"store": "SMART POINT", "date": "2026-03-10", "bill_no": "SP-88121",
        "items": [{"name": "TOMATO", "quantity": 2, "unit_price": 30, "total": 60},
                  {"name": "BANANA ROBUSTA", "quantity": 1.2, "unit_price": 50, "total": 60}],
        "total": 120}),
    ("kpn-2026-06-12.png", {"store": "KPN FRESH", "date": "2026-06-12", "bill_no": "K-20931",
        "items": [{"name": "TOMATO", "quantity": 1.5, "unit_price": 42, "total": 63},
                  {"name": "ONION", "quantity": 2, "unit_price": 35, "total": 70}],
        "total": 133}),
    ("dmart-2026-09-14.png", {"store": "D MART", "date": "2026-09-14", "bill_no": "DM-4471209",
        "items": [{"name": "LION DATES 500G", "quantity": 1, "unit_price": 180, "total": 180},
                  {"name": "TOMATO", "quantity": 1, "unit_price": 60, "total": 60},
                  {"name": "AASHIRVAAD ATTA 5KG", "quantity": 1, "unit_price": 285, "total": 285},
                  {"name": "AMUL BUTTER 500G", "quantity": 1, "unit_price": 280, "total": 280},
                  {"name": "TATA SAMPANN TOOR DAL 1KG", "quantity": 1, "unit_price": 189, "total": 189},
                  {"name": "MYSTERY SNACK MIX 200G", "quantity": 2, "unit_price": 120.25, "total": 240.5}],
        "total": 1234.5}),
    ("csd-2026-09-27.png", {"store": "UNIT RUN CANTEEN CSD", "date": "2026-09-27", "bill_no": "",
        "items": [{"name": "TOMATO", "quantity": 1, "unit_price": 62, "total": 62},
                  {"name": "NANDINI GHEE 1L", "quantity": 1, "unit_price": 610, "total": 610}],
        "total": 672}),
]
# Which account paid each receipt (the DMart bill is on the HDFC card statement too).
RECEIPT_PAID_WITH = {"dmart-2026-09-14.png": "Liabilities:Self:CC:HDFC-Regalia"}


def make_receipt_image(extraction: dict) -> bytes:
    img = Image.new("RGB", (420, 120 + 22 * len(extraction["items"])), "white")
    d = ImageDraw.Draw(img)
    y = 10
    for text in [extraction["store"], f"Date {extraction['date']}  Bill {extraction['bill_no']}", "-" * 50]:
        d.text((10, y), text, fill="black")
        y += 22
    for it in extraction["items"]:
        d.text((10, y), f"{it['name']:<28} {it['quantity']:>5} {it['total']:>9.2f}", fill="black")
        y += 22
    d.text((10, y + 6), f"NET AMOUNT {extraction['total']:>30.2f}", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- manual entries

SELF_EXTRA = """
2026-04-01 * "Opening balances"
  Assets:Self:HDFC                                 320000.00 INR
  Equity:Self:Opening-Balances

2026-06-01 * "Opening balances"
  Assets:Self:Wallet:AmazonPay                      12000.00 INR
  Equity:Self:Opening-Balances

2026-09-01 * "Opening balances"
  Liabilities:Self:CC:HDFC-Regalia                 -45000.00 INR
  Equity:Self:Opening-Balances

2026-04-02 * "Reliance Digital" "Laptop on Regalia (earlier this card year)"
  Liabilities:Self:CC:HDFC-Regalia                -120000.00 INR
  Expenses:Self:Shopping

2026-05-05 * "HDFC" "Card bill paid"
  Liabilities:Self:CC:HDFC-Regalia                 120000.00 INR
  Assets:Self:HDFC                                -120000.00 INR

2026-09-20 * "Amazon" "Household order paid from Amazon Pay balance"
  Expenses:Self:Shopping                              3000.00 INR
  Assets:Self:Wallet:AmazonPay

2026-01-15 custom "policy" "Health" "Star Health family floater"
  due: 2026-10-10
  premium: 24500

2026-03-02 custom "policy" "Term" "HDFC Life Click 2 Protect"
  due: 2027-03-02

2026-06-20 custom "policy" "Vehicle" "Car insurance KA01 AB 1234"
  due: 2026-11-02
"""

MOM_EXTRA = """
2026-06-01 * "Opening balances"
  Assets:Mom:Savings                                40000.00 INR
  Equity:Mom:Opening-Balances
"""


def build(target: Path) -> Path:
    target = Path(target)
    if target.exists():
        shutil.rmtree(target)
    (target / "config").mkdir(parents=True)
    shutil.copy(REPO / "config" / "config.example.toml", target / "config" / "config.toml")
    shutil.copy(REPO / "config" / "canonical_items.toml", target / "config" / "canonical_items.toml")
    shutil.copytree(REPO / "ledgers", target / "ledgers")
    cfg = (target / "config" / "config.toml").read_text()
    for sid, last4 in [("hdfc-regalia", "4321"), ("sbi-cashback", "8765"), ("icici-amazon", "1111")]:
        block = cfg.index(f"[sources.{sid}]")
        at = cfg.index('last4 = "0000"', block)
        cfg = cfg[:at] + f'last4 = "{last4}"' + cfg[at + len('last4 = "0000"'):]
    (target / "config" / "config.toml").write_text(cfg)

    with open(target / "ledgers" / "self.beancount", "a") as fh:
        fh.write(SELF_EXTRA)
    with open(target / "ledgers" / "mom.beancount", "a") as fh:
        fh.write(MOM_EXTRA)

    stmts = target / "data" / "raw_statements"
    stmts.mkdir(parents=True)
    for name, pw, lines in STATEMENTS:
        (stmts / name).write_bytes(make_pdf(lines, pw))

    rcpts = target / "data" / "raw_receipts"
    rcpts.mkdir(parents=True)
    for name, extraction in RECEIPTS:
        (rcpts / name).write_bytes(make_receipt_image(extraction))
        (rcpts / (Path(name).stem + ".json")).write_text(json.dumps(extraction, indent=2))
    return target


def env_line() -> str:
    return "export " + " ".join(f'{k}="{v}"' for k, v in IDENTITY.items())


if __name__ == "__main__":
    out = build(Path(sys.argv[1] if len(sys.argv) > 1 else "demo").resolve())
    print(f"demo built in {out}")
    print(f"export PF_ROOT={out}")
    print(env_line())
