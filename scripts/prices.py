"""Market prices for held commodities -> ledgers/prices.beancount.

    prices.py [--dry-run]

Reads `commodity` directives from the ledger and fetches a price for each one that
names a source in its metadata:
    amfi: "<scheme code>"   latest NAV from AMFI's NAVAll.txt (one download for all funds)
    yahoo: "<ticker>"       latest daily close from yfinance (e.g. "RELIANCE.NS")
Appends `price` directives dated with the NAV / close date, skipping any
(commodity, date) already present, so it is safe to run daily.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal

from beancount.core import data

import ledger_io
from common import PFError, ledger_dir, log, run_cli, setup_logging

AMFI_URLS = ["https://portal.amfiindia.com/spages/NAVAll.txt",
             "https://www.amfiindia.com/spages/NAVAll.txt"]


def fetch_amfi() -> dict[str, tuple[date, Decimal]]:
    """scheme code -> (nav date, nav)."""
    import requests
    last = None
    for url in AMFI_URLS:
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            return parse_amfi(resp.text)
        except Exception as ex:          # try the next mirror
            last = ex
            log.warning("AMFI %s failed: %s", url, ex)
    raise PFError(f"could not download AMFI NAVs: {last}")


def parse_amfi(text: str) -> dict[str, tuple[date, Decimal]]:
    # Code;ISIN;ISIN;Scheme Name[;Plan;Option];NAV;Date - AMFI added the Plan/Option columns
    # in 2026, so read NAV and date from the END of the row to work with both layouts.
    out = {}
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue
        try:
            nav = Decimal(parts[-2].strip())
            when = datetime.strptime(parts[-1].strip(), "%d-%b-%Y").date()
        except Exception:
            continue                      # "N.A." NAVs and malformed rows
        out[parts[0].strip()] = (when, nav)
    return out


def fetch_yahoo(ticker: str) -> tuple[date, Decimal] | None:
    import yfinance as yf
    hist = yf.Ticker(ticker).history(period="7d", auto_adjust=False)
    if hist.empty:
        return None
    ts = hist.index[-1]
    return ts.date(), Decimal(str(round(float(hist["Close"].iloc[-1]), 4)))


def run(dry_run: bool) -> int:
    entries, _, _ = ledger_io.load()
    existing = {(e.currency, e.date) for e in entries if isinstance(e, data.Price)}
    commodities = [e for e in entries if isinstance(e, data.Commodity)]
    lines, failures = [], 0
    amfi = None
    for c in commodities:
        meta = c.meta or {}
        quote = None
        if "amfi" in meta:
            amfi = amfi if amfi is not None else fetch_amfi()
            quote = amfi.get(str(meta["amfi"]))
            if quote is None:
                print(f"FAIL  {c.currency}: AMFI scheme {meta['amfi']} not in NAVAll.txt", file=sys.stderr)
                failures += 1
                continue
        elif "yahoo" in meta:
            try:
                quote = fetch_yahoo(str(meta["yahoo"]))
            except Exception as ex:
                quote = None
                log.warning("yfinance %s: %s", meta["yahoo"], ex)
            if quote is None:
                print(f"FAIL  {c.currency}: no recent close for {meta['yahoo']}", file=sys.stderr)
                failures += 1
                continue
        else:
            continue
        when, px = quote
        if (c.currency, when) in existing:
            print(f"have  {c.currency} {when} {px}")
            continue
        lines.append(f"{when.isoformat()} price {c.currency:<14} {px} INR")
        print(f"new   {c.currency} {when} {px} INR")
    if lines and not dry_run:
        ledger_io.append(ledger_dir() / "prices.beancount", lines, header=f"prices {date.today()}")
    print(f"{len(lines)} new price(s)" + (" (dry run)" if dry_run else ""))
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    setup_logging(args.verbose)
    return run(args.dry_run)


if __name__ == "__main__":
    run_cli(main)
