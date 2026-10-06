"""Statement ingestion: unlock PDFs, parse transactions, pair transfers, dedupe, append.

    importer.py import [PDF ...]        # default: every PDF in data/raw_statements/
                [--dry-run] [--source ID] [--ask-password] [--dump-text]
    importer.py split --date D --from ACCOUNT --total N --part ACCOUNT=AMOUNT ...

How a statement becomes ledger entries:
  1. The PDF's source (card / bank account) is found from config [sources.*].match globs,
     or from the card's last 4 digits in the text when no glob matches.
  2. It is unlocked in memory - never written decrypted to disk - by trying that
     source's password patterns ([passwords] in config) filled from Keychain secrets.
  3. Lines are parsed by a bank profile: one general line rule (date ... amount Cr/Dr)
     parameterised per issuer, or the running-balance rule for savings accounts, which
     reads direction from how the balance moved instead of trusting column layout.
  4. Each line gets a fingerprint (import_id), counted per statement. Lines already in
     the ledger - or seen earlier in the same run (a duplicate PDF) - are skipped, so
     re-importing is harmless.
  5. A line already recorded another way - a grocery receipt, an Amazon Pay gift card
     load, a split debit; anything without an import_id that is not an opening balance -
     on the same account for the same amount within the transfer window is skipped. A
     `note` records the match so that entry can never stand in for a second line later.
  6. Two lines on different accounts with the same amount in opposite directions within
     ±window_days, BOTH reading as transfers ([transfers].keywords), become ONE balanced
     transaction (e.g. Assets:Self:HDFC -> Assets:Mom:Savings). A transfer whose other
     half is not in this batch - whichever side arrives first - parks in
     Assets:<Entity>:Clearing and is closed off when the other statement is imported.
  7. Everything else is categorised by [[rules]]; unmatched lines go to
     Uncategorized and are flagged "!". All files are appended as one unit.
"""
from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import io
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pikepdf
import pdfplumber
from beancount.core import data

import ledger_io
from common import (PFError, data_dir, entity_label, entity_ledger, entity_of_account,
                    fill_entity, load_config, log, money, parse_date, run_cli, secret,
                    setup_logging, sources, to_decimal)
from ledger_io import Posting, Txn


class StatementError(PFError):
    pass


class PasswordError(StatementError):
    pass


AMT = r"\d{1,3}(?:,\d{2,3})*\.\d{2}|\d+\.\d{2}"


# --------------------------------------------------------------------------- bank profiles

@dataclass(frozen=True)
class Profile:
    """How one issuer prints a transaction line. Data, not code: add a bank here."""
    date_re: str
    date_formats: tuple[str, ...]
    credit_marks: tuple[str, ...] = ("Cr", "CR", "C")
    has_ref: bool = False        # a long reference number right after the date (ICICI)
    has_points: bool = False     # reward points printed before the amount
    balance_rule: bool = False   # savings statement: amount + running balance per line


SKIP = re.compile(r"(?i)opening balance|closing balance|\btotal\b|minimum amount|"
                  r"statement date|payment due|credit limit|available limit|previous balance")

PROFILES: dict[str, Profile] = {
    # 12/09/2026 [14:22] SWIGGY BANGALORE [12] 1,234.00 [Cr]
    "hdfc_cc": Profile(r"\d{2}/\d{2}/\d{4}", ("%d/%m/%Y",), has_points=True),
    # 12 Sep 26 AMAZON PAY INDIA 1,234.00 D|C
    "sbi_cc": Profile(r"\d{2} [A-Za-z]{3} \d{2,4}", ("%d %b %y", "%d %b %Y"),
                      credit_marks=("C", "CR", "Cr")),
    # 12/09/2026 10234567891 AMAZON PAY INDIA [12] 1,234.00 [CR]
    "icici_cc": Profile(r"\d{2}/\d{2}/\d{4}", ("%d/%m/%Y",), has_ref=True, has_points=True),
    # 01/09/26 NEFT CR-... [ref] [value date] 50,000.00 1,23,456.78
    "bank_balance": Profile(r"\d{2}[/-]\d{2}[/-]\d{2,4}|\d{2} [A-Za-z]{3} \d{2,4}",
                            ("%d/%m/%y", "%d/%m/%Y", "%d-%m-%y", "%d-%m-%Y", "%d %b %y", "%d %b %Y"),
                            balance_rule=True),
}

_CREDIT_WORDS = re.compile(r"(?i)\bcr\b|deposit|credit|salary|interest|refund|reversal|neft cr|imps cr")


@dataclass
class Line:
    date: date
    description: str
    amount: Decimal          # signed for the account: + money in / liability down, - money out
    source: dict
    ref: str = ""
    points: int | None = None
    flags: list[str] = field(default_factory=list)
    fp: str = ""

    @property
    def account(self) -> str:
        return self.source["account"]

    @property
    def entity(self) -> str:
        return self.source["entity"]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" |-")


def parse_lines(text_lines: list[str], source: dict) -> list[Line]:
    profile = PROFILES.get(source["parser"])
    if profile is None:
        raise StatementError(f"unknown parser {source['parser']!r} for source {source['id']}; "
                             f"known: {', '.join(PROFILES)}")
    return (_parse_balance(text_lines, source, profile) if profile.balance_rule
            else _parse_card(text_lines, source, profile))


def _date_led(raw: str, p: Profile) -> bool:
    return re.match(rf"^\s*(?:{p.date_re})\b", raw) is not None


def _report_unparsed(source: dict, unparsed: list[str]) -> None:
    if unparsed:
        log.warning("%s: %d date-led line(s) did not match the %s layout and were NOT imported "
                    "(re-run with --dump-text): %s", source["id"], len(unparsed), source["parser"],
                    " | ".join(u.strip()[:60] for u in unparsed[:3]))


def _split_points(rest: str, amount: Decimal, credit: bool) -> tuple[str, int | None]:
    """Strip a trailing reward-points number - only when it is plausible as points.

    Points never exceed half the rupees spent on any Indian card, and credits only carry
    points when they are negative (reversals), so "MCDONALDS 1023 850.00" keeps its 1023
    and "PAYMENT ... 12345 45,000.00 Cr" keeps its reference.
    """
    r = re.match(r"(.*?)\s+([+-]?\s?\d{1,5})$", rest)
    if not r:
        return rest, None
    pts = int(r.group(2).replace(" ", ""))
    if credit and pts >= 0:
        return rest, None
    if abs(pts) > amount / 2:
        return rest, None
    return r.group(1), pts


def _parse_card(text_lines: list[str], source: dict, p: Profile) -> list[Line]:
    marks = "|".join(sorted({*p.credit_marks, "Dr", "DR", "D"}, key=len, reverse=True))
    rx = re.compile(rf"^\s*(?P<date>{p.date_re})(?:\s*\|?\s*\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?\s+"
                    rf"(?P<rest>.+?)\s+(?P<neg>-)?\s?(?P<amt>{AMT})\s*(?P<mark>{marks})?\s*$")
    out, unparsed = [], []
    for raw in text_lines:
        # A line that starts with a date is a transaction even if it says "TOTAL" or
        # "CREDIT LIMIT"; SKIP only filters summary rows that do not.
        if not _date_led(raw, p):
            continue
        m = rx.match(raw)
        try:
            d = parse_date(m["date"], list(p.date_formats)) if m else None
        except ValueError:
            d = None
        if d is None:
            if not SKIP.search(raw):
                unparsed.append(raw)
            continue
        rest, ref, points = m["rest"], "", None
        amount = to_decimal(m["amt"])
        credit = (m["mark"] or "") in p.credit_marks or bool(m["neg"])
        if p.has_ref:
            r = re.match(r"(\d{6,})\s+(.*)", rest)
            if r:
                ref, rest = r.group(1), r.group(2)
        if p.has_points:
            rest, points = _split_points(rest, amount, credit)
        out.append(Line(d, _clean(rest), amount if credit else -amount, source, ref=ref, points=points))
    _report_unparsed(source, unparsed)
    return out


def _parse_balance(text_lines: list[str], source: dict, p: Profile) -> list[Line]:
    """Savings statements: the last two amounts are (transaction, closing balance).

    Direction comes from how the balance moved, so it does not matter which column
    (withdrawal / deposit) the bank printed the amount in. Lines where the movement
    does not equal the amount are kept but flagged for review. A date-led line that
    cannot be parsed breaks the balance chain, so the next line's direction is flagged
    as guessed rather than silently inferred across the gap.
    """
    rx = re.compile(rf"^\s*(?P<date>{p.date_re})\s+(?P<rest>.*?)\s+(?P<amt>{AMT})\s+"
                    rf"(?P<bal>{AMT})\s*(?P<balmark>Cr|Dr|CR|DR)?\s*$")
    opening = None
    joined = "\n".join(text_lines)
    om = re.search(rf"(?i)opening balance\s*:?\s*(?:Rs\.?|INR|₹)?\s*({AMT})", joined)
    if om:
        opening = to_decimal(om.group(1))
    out, unparsed, prev = [], [], opening
    for raw in text_lines:
        if not _date_led(raw, p):
            continue
        m = rx.match(raw)
        try:
            d = parse_date(m["date"], list(p.date_formats)) if m else None
        except ValueError:
            d = None
        if d is None:
            if not SKIP.search(raw):
                unparsed.append(raw)
                prev = None
            continue
        amount, bal = to_decimal(m["amt"]), to_decimal(m["bal"])
        if (m["balmark"] or "").lower() == "dr":
            bal = -bal
        rest, ref = m["rest"], ""
        # Trailing value-date and reference columns are not part of the narration;
        # the value date is dropped, the reference kept.
        while True:
            t = re.match(rf"(.*?)\s+({p.date_re}|\S*\d{{6,}}\S*)$", rest)
            if not t:
                break
            rest = t.group(1)
            if not re.fullmatch(p.date_re, t.group(2)):
                ref = (t.group(2) + " " + ref).strip()
        flags = []
        if prev is not None and abs(abs(bal - prev) - amount) < Decimal("0.01"):
            sign = 1 if bal > prev else -1
        else:
            sign = 1 if _CREDIT_WORDS.search(rest) else -1
            flags.append("direction guessed: balance movement did not match the amount"
                         if prev is not None else "direction guessed: previous balance unknown")
        prev = bal
        out.append(Line(d, _clean(rest), sign * amount, source, ref=ref, flags=flags))
    _report_unparsed(source, unparsed)
    return out


# --------------------------------------------------------------------------- passwords

_FIELD_SECRET = {"name4_upper": "name", "name4_lower": "name", "name4_title": "name",
                 "ddmm": "dob", "ddmmyy": "dob", "ddmmyyyy": "dob", "yyyy": "dob",
                 "customer_id": "customer_id"}


def password_candidates(source: dict, cfg: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """[(pattern, password)] for a source, plus the secrets that were missing.

    The pattern string (e.g. "{name4_upper}{ddmm}") is what gets reported - never the
    password itself.
    """
    entity = source["entity"]
    ctx: dict[str, str] = {}
    name = secret(f"{entity}.name")
    if name:
        first = re.sub(r"[^A-Za-z]", "", name.split()[0]) if name.split() else ""
        n4 = first[:4]
        ctx.update(name4_upper=n4.upper(), name4_lower=n4.lower(), name4_title=n4.title())
    dob = secret(f"{entity}.dob")
    if dob:
        try:
            d = parse_date(dob)
            ctx.update(ddmm=d.strftime("%d%m"), ddmmyy=d.strftime("%d%m%y"),
                       ddmmyyyy=d.strftime("%d%m%Y"), yyyy=d.strftime("%Y"))
        except ValueError:
            log.warning("secret %s.dob is not a date - fix it with `common.py secret set %s.dob`",
                        entity, entity)
    last4 = str(source.get("last4") or "")
    if re.fullmatch(r"\d{4}", last4) and last4 != "0000":
        ctx["last4"] = last4
    cid = secret(f"{entity}.customer_id")
    if cid:
        ctx["customer_id"] = cid
    pw_cfg = cfg.get("passwords", {})
    patterns = list(dict.fromkeys(pw_cfg.get(source["parser"], []) + pw_cfg.get("fallback", [])))
    out, missing = [], set()
    for pat in patterns:
        fields_ = re.findall(r"\{(\w+)\}", pat)
        absent = [f for f in fields_ if f not in ctx]
        if absent:
            missing.update(f"{entity}.{_FIELD_SECRET[f]}" if f in _FIELD_SECRET
                           else f"[sources.{source['id']}].{f}" for f in absent)
            continue
        out.append((pat, pat.format(**ctx)))
    return out, sorted(missing)


def unlock(path: Path, candidates: list[tuple[str, str]],
           missing: list[str] | None = None) -> tuple[bytes, str]:
    """Decrypt in memory. Returns (pdf bytes without encryption, label of what worked)."""
    def to_bytes(pdf) -> bytes:
        buf = io.BytesIO()
        pdf.save(buf)  # default save drops encryption; the bytes never touch disk
        return buf.getvalue()

    try:
        with pikepdf.open(path) as pdf:
            return to_bytes(pdf), "not encrypted"
    except pikepdf.PasswordError:
        pass
    except (pikepdf.PdfError, OSError) as ex:
        raise StatementError(f"{path.name}: not a readable PDF ({ex})")
    for label, pw in candidates:
        try:
            with pikepdf.open(path, password=pw) as pdf:
                return to_bytes(pdf), label
        except pikepdf.PasswordError:
            continue
        except pikepdf.PdfError as ex:
            raise StatementError(f"{path.name}: damaged PDF ({ex})")
    msg = f"{path.name}: none of {len(candidates)} password pattern(s) worked"
    if candidates:
        msg += " (" + ", ".join(lbl for lbl, _ in candidates) + ")"
    if missing:
        msg += ". Missing secrets that other patterns need: " + ", ".join(missing)
    msg += ". Check the password rule in the statement e-mail, fix [passwords] or the " \
           "secrets, or re-run with --ask-password."
    raise PasswordError(msg)


def pdf_text_lines(pdf_bytes: bytes) -> list[str]:
    lines = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            lines += (page.extract_text() or "").splitlines()
    return lines


# --------------------------------------------------------------------------- source detection

def match_source(path: Path, srcs: dict[str, dict]) -> dict | None:
    name = path.name.lower()
    hits = [s for s in srcs.values() if any(fnmatch.fnmatch(name, g.lower()) for g in s.get("match", []))]
    if len(hits) > 1:
        raise StatementError(f"{path.name} matches several sources ({', '.join(s['id'] for s in hits)}); "
                             "tighten their `match` globs or pass --source")
    return hits[0] if hits else None


def source_by_last4(lines: list[str], srcs: dict[str, dict]) -> dict | None:
    """Identify a card by its masked number (XXXX 1234 / **1234); never by bare digits,
    which a reference number could end in."""
    text = "\n".join(lines)
    hits = [s for s in srcs.values()
            if re.fullmatch(r"\d{4}", str(s.get("last4", ""))) and s["last4"] != "0000"
            and re.search(rf"[Xx*]{{2,}}[\sXx*-]*{s['last4']}\b", text)]
    return hits[0] if len(hits) == 1 else None


def read_statement(path: Path, cfg: dict, *, source_id: str | None = None,
                   ask_password: bool = False, dump_text: bool = False) -> tuple[dict, list[Line], str]:
    srcs = sources(cfg)
    if source_id:
        if source_id not in srcs:
            raise StatementError(f"unknown source {source_id!r}; known: {', '.join(srcs)}")
        src = srcs[source_id]
    else:
        src = match_source(path, srcs)
    if src:
        cands, missing = password_candidates(src, cfg)
    else:  # unknown file: try everyone's patterns, identify it afterwards by last 4 digits
        cands, missing = [], []
        for s in srcs.values():
            c, m = password_candidates(s, cfg)
            cands += [x for x in c if x not in cands]
            missing += [x for x in m if x not in missing]
    if ask_password:
        cands = [("--ask-password", getpass.getpass(f"password for {path.name}: "))] + cands
    pdf_bytes, how = unlock(path, cands, missing)
    lines = pdf_text_lines(pdf_bytes)
    if dump_text:
        print(f"----- {path.name} ({len(lines)} text lines) -----")
        print("\n".join(lines))
    if src is None:
        src = source_by_last4(lines, srcs)
        if src is None:
            raise StatementError(f"{path.name}: cannot tell which card/account this is - add a "
                                 "`match` glob or a real `last4` to its [sources.*] block, or pass --source")
    parsed = parse_lines(lines, src)
    assign_fingerprints(parsed)
    if not parsed:
        log.warning("%s: unlocked (%s) but no transaction lines matched the %s layout - "
                    "re-run with --dump-text and adjust PROFILES", path.name, how, src["parser"])
    return src, parsed, how


# --------------------------------------------------------------------------- dedup + pairing

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def assign_fingerprints(lines: list[Line]) -> None:
    """Stable id per line of ONE statement.

    The occurrence counter keeps two identical coffees on the same day apart. It runs per
    statement, so the same statement imported twice (or a re-downloaded copy, or two
    overlapping statements) yields the same ids and the copy is recognised. The amount is
    taken unsigned so a savings line whose direction was guessed differently on a
    re-download still matches.
    """
    seen: Counter = Counter()
    for ln in lines:
        key = f"{ln.account}|{ln.date}|{money(abs(ln.amount))}|{_norm(ln.description)}"
        seen[key] += 1
        ln.fp = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:16]


def _entry_key(d: date, account: str, amount: Decimal) -> str:
    return f"{d}|{account}|{money(amount)}"


def recorded_index(entries) -> dict[tuple[str, Decimal], list[tuple[date, str, str]]]:
    """(account, amount) -> [(date, label, key)] for money recorded other than by this
    importer (receipts, gift card loads, splits): transactions without an import_id.
    Opening balances (anything touching Equity) are not purchases and never match.
    Entries already matched by an earlier import (a `note` carrying matched_entry) are
    used up, so one hand entry can only stand in for one statement line, ever.
    """
    used = Counter(e.meta["matched_entry"] for e in entries
                   if isinstance(e, data.Note) and "matched_entry" in (e.meta or {}))
    idx = defaultdict(list)
    for t in ledger_io.transactions(entries):
        if any(k.startswith("import_id") for k in t.meta) or \
                any(p.account.startswith("Equity:") for p in t.postings):
            continue
        label = f"receipt {t.meta['receipt_id']}" if t.meta.get("receipt_id") else \
            f"{t.date} {t.payee or ''} {t.narration}".strip()
        for p in t.postings:
            if p.units and p.units.currency == "INR":
                key = _entry_key(t.date, p.account, p.units.number)
                if used[key]:
                    used[key] -= 1
                    continue
                idx[(p.account, money(p.units.number))].append((t.date, label, key))
    return idx


CLEARING = re.compile(r"^Assets:[^:]+:Clearing$")


def open_clearing_items(entries, window: int) -> list[dict]:
    """Clearing postings not yet netted off by an opposite posting in the SAME Clearing
    account within the window."""
    items = []
    for t in ledger_io.transactions(entries):
        for p in t.postings:
            if CLEARING.match(p.account) and p.units:
                items.append({"date": t.date, "account": p.account,
                              "amount": money(p.units.number), "used": False,
                              "source_accounts": [q.account for q in t.postings if q is not p]})
    for a in items:
        if a["used"]:
            continue
        for b in items:
            if b is not a and not b["used"] and b["account"] == a["account"] \
                    and b["amount"] == -a["amount"] and abs((b["date"] - a["date"]).days) <= window:
                a["used"] = b["used"] = True
                break
    return [i for i in items if not i["used"]]


def direction_note(from_entity: str, to_entity: str, cfg: dict) -> dict:
    return cfg.get("transfers", {}).get("notes", {}).get(f"{from_entity}->{to_entity}", {})


def counter_account(ln: Line, cfg: dict, transfer_like: bool) -> tuple[str, str | None]:
    """(account for the other leg, review reason or None).

    Order: a category rule; else a transfer keyword -> the entity's Clearing account,
    waiting for the other half; else Uncategorized, flagged. Unknown card credits are
    refunds, so they reduce Expenses:<Entity>:Uncategorized rather than look like income.
    """
    for rule in cfg.get("rules", []):
        if re.search(rule["pattern"], ln.description):
            return fill_entity(rule["account"], ln.entity, cfg), None
    label = entity_label(ln.entity, cfg)
    if transfer_like:
        return f"Assets:{label}:Clearing", None
    if ln.amount < 0 or ln.source.get("kind") == "card":
        return f"Expenses:{label}:Uncategorized", "no category rule matched"
    return f"Income:{label}:Uncategorized", "no category rule matched"


def build(lines: list[Line], entries, cfg: dict) -> tuple[list[tuple[str, object]], Counter]:
    """Turn parsed lines into (entity, Txn | Note) pairs plus a tally of what happened."""
    tcfg = cfg.get("transfers", {})
    window = int(tcfg.get("window_days", 2))
    kw = re.compile(tcfg.get("keywords", r"(?i)neft|imps|rtgs|transfer"))
    stats: Counter = Counter()

    by_source: dict[str, list[Line]] = defaultdict(list)
    for ln in lines:
        if not ln.fp:
            by_source[ln.source["id"]].append(ln)
    for group in by_source.values():
        assign_fingerprints(group)

    known = ledger_io.meta_values(entries, "import_id")
    fresh, seen = [], set()
    for ln in lines:
        if ln.fp in known:
            stats["already in ledger"] += 1
        elif ln.fp in seen:
            stats["duplicate within this run"] += 1
        else:
            seen.add(ln.fp)
            fresh.append(ln)

    out: list[tuple[str, object]] = []
    recorded = recorded_index(entries)
    kept = []
    for ln in fresh:
        options = recorded.get((ln.account, money(ln.amount)), [])
        hit = next((o for o in options if abs((o[0] - ln.date).days) <= window), None)
        if hit:
            options.remove(hit)
            stats["already recorded by hand/receipt"] += 1
            log.info("skip %s %s %s - already recorded (%s)", ln.date, ln.description, ln.amount, hit[1])
            out.append((ln.entity, ledger_io.Note(
                ln.date, ln.account, f"Statement line {ln.description} {ln.amount} matched {hit[1]}",
                meta={"import_id": ln.fp, "matched_entry": hit[2]})))
        else:
            kept.append(ln)

    transfer_like = [bool(kw.search(ln.description)) for ln in kept]
    # Pair transfers inside the batch: both sides must read as a transfer; closest dates
    # first, one partner each; any line with a second possible partner is flagged.
    cands = []
    for i, a in enumerate(kept):
        for j in range(i + 1, len(kept)):
            b = kept[j]
            if a.account == b.account or a.amount != -b.amount:
                continue
            dd = abs((a.date - b.date).days)
            if dd <= window and transfer_like[i] and transfer_like[j]:
                cands.append((dd, i, j))
    cands.sort()
    n_options = Counter(x for _, i, j in cands for x in (i, j))
    partner: dict[int, int] = {}
    for dd, i, j in cands:
        if i in partner or j in partner:
            continue
        partner[i], partner[j] = j, i
        out.append(_transfer_txn(kept[i], kept[j], cfg, n_options[i] > 1 or n_options[j] > 1))
        stats["transfers paired"] += 1

    clearing = open_clearing_items(entries, window)
    for idx, ln in enumerate(kept):
        if idx in partner:
            continue
        # The other half already in the ledger, parked in a Clearing account?
        hit = transfer_like[idx] and next(
            (c for c in clearing if c["amount"] == ln.amount and ln.account not in c["source_accounts"]
             and abs((c["date"] - ln.date).days) <= window), None)
        if hit:
            clearing.remove(hit)
            out.append(_single_txn(ln, cfg, hit["account"], None, closes_clearing=True))
            stats["transfers closed against Clearing"] += 1
            continue
        counter, reason = counter_account(ln, cfg, transfer_like[idx])
        out.append(_single_txn(ln, cfg, counter, reason))
        stats["flagged for review" if out[-1][1].flag == "!" else
              "waiting in Clearing" if CLEARING.match(counter) else "new"] += 1
    out.sort(key=lambda et: et[1].date)
    return out, stats


def _transfer_txn(a: Line, b: Line, cfg: dict, ambiguous: bool) -> tuple[str, Txn]:
    out_ln, in_ln = (a, b) if a.amount < 0 else (b, a)
    amt = abs(out_ln.amount)
    note = direction_note(out_ln.entity, in_ln.entity, cfg)
    same = out_ln.entity == in_ln.entity
    payee = ("Card payment" if in_ln.source.get("kind") == "card" else "Own transfer") if same \
        else f"Transfer {entity_label(out_ln.entity, cfg)} -> {entity_label(in_ln.entity, cfg)}"
    meta = {"import_id": out_ln.fp, "import_id_2": in_ln.fp}
    if in_ln.date != out_ln.date:
        meta["received"] = in_ln.date
    if note.get("tax_note"):
        meta["tax_note"] = note["tax_note"]
    flags = out_ln.flags + in_ln.flags
    if ambiguous:
        flags.append("more than one line could be the other half of this transfer")
    if flags:
        meta["review"] = "; ".join(flags)
    txn = Txn(out_ln.date, payee, out_ln.description,
              [Posting(in_ln.account, amt), Posting(out_ln.account, -amt)],
              flag="!" if flags else "*",
              tags=[note["tag"]] if note.get("tag") else [], meta=meta)
    return out_ln.entity, txn


def _single_txn(ln: Line, cfg: dict, counter: str, reason: str | None,
                closes_clearing: bool = False) -> tuple[str, Txn]:
    flags = list(ln.flags) + ([reason] if reason else [])
    meta = {"import_id": ln.fp, "source": ln.source["id"]}
    if ln.ref:
        meta["ref"] = ln.ref
    if ln.points is not None:
        meta["reward_points"] = ln.points
    tags = []
    if CLEARING.match(counter) and not closes_clearing:
        meta["clearing"] = "transfer waiting for its other half to be imported"
    if closes_clearing:
        other = entity_of_account(counter, cfg)
        frm, to = (other, ln.entity) if ln.amount > 0 else (ln.entity, other)
        note = direction_note(frm or "", to or "", cfg)
        if note.get("tag"):
            tags.append(note["tag"])
        if note.get("tax_note"):
            meta["tax_note"] = note["tax_note"]
    if flags:
        meta["review"] = "; ".join(flags)
    txn = Txn(ln.date, None, ln.description,
              [Posting(ln.account, ln.amount), Posting(counter, -ln.amount)],
              flag="!" if flags else "*", tags=tags, meta=meta)
    return ln.entity, txn


def write(built: list[tuple[str, object]], cfg: dict, *, dry_run: bool, header: str) -> int:
    """Append to every entity's file at once: either all of them change or none does."""
    by_file: dict[Path, list[str]] = defaultdict(list)
    for entity, item in built:
        item.check_balance()
        by_file[entity_ledger(entity, cfg)].append(item.render())
    if dry_run:
        for path, chunks in by_file.items():
            print(f"\n; ===== would append to {path.name} =====")
            print("\n\n".join(chunks))
        return 0
    ledger_io.append_many(by_file, header=header)
    return sum(1 for _, item in built if isinstance(item, Txn))


# --------------------------------------------------------------------------- split debit

def split_txn(*, when: date, from_account: str, total: Decimal, parts: list[tuple[str, Decimal]],
              payee: str | None, narration: str) -> Txn:
    total = money(total)
    part_sum = money(sum((a for _, a in parts), Decimal(0)))
    if part_sum != total:
        raise PFError(f"parts add up to {part_sum}, not the debit total {total} "
                      f"(difference {total - part_sum})")
    postings = [Posting(acc, amt) for acc, amt in parts] + [Posting(from_account, -total)]
    return Txn(when, payee, narration, postings, meta={"split_of": str(total)})


# --------------------------------------------------------------------------- CLI

def cmd_import(args) -> int:
    cfg = load_config()
    paths = [Path(p) for p in args.pdfs] or sorted((data_dir() / "raw_statements").glob("*.pdf"))
    if not paths:
        print(f"no PDFs given and none in {data_dir() / 'raw_statements'}")
        return 1
    entries, errors, _ = ledger_io.load()
    if errors:
        log.warning("ledger already has %d error(s); fix them first for a clean import", len(errors))
    all_lines, failures = [], 0
    for path in paths:
        try:
            src, lines, how = read_statement(path, cfg, source_id=args.source,
                                             ask_password=args.ask_password, dump_text=args.dump_text)
        except StatementError as ex:
            print(f"FAIL  {ex}", file=sys.stderr)
            failures += 1
            continue
        print(f"read  {path.name}: {src['id']} -> {src['account']}, {len(lines)} lines "
              f"(unlocked: {how})")
        all_lines += lines
    built, stats = build(all_lines, entries, cfg)
    written = write(built, cfg, dry_run=args.dry_run,
                    header=f"import {date.today()} from {len(paths)} statement(s)")
    print("\n" + (", ".join(f"{v} {k}" for k, v in stats.items()) or "nothing to do"))
    print("dry run - nothing written" if args.dry_run else f"wrote {written} transaction(s)")
    return 1 if failures else 0


def cmd_split(args) -> int:
    parts = []
    for spec in args.part:
        acc, _, amt = spec.partition("=")
        if not acc or not amt:
            raise PFError(f"--part must look like ACCOUNT=AMOUNT, got {spec!r}")
        parts.append((acc.strip(), to_decimal(amt)))
    txn = split_txn(when=parse_date(args.date), from_account=args.from_account,
                    total=to_decimal(args.total), parts=parts, payee=args.payee,
                    narration=args.narration or "Split debit")
    cfg = load_config()
    entity = entity_of_account(args.from_account, cfg) or cfg["general"]["default_entity"]
    write([(entity, txn)], cfg, dry_run=args.dry_run, header="split debit")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="import statement PDFs")
    i.add_argument("pdfs", nargs="*")
    i.add_argument("--dry-run", action="store_true", help="print what would be appended")
    i.add_argument("--source", help="force the [sources.*] id for every PDF given")
    i.add_argument("--ask-password", action="store_true", help="prompt for a password first")
    i.add_argument("--dump-text", action="store_true", help="print extracted text (tune parsers)")
    s = sub.add_parser("split", help="record one debit split across several accounts")
    s.add_argument("--date", required=True)
    s.add_argument("--from", dest="from_account", required=True)
    s.add_argument("--total", required=True)
    s.add_argument("--part", action="append", required=True, help="ACCOUNT=AMOUNT (repeat)")
    s.add_argument("--payee")
    s.add_argument("--narration")
    s.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    setup_logging(args.verbose)
    return cmd_import(args) if args.cmd == "import" else cmd_split(args)


if __name__ == "__main__":
    run_cli(main)
