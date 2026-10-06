"""Card milestones, reward points and Amazon Pay gift cards.

    milestones.py status [--date D]            # fee-waiver progress + reward point balances
    milestones.py earn   --card ID --points N [--date D] [--note TEXT]
    milestones.py redeem --card ID --points N --value INR [--to ACCOUNT] [--date D]
    milestones.py amazon-gc --amount N --paid-with ACCOUNT [--cashback N] [--pending]
                            [--wallet ACCOUNT] [--date D]

Card year: runs from the card's `anniversary` (MM-DD in config) to the day before the
next one. Eligible spend = purchases on the card whose other leg matches the card's
`milestone_counts` regex (Expenses by default; add wallets for cards that count gift
card loads), minus refunds, excluding anything whose payee/narration matches
`milestone_exclude` or that carries the tag #no-milestone.

Reward points are their own commodities (HDFC_RP, SBI_CASHBACK, ...), so a balance is
never confused with rupees. Redeeming converts them at the value actually received
(`@@` total price), which keeps the books balanced in both currencies.

Every write command accepts --dry-run.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

import ledger_io
from common import (PFError, entity_ledger, entity_of_account, load_config, money, parse_date,
                    run_cli, setup_logging, sources, to_decimal)
from ledger_io import Posting, Txn


# --------------------------------------------------------------------------- card year

def _anniv(year: int, mmdd: str) -> date:
    m, d = (int(x) for x in mmdd.split("-"))
    try:
        return date(year, m, d)
    except ValueError:          # 02-29 in a non-leap year
        return date(year, m, 28)


def card_year(anniversary: str, on: date) -> tuple[date, date]:
    """(first day, last day) of the card year containing `on`."""
    start = _anniv(on.year, anniversary)
    if start > on:
        start = _anniv(on.year - 1, anniversary)
    end = _anniv(start.year + 1, anniversary) - timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- spend

def _excluded(txn, patterns: list[str]) -> bool:
    text = f"{txn.payee or ''} {txn.narration or ''}"
    return "no-milestone" in (txn.tags or set()) or any(re.search(p, text) for p in patterns)


def eligible_purchases(entries, card: dict, start: date, end: date):
    """Yield (txn, amount) for spend that counts toward this card's milestones."""
    counts = re.compile(card.get("milestone_counts", "^Expenses:"))
    excludes = card.get("milestone_exclude", [])
    for txn in ledger_io.transactions(entries):
        if not (start <= txn.date <= end) or _excluded(txn, excludes):
            continue
        legs = [p for p in txn.postings if p.account == card["account"]
                and p.units and p.units.currency == "INR"]
        if not legs:
            continue
        if not any(counts.search(p.account) for p in txn.postings if p.account != card["account"]):
            continue
        yield txn, -sum((p.units.number for p in legs), Decimal(0))


@dataclass
class Progress:
    card: str
    start: date
    end: date
    spent: Decimal
    threshold: Decimal
    days_total: int
    days_left: int

    @property
    def remaining(self) -> Decimal:
        return max(self.threshold - self.spent, Decimal(0))

    @property
    def pct(self) -> Decimal:
        return (self.spent / self.threshold * 100).quantize(Decimal("0.1")) if self.threshold else Decimal(0)

    @property
    def needed_per_day(self) -> Decimal:
        return money(self.remaining / self.days_left) if self.days_left else self.remaining

    @property
    def projected(self) -> Decimal:
        elapsed = self.days_total - self.days_left
        return money(self.spent / elapsed * self.days_total) if elapsed > 0 else self.spent

    @property
    def on_track(self) -> bool:
        return self.spent >= self.threshold or self.projected >= self.threshold


def milestone_progress(entries, cfg: dict, on: date) -> list[Progress]:
    out = []
    for sid, card in sources(cfg, kind="card").items():
        if not card.get("anniversary") or not card.get("fee_waiver_spend"):
            continue
        start, end = card_year(card["anniversary"], on)
        spent = money(sum((amt for _, amt in eligible_purchases(entries, card, start, end)), Decimal(0)))
        out.append(Progress(sid, start, end, spent, to_decimal(card["fee_waiver_spend"]),
                            (end - start).days + 1, max((end - on).days + 1, 0)))
    return out


@dataclass
class Points:
    card: str
    commodity: str
    balance: Decimal
    statement_points: Decimal     # reward_points printed on imported statements, this card year
    estimated_points: Decimal     # from reward_points per reward_per_inr, this card year


def points_status(entries, cfg: dict, on: date) -> list[Points]:
    out = []
    for sid, card in sources(cfg, kind="card").items():
        com = card.get("reward_commodity")
        if not com:
            continue
        balance = Decimal(0)
        for txn in ledger_io.transactions(entries):
            if txn.date > on:
                continue
            for p in txn.postings:
                if p.account == card.get("reward_account") and p.units and p.units.currency == com:
                    balance += p.units.number
        stmt = est = Decimal(0)
        if card.get("anniversary"):
            start, end = card_year(card["anniversary"], on)
            rate, per = Decimal(card.get("reward_points", 0)), Decimal(card.get("reward_per_inr", 0) or 0)
            for txn, amt in eligible_purchases(entries, card, start, min(end, on)):
                if per and amt > 0:
                    est += (amt / per).to_integral_value(ROUND_FLOOR) * rate
                rp = txn.meta.get("reward_points")
                if rp is not None:
                    stmt += Decimal(str(rp))
        out.append(Points(sid, com, balance, stmt, est))
    return out


# --------------------------------------------------------------------------- entries

def earn_txn(card: dict, points: Decimal, when: date, note: str | None) -> Txn:
    com = card["reward_commodity"]
    return Txn(when, card["id"], note or f"Reward points credited",
               [Posting(card["reward_account"], points, com),
                Posting(card["reward_income"], -points, com)], tags=["rewards"])


def redeem_txn(card: dict, points: Decimal, value: Decimal, to_account: str, when: date) -> Txn:
    com = card["reward_commodity"]
    return Txn(when, card["id"], f"Redeemed {points} {com} for ₹{money(value)}",
               [Posting(card["reward_account"], -points, com, price=f"@@ {money(value)} INR"),
                Posting(to_account, value)], tags=["rewards"])


def amazon_gc_txns(*, amount: Decimal, paid_with: str, wallet: str, cashback: Decimal,
                   pending: bool, when: date, entity_label_: str) -> list[Txn]:
    txns = [Txn(when, "Amazon", "Amazon Pay gift card load",
                [Posting(wallet, amount), Posting(paid_with, -amount)], tags=["amazon-gc"])]
    if cashback:
        to = f"Assets:{entity_label_}:Receivable:Cashback" if pending else wallet
        txns.append(Txn(when, "Amazon", "Cashback on gift card load" + (" (pending)" if pending else ""),
                        [Posting(to, cashback), Posting(f"Income:{entity_label_}:Cashback", -cashback)],
                        tags=["amazon-gc", "cashback"]))
    return txns


# --------------------------------------------------------------------------- CLI

def _write(entity: str, txns: list[Txn], cfg: dict, dry_run: bool, header: str) -> None:
    for t in txns:
        t.check_balance()
    chunks = [t.render() for t in txns]
    if dry_run:
        print("\n\n".join(chunks))
        print("\ndry run - nothing written")
    else:
        ledger_io.append(entity_ledger(entity, cfg), chunks, header=header)


def _card(cfg: dict, cid: str, need_rewards: bool = False) -> dict:
    cards = sources(cfg, kind="card")
    if cid not in cards:
        raise PFError(f"unknown card {cid!r}; known: {', '.join(cards)}")
    card = cards[cid]
    if need_rewards and not all(card.get(k) for k in ("reward_commodity", "reward_account", "reward_income")):
        raise PFError(f"card {cid} has no reward_commodity/reward_account/reward_income in config")
    return card


def cmd_status(args) -> int:
    cfg = load_config()
    on = parse_date(args.date) if args.date else date.today()
    entries, _, _ = ledger_io.load()
    print(f"Fee-waiver progress on {on}")
    for p in milestone_progress(entries, cfg, on):
        state = "DONE" if p.spent >= p.threshold else ("on track" if p.on_track else "AT RISK")
        print(f"  {p.card:<18} ₹{p.spent:>12,} of ₹{p.threshold:,} ({p.pct}%)  "
              f"left ₹{p.remaining:,} in {p.days_left}d = ₹{p.needed_per_day:,}/day  "
              f"[{state}; year {p.start}..{p.end}, projected ₹{p.projected:,}]")
    print("Reward points")
    for r in points_status(entries, cfg, on):
        print(f"  {r.card:<18} balance {r.balance} {r.commodity}; this card year: "
              f"{r.statement_points} on statements, ~{r.estimated_points} expected from spend")
    return 0


def cmd_earn(args) -> int:
    cfg = load_config()
    card = _card(cfg, args.card, need_rewards=True)
    when = parse_date(args.date) if args.date else date.today()
    _write(card["entity"], [earn_txn(card, to_decimal(args.points), when, args.note)], cfg,
           args.dry_run, f"rewards {args.card}")
    return 0


def cmd_redeem(args) -> int:
    cfg = load_config()
    card = _card(cfg, args.card, need_rewards=True)
    when = parse_date(args.date) if args.date else date.today()
    _write(card["entity"], [redeem_txn(card, to_decimal(args.points), to_decimal(args.value),
                                       args.to or card["account"], when)], cfg, args.dry_run,
           f"redeem {args.card}")
    return 0


def cmd_amazon_gc(args) -> int:
    cfg = load_config()
    entity = entity_of_account(args.wallet, cfg)
    if not entity:
        raise PFError(f"cannot tell whose wallet {args.wallet} is")
    when = parse_date(args.date) if args.date else date.today()
    txns = amazon_gc_txns(amount=to_decimal(args.amount), paid_with=args.paid_with, wallet=args.wallet,
                          cashback=to_decimal(args.cashback or 0), pending=args.pending, when=when,
                          entity_label_=cfg["entities"][entity]["label"])
    _write(entity, txns, cfg, args.dry_run, "amazon pay gift card")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--date")
    e = sub.add_parser("earn")
    e.add_argument("--card", required=True)
    e.add_argument("--points", required=True)
    e.add_argument("--note")
    r = sub.add_parser("redeem")
    r.add_argument("--card", required=True)
    r.add_argument("--points", required=True)
    r.add_argument("--value", required=True, help="INR received for the points")
    r.add_argument("--to", help="account credited (default: the card itself)")
    g = sub.add_parser("amazon-gc")
    g.add_argument("--amount", required=True)
    g.add_argument("--paid-with", required=True)
    g.add_argument("--wallet", default="Assets:Self:Wallet:AmazonPay")
    g.add_argument("--cashback")
    g.add_argument("--pending", action="store_true", help="cashback promised, not yet credited")
    for sp in (e, r, g):
        sp.add_argument("--date")
        sp.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    setup_logging(args.verbose)
    return {"status": cmd_status, "earn": cmd_earn, "redeem": cmd_redeem,
            "amazon-gc": cmd_amazon_gc}[args.cmd](args)


if __name__ == "__main__":
    run_cli(main)
