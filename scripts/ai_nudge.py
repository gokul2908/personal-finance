"""Weekly financial-discipline nudges from Gemma.

    ai_nudge.py [--date D] [--prompt-only] [--no-save]

Builds a structured, numbers-only summary of the week from the other modules and asks
Gemma for 3-5 nudges. The model writes the words; every number comes from here, and
the prompt forbids it from inventing any. The reply is requested as JSON, parsed
tolerantly, validated (each nudge must cite evidence and an action) and re-asked on
failure. Saved to data/nudges/<ISO week>.md.

Sections in the summary:
  idle cash drag      wallet balances priced at the idle rate (analytics.wallet_drag)
  card milestones     fee-waiver progress and what is still needed per day
  reward points       balances and statement-vs-expected gaps
  grocery inflation   items whose price per unit jumped (analytics.grocery_inflation)
  spending shifts     this week vs the 4-week average per category
  upcoming dues       next 14 days (google_sync.plan_reminders source data)
  data hygiene        unmatched transfers in Clearing, uncategorised spend, receipts to review
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from decimal import Decimal

import analytics
import google_sync
import ledger_io
import milestones
import sidecar
from common import (OllamaError, data_dir, load_config, money, ollama_json, parse_date, run_cli,
                    setup_logging)

NUDGE_SCHEMA = {
    "type": "object",
    "properties": {"nudges": {"type": "array", "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "evidence": {"type": "string"},
                       "action": {"type": "string"}},
        "required": ["title", "evidence", "action"]}}},
    "required": ["nudges"],
}

SYSTEM = """You are a calm, practical money coach for an Indian household that keeps its \
books in Beancount. You get a weekly summary with exact numbers. Write 3 to 5 nudges that \
would most improve their financial discipline this week.
Rules:
- Use ONLY numbers that appear in the summary, copied exactly. Never estimate or invent figures.
- Each nudge: a short title, the evidence (quote the numbers), and ONE concrete action doable this week.
- Prioritise money actually being lost (idle cash, missed fee waivers, overdue items) over small optimisations.
- Skip a section if it has nothing actionable. No generic advice, no lecturing.
Reply with JSON: {"nudges": [{"title": "...", "evidence": "...", "action": "..."}]}"""


def _inr(d) -> str:
    return f"₹{money(Decimal(str(d))):,}"


def build_summary(entries, cfg: dict, on: date, conn=None) -> dict[str, list[str]]:
    """Section name -> bullet lines. Pure data; no model involved."""
    s: dict[str, list[str]] = {}
    week_start = on - timedelta(days=6)

    drags = analytics.wallet_drag(entries, cfg, on - timedelta(days=89), on)
    s["Idle cash drag (last 90 days)"] = [
        f"{d.account}: now {_inr(d.current_balance)}, 90-day average {_inr(d.avg_balance)}, "
        f"interest lost {_inr(d.lost_interest)} at {d.rate:.0%}; costs {_inr(d.yearly_cost_at_current)}/year "
        f"if the current balance stays idle" for d in drags if d.avg_balance > 0 or d.current_balance > 0]

    s["Card fee-waiver milestones"] = [
        f"{p.card}: spent {_inr(p.spent)} of {_inr(p.threshold)} ({p.pct}%) in card year "
        f"{p.start}..{p.end}; {p.days_left} days left; needs {_inr(p.needed_per_day)}/day; "
        f"projected {_inr(p.projected)} -> " + ("DONE" if p.spent >= p.threshold else
                                               "on track" if p.on_track else "AT RISK")
        for p in milestones.milestone_progress(entries, cfg, on)]

    s["Reward points"] = [
        f"{r.card}: balance {r.balance} {r.commodity}; this card year statements show "
        f"{r.statement_points}, spend suggests about {r.estimated_points}"
        for r in milestones.points_status(entries, cfg, on)]

    conn = conn or sidecar.connect()
    s["Grocery price jumps (recent 4 weeks vs previous 16)"] = [
        f"{r['item']}: {_inr(r['baseline'])}/{r['unit']} -> {_inr(r['recent'])}/{r['unit']} "
        f"({r['change_pct']:+}%)" for r in analytics.grocery_inflation(conn, cfg, on)[:8]]

    s[f"Spending this week ({week_start}..{on}) vs 4-week average"] = [
        f"{r['category']}: {_inr(r['this_week'])} vs {_inr(r['weekly_avg'])} average"
        for r in analytics.weekly_spend_shift(entries, on)[:6] if r["this_week"] or r["weekly_avg"]]

    s["Due in the next 14 days"] = [
        f"{d.when}: {d.title}" for d in sorted(
            {d.key: d for d in google_sync.ledger_dues(entries) + google_sync.card_dues(cfg, on)}.values(),
            key=lambda d: d.when) if on <= d.when <= on + timedelta(days=14)]

    hygiene = []
    clearing: dict[str, Decimal] = {}
    uncategorised = Decimal(0)
    review = 0
    for txn in ledger_io.transactions(entries):
        if txn.date > on:
            continue
        review += txn.flag == "!"
        for p in txn.postings:
            if not p.units or p.units.currency != "INR":
                continue
            if re.match(r"^Assets:[^:]+:Clearing$", p.account):
                clearing[p.account] = clearing.get(p.account, Decimal(0)) + p.units.number
            if p.account.endswith(":Uncategorized") and txn.date >= week_start:
                uncategorised += p.units.number
    for account, bal in sorted(clearing.items()):
        if bal:   # per account: Self's -3000 and Mom's +3000 are two open items, not zero
            hygiene.append(f"{_inr(abs(bal))} sits in {account}: a transfer whose other half "
                           "has not been imported")
    if uncategorised:
        hygiene.append(f"{_inr(uncategorised)} of this week's spend is uncategorised")
    if review:
        hygiene.append(f"{review} ledger transaction(s) flagged '!' for review")
    n = conn.execute("SELECT COUNT(*) FROM receipts WHERE needs_review = 1").fetchone()[0]
    if n:
        hygiene.append(f"{n} grocery receipt(s) need review")
    s["Data hygiene"] = hygiene
    return s


def render_summary(summary: dict[str, list[str]], on: date) -> str:
    out = [f"WEEKLY SUMMARY - week ending {on.isoformat()}"]
    for title, lines in summary.items():
        out.append(f"\n## {title}")
        out += [f"- {ln}" for ln in lines] or ["- nothing to report"]
    return "\n".join(out)


def validate(value) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("nudges"), list):
        return ['reply must be an object with a "nudges" list']
    nudges = value["nudges"]
    problems = []
    if not 1 <= len(nudges) <= 5:
        problems.append(f"give 3 to 5 nudges, not {len(nudges)}")
    for i, n in enumerate(nudges, 1):
        if not isinstance(n, dict):
            problems.append(f"nudge {i} is not an object")
            continue
        for k in ("title", "evidence", "action"):
            if not str(n.get(k) or "").strip():
                problems.append(f"nudge {i} has no {k}")
    return problems


def numbers_not_in(text: str, summary_text: str) -> list[str]:
    """Rupee figures the model wrote that do not appear in the summary (hallucination check)."""
    def nums(t):
        return {re.sub(r"[,\s]", "", m) for m in re.findall(r"₹\s?([\d,]+(?:\.\d+)?)", t)}
    allowed = nums(summary_text) | {n.split(".")[0] for n in nums(summary_text)}
    return sorted(n for n in nums(text) if n not in allowed and n.split(".")[0] not in allowed)


def render_nudges(nudges: list[dict], on: date, unverified: list[str]) -> str:
    out = [f"# Money nudges - week ending {on:%d %b %Y}", ""]
    for i, n in enumerate(nudges, 1):
        out += [f"## {i}. {n['title'].strip()}", f"**Why:** {n['evidence'].strip()}",
                f"**This week:** {n['action'].strip()}", ""]
    if unverified:
        out.append("> Note: these rupee figures in the nudges are not in the summary and may be "
                   "wrong: " + ", ".join(unverified))
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date")
    p.add_argument("--prompt-only", action="store_true", help="print the prompt; do not call the model")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    setup_logging(args.verbose)
    cfg = load_config()
    on = parse_date(args.date) if args.date else date.today()
    entries, _, _ = ledger_io.load()
    summary_text = render_summary(build_summary(entries, cfg, on), on)
    if args.prompt_only:
        print(SYSTEM + "\n\n" + summary_text)
        return 0
    model = cfg.get("ollama", {}).get("text_model", "gemma3:4b")
    try:
        value, problems = ollama_json([{"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": summary_text}],
                                      model, schema=NUDGE_SCHEMA, validate=validate, cfg=cfg)
    except OllamaError as ex:
        print(summary_text)
        print(f"\nerror: {ex}\n(the summary above is what Gemma would have been given)", file=sys.stderr)
        return 2
    if problems:
        print("warning: model reply still has problems: " + "; ".join(problems), file=sys.stderr)
    nudges = [n for n in value.get("nudges", []) if isinstance(n, dict) and n.get("title")]
    unverified = numbers_not_in(" ".join(f"{n.get('evidence','')} {n.get('action','')}" for n in nudges),
                                summary_text)
    md = render_nudges(nudges, on, unverified)
    print(md)
    if not args.no_save:
        out = data_dir() / "nudges" / f"{on.isocalendar().year}-W{on.isocalendar().week:02d}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md + "\n\n---\n\n" + summary_text + "\n")
        print(f"\nsaved {out}")
    return 0


if __name__ == "__main__":
    run_cli(main)
