"""Reading the ledger and appending to it safely.

Writers never edit existing text. They append rendered directives to an entity file,
re-load the whole ledger, and roll the file back if the append introduced a new
Beancount error. A bad import therefore never leaves the books unloadable.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from beancount import loader
from beancount.core import data

from common import PFError, log, main_ledger, money


class LedgerError(PFError):
    pass


def load(path: Path | None = None):
    """(entries, errors, options) for the master ledger."""
    path = path or main_ledger()
    if not path.exists():
        raise LedgerError(f"ledger not found: {path}")
    return loader.load_file(str(path))


def transactions(entries) -> list[data.Transaction]:
    return [e for e in entries if isinstance(e, data.Transaction)]


def meta_values(entries, prefix: str) -> set[str]:
    """All string metadata values whose key starts with `prefix` (e.g. 'import_id')."""
    found = set()
    for e in entries:
        for k, v in (getattr(e, "meta", None) or {}).items():
            if k.startswith(prefix) and isinstance(v, str):
                found.add(v)
    return found


# --------------------------------------------------------------------------- rendering

def quote(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_meta_value(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (Decimal, int)):
        return str(v)
    return quote(str(v))


_TAGLIKE = re.compile(r"[^A-Za-z0-9\-_/.]")


def tagify(text: str) -> str:
    """Beancount tags/links allow only [A-Za-z0-9-_/.] - no colons, no spaces."""
    return _TAGLIKE.sub("-", text)


@dataclass
class Posting:
    account: str
    amount: Decimal | None = None
    currency: str = "INR"
    price: str | None = None        # e.g. "@ 0.50 INR"
    meta: dict = field(default_factory=dict)

    def render(self) -> str:
        if self.amount is None:
            line = f"  {self.account}"
        else:
            amt = money(self.amount) if self.currency == "INR" else self.amount
            line = f"  {self.account:<44} {amt:>12} {self.currency}"
            if self.price:
                line += f" {self.price}"
        lines = [line]
        lines += [f"    {k}: {render_meta_value(v)}" for k, v in self.meta.items()]
        return "\n".join(lines)


@dataclass
class Txn:
    date: date
    payee: str | None
    narration: str
    postings: list[Posting]
    flag: str = "*"
    tags: Iterable[str] = ()
    links: Iterable[str] = ()
    meta: dict = field(default_factory=dict)
    comment: str | None = None

    def render(self) -> str:
        head = f"{self.date.isoformat()} {self.flag} "
        head += f"{quote(self.payee)} {quote(self.narration)}" if self.payee else quote(self.narration)
        for t in self.tags:
            head += f" #{tagify(t)}"
        for ln in self.links:
            head += f" ^{tagify(ln)}"
        lines = []
        if self.comment:
            lines += [f"; {c}" for c in self.comment.splitlines()]
        lines.append(head)
        lines += [f"  {k}: {render_meta_value(v)}" for k, v in self.meta.items()]
        lines += [p.render() for p in self.postings]
        return "\n".join(lines)

    def check_balance(self) -> None:
        """Raise when the INR legs that carry amounts do not sum to zero.

        Postings in other commodities (reward points) or with a price annotation are
        left to Beancount, which checks them on load.
        """
        legs = [p for p in self.postings if p.amount is not None]
        if any(p.amount is None for p in self.postings):
            return
        if any(p.currency != "INR" or p.price for p in legs):
            return
        total = sum((money(p.amount) for p in legs), Decimal(0))
        if total != 0:
            raise LedgerError(f"transaction {self.payee!r} on {self.date} is off by {total} INR")


@dataclass
class Note:
    """A Beancount `note` directive - used to record that a statement line was matched
    to an existing entry, so the match survives into later runs."""
    date: date
    account: str
    comment: str
    meta: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{self.date.isoformat()} note {self.account} {quote(self.comment)}"]
        lines += [f"  {k}: {render_meta_value(v)}" for k, v in self.meta.items()]
        return "\n".join(lines)

    def check_balance(self) -> None:
        return None


# --------------------------------------------------------------------------- appending

def _error_keys(errors) -> Counter:
    # Line numbers shift as files grow, so compare on message + file name; a Counter so
    # a second copy of an already-present error still counts as new.
    keys: Counter = Counter()
    for err in errors:
        src = getattr(err, "source", None) or {}
        keys[f"{Path(str(src.get('filename', ''))).name}|{err.message}"] += 1
    return keys


def _joined(before: str, chunks: list[str], header: str | None) -> str:
    text = "" if before.endswith("\n\n") or not before else ("\n" if before.endswith("\n") else "\n\n")
    if header:
        text += f"; ---- {header}\n"
    return before + text + "\n\n".join(chunks) + "\n"


def append_many(chunks_by_file: dict[Path, list[str]], *, header: str | None = None,
                validate: bool = True) -> int:
    """Append to several ledger files as one unit: if the combined result adds a ledger
    error, every file is restored. Returns the number of chunks written."""
    work = {Path(p): c for p, c in chunks_by_file.items() if c}
    if not work:
        return 0
    before = {p: (p.read_text() if p.exists() else "") for p in work}
    baseline = _error_keys(load()[1]) if validate else Counter()
    try:
        for p, chunks in work.items():
            p.write_text(_joined(before[p], chunks, header))
        if validate:
            new_errors = _error_keys(load()[1]) - baseline
            if new_errors:
                raise LedgerError("append rolled back - it would add ledger errors:\n  "
                                  + "\n  ".join(sorted(new_errors)))
    except BaseException:
        for p, text in before.items():
            p.write_text(text)
        raise
    n = sum(len(c) for c in work.values())
    log.info("appended %d entr%s to %s", n, "y" if n == 1 else "ies", ", ".join(p.name for p in work))
    return n


def append(path: Path, chunks: list[str], *, header: str | None = None,
           validate: bool = True) -> int:
    """Append rendered directives to one file; roll back if the ledger gains an error."""
    return append_many({Path(path): chunks}, header=header, validate=validate)
