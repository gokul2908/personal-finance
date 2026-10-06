"""Opportunity cost and price analytics.

    analytics.py wallet-drag [--days 90 | --from D --to D] [--rate 0.07]
    analytics.py price-timeline ITEM [--store NAME]
    analytics.py inflation [--date D]
    analytics.py spend [--date D]

Idle wallet drag: money parked in a wallet (Amazon Pay, Paytm, ...) earns nothing.
For every day in the window the end-of-day balance is priced at `idle_rate` (7% p.a.
by default, roughly what a liquid fund or sweep FD would pay):
    lost interest = sum over days of  max(balance, 0) * rate / 365
Accounts come from [analytics].idle_accounts (regexes).

Price timeline: weighted average price per kg / l / piece of a canonical item per
quarter (total spent / quantity bought, so a 5 kg bag and a 500 g pack compare
fairly), with the change from the previous quarter that has purchases (a quarter
with no purchases is skipped, not treated as zero).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import ledger_io
import sidecar
from common import PFError, load_config, money, parse_date, run_cli, setup_logging, to_decimal


# --------------------------------------------------------------------------- idle wallet drag

@dataclass
class Drag:
    account: str
    days: int
    avg_balance: Decimal
    max_balance: Decimal
    current_balance: Decimal
    lost_interest: Decimal
    rate: Decimal

    @property
    def yearly_cost_at_current(self) -> Decimal:
        return money(max(self.current_balance, Decimal(0)) * self.rate)


def wallet_drag(entries, cfg: dict, start: date, end: date, rate: Decimal | None = None) -> list[Drag]:
    acfg = cfg.get("analytics", {})
    rate = Decimal(str(rate if rate is not None else acfg.get("idle_rate", 0.07)))
    patterns = [re.compile(p) for p in acfg.get("idle_accounts", [r"^Assets:[^:]+:Wallet:"])]
    deltas: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for txn in ledger_io.transactions(entries):
        if txn.date > end:
            continue
        for p in txn.postings:
            if p.units and p.units.currency == "INR" and any(rx.search(p.account) for rx in patterns):
                deltas[p.account][txn.date] += p.units.number
    out = []
    days = (end - start).days + 1
    for account, by_day in sorted(deltas.items()):
        bal = sum((v for d, v in by_day.items() if d < start), Decimal(0))
        total = peak = lost = Decimal(0)
        for i in range(days):
            day = start + timedelta(days=i)
            bal += by_day.get(day, Decimal(0))
            positive = max(bal, Decimal(0))
            total += positive
            peak = max(peak, positive)
            lost += positive * rate / 365
        out.append(Drag(account, days, money(total / days), money(peak), money(bal), money(lost), rate))
    return out


# --------------------------------------------------------------------------- price timeline

PRICE_TIMELINE_SQL = """
WITH q AS (
    SELECT strftime('%Y', date) || '-Q' || ((CAST(strftime('%m', date) AS INTEGER) + 2) / 3) AS quarter,
           store, unit, quantity, total_price, price_per_unit
    FROM receipt_items
    WHERE canonical_name = :item COLLATE NOCASE
      AND quantity > 0
      AND (:store IS NULL OR store = :store)
), agg AS (
    SELECT quarter, unit,
           ROUND(SUM(total_price) / SUM(quantity), 2) AS avg_price,
           ROUND(MIN(price_per_unit), 2)              AS min_price,
           ROUND(MAX(price_per_unit), 2)              AS max_price,
           COUNT(*)                                   AS purchases,
           ROUND(SUM(quantity), 3)                    AS quantity_bought,
           GROUP_CONCAT(DISTINCT store)               AS stores
    FROM q
    GROUP BY quarter, unit
)
SELECT agg.*,
       ROUND(100.0 * (avg_price - LAG(avg_price) OVER w) / LAG(avg_price) OVER w, 1) AS change_pct
FROM agg
WINDOW w AS (PARTITION BY unit ORDER BY quarter)
ORDER BY unit, quarter
"""


def price_timeline(item: str, conn: sqlite3.Connection | None = None,
                   store: str | None = None) -> list[dict]:
    """Quarterly price per unit for one canonical item, e.g. price_timeline("Tomato")."""
    conn = conn or sidecar.connect()
    return [dict(r) for r in conn.execute(PRICE_TIMELINE_SQL, {"item": item, "store": store})]


INFLATION_SQL = """
SELECT canonical_name, unit,
       SUM(CASE WHEN date >  :split THEN total_price END) / SUM(CASE WHEN date >  :split THEN quantity END) AS recent,
       SUM(CASE WHEN date <= :split THEN total_price END) / SUM(CASE WHEN date <= :split THEN quantity END) AS baseline,
       SUM(CASE WHEN date >  :split THEN 1 ELSE 0 END) AS recent_n,
       SUM(CASE WHEN date <= :split THEN 1 ELSE 0 END) AS baseline_n
FROM receipt_items
WHERE date > :start AND date <= :end AND quantity > 0
GROUP BY canonical_name, unit
HAVING recent_n > 0 AND baseline_n > 0
"""


def grocery_inflation(conn: sqlite3.Connection | None, cfg: dict, on: date) -> list[dict]:
    """Items whose recent price per unit beat their baseline by >= inflation_threshold."""
    conn = conn or sidecar.connect()
    acfg = cfg.get("analytics", {})
    recent = int(acfg.get("inflation_recent_days", 28))
    baseline = int(acfg.get("inflation_baseline_days", 112))
    threshold = float(acfg.get("inflation_threshold", 0.15))
    split = on - timedelta(days=recent)
    params = {"split": split.isoformat(), "end": on.isoformat(),
              "start": (split - timedelta(days=baseline)).isoformat()}
    out = []
    for r in conn.execute(INFLATION_SQL, params):
        change = r["recent"] / r["baseline"] - 1 if r["baseline"] else 0
        if change >= threshold:
            out.append({"item": r["canonical_name"], "unit": r["unit"],
                        "recent": round(r["recent"], 2), "baseline": round(r["baseline"], 2),
                        "change_pct": round(change * 100, 1),
                        "recent_purchases": r["recent_n"], "baseline_purchases": r["baseline_n"]})
    return sorted(out, key=lambda x: -x["change_pct"])


# --------------------------------------------------------------------------- spend by category

def category_spend(entries, start: date, end: date, depth: int = 3) -> dict[str, Decimal]:
    """INR spent per Expenses account (truncated to `depth` segments) in [start, end]."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for txn in ledger_io.transactions(entries):
        if start <= txn.date <= end:
            for p in txn.postings:
                if p.account.startswith("Expenses:") and p.units and p.units.currency == "INR":
                    out[":".join(p.account.split(":")[:depth])] += p.units.number
    return dict(out)


def weekly_spend_shift(entries, on: date, weeks: int = 4) -> list[dict]:
    """This week's spend per category against the average of the previous `weeks` weeks."""
    this = category_spend(entries, on - timedelta(days=6), on)
    prev = category_spend(entries, on - timedelta(days=7 * (weeks + 1) - 1), on - timedelta(days=7))
    rows = []
    for cat in sorted(set(this) | set(prev)):
        avg = money(prev.get(cat, Decimal(0)) / weeks)
        now = money(this.get(cat, Decimal(0)))
        rows.append({"category": cat, "this_week": now, "weekly_avg": avg, "delta": now - avg})
    return sorted(rows, key=lambda r: -abs(r["delta"]))


# --------------------------------------------------------------------------- CLI

def _window(args) -> tuple[date, date]:
    end = parse_date(args.to) if getattr(args, "to", None) else date.today()
    start = parse_date(args.from_) if getattr(args, "from_", None) else end - timedelta(days=args.days - 1)
    return start, end


def cmd_wallet_drag(args) -> int:
    cfg = load_config()
    if args.days < 1:
        raise PFError("--days must be at least 1")
    start, end = _window(args)
    if start > end:
        raise PFError(f"--from {start} is after --to {end}")
    entries, _, _ = ledger_io.load()
    rows = wallet_drag(entries, cfg, start, end, to_decimal(args.rate) if args.rate else None)
    if not rows:
        print("no wallet activity found")
    for d in rows:
        print(f"{d.account:<34} {start}..{end}: avg ₹{d.avg_balance:,} (peak ₹{d.max_balance:,}) "
              f"lost ₹{d.lost_interest:,} at {d.rate:.0%}; now ₹{d.current_balance:,} "
              f"= ₹{d.yearly_cost_at_current:,}/yr if it stays idle")
    return 0


def cmd_price_timeline(args) -> int:
    rows = price_timeline(args.item, store=args.store)
    if not rows:
        print(f"no purchases of {args.item!r} in sidecar.db")
        return 1
    print(f"{'quarter':<9} {'unit':<4} {'avg/unit':>9} {'min':>8} {'max':>8} {'n':>3} {'qty':>8}  change  stores")
    for r in rows:
        chg = f"{r['change_pct']:+.1f}%" if r["change_pct"] is not None else "   -"
        print(f"{r['quarter']:<9} {r['unit']:<4} {r['avg_price']:>9.2f} {r['min_price']:>8.2f} "
              f"{r['max_price']:>8.2f} {r['purchases']:>3} {r['quantity_bought']:>8} {chg:>7}  {r['stores']}")
    return 0


def cmd_inflation(args) -> int:
    on = parse_date(args.date) if args.date else date.today()
    rows = grocery_inflation(None, load_config(), on)
    for r in rows:
        print(f"{r['item']:<16} ₹{r['baseline']}/{r['unit']} -> ₹{r['recent']}/{r['unit']} ({r['change_pct']:+}%)")
    if not rows:
        print("no item over the inflation threshold")
    return 0


def cmd_spend(args) -> int:
    on = parse_date(args.date) if args.date else date.today()
    entries, _, _ = ledger_io.load()
    for r in weekly_spend_shift(entries, on):
        print(f"{r['category']:<34} this week ₹{r['this_week']:>10,}  4-wk avg ₹{r['weekly_avg']:>10,}  "
              f"Δ ₹{r['delta']:+,}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wallet-drag")
    w.add_argument("--days", type=int, default=90)
    w.add_argument("--from", dest="from_")
    w.add_argument("--to")
    w.add_argument("--rate")
    t = sub.add_parser("price-timeline")
    t.add_argument("item")
    t.add_argument("--store")
    i = sub.add_parser("inflation")
    i.add_argument("--date")
    s = sub.add_parser("spend")
    s.add_argument("--date")
    args = p.parse_args()
    setup_logging(args.verbose)
    return {"wallet-drag": cmd_wallet_drag, "price-timeline": cmd_price_timeline,
            "inflation": cmd_inflation, "spend": cmd_spend}[args.cmd](args)


if __name__ == "__main__":
    run_cli(main)
