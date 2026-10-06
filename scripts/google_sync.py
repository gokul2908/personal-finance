"""Google Calendar reminders for due dates, and Google Drive backup of the books.

    google_sync.py auth                    # one-time browser sign-in (click Allow)
    google_sync.py remind [--apply] [--horizon 60] [--date D]
    google_sync.py backup [--apply]

Both commands only print their plan unless --apply is given.

Due dates come from two places:
  * the ledger: any directive with `due: YYYY-MM-DD` metadata (insurance policies are
    `custom "policy" ...` entries with kind Health/Term/Vehicle), or a transaction
    tagged #due-YYYY-MM-DD. (Beancount tags cannot contain ':', so "#due:..." is not
    valid syntax.)
  * config: every card with `due_day` gets its next payment due date.
Each due date gets an all-day event N days before it for N in [google].reminder_offsets
(7 and 2). Event ids are derived from the due item, so re-running never duplicates.

Backup uploads ledgers/**/*.beancount and a consistent snapshot of sidecar.db into
one Drive folder, skipping files whose MD5 already matches. Scope is drive.file: the
app can only see files it created.

Auth reuses the desktop OAuth client already in the Keychain for salary-invoice and
keeps this project's own token (service "personal-finance", item "google-token").
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import ledger_io
from common import (KEYCHAIN_SERVICE, PFError, ledger_dir, load_config, parse_date, run_cli,
                    setup_logging, sidecar_path, sources)

SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/drive.file"]
TOKEN_ITEM = "google-token"
DUE_TAG = re.compile(r"^due-(\d{4}-\d{2}-\d{2})$")


# --------------------------------------------------------------------------- due dates

@dataclass(frozen=True)
class Due:
    when: date
    title: str
    kind: str          # insurance | card | other
    key: str           # stable identity -> stable event ids


def _policy_title(entry) -> tuple[str, str]:
    vals = [str(v.value) for v in getattr(entry, "values", []) or []]
    if getattr(entry, "type", "") == "policy" and vals:
        return f"{vals[0]} insurance renewal" + (f" - {vals[1]}" if len(vals) > 1 else ""), "insurance"
    if hasattr(entry, "narration"):
        return entry.narration or entry.payee or "Payment due", "other"
    if hasattr(entry, "comment"):
        return entry.comment, "other"
    return f"{type(entry).__name__} due", "other"


def ledger_dues(entries) -> list[Due]:
    out = []
    for e in entries:
        meta = getattr(e, "meta", None) or {}
        whens = []
        if "due" in meta:
            v = meta["due"]
            try:
                whens.append(v if isinstance(v, date) else parse_date(str(v)))
            except ValueError:
                print(f"warning: {meta.get('filename')}:{meta.get('lineno')}: due {v!r} is not a date",
                      file=sys.stderr)
        for tag in getattr(e, "tags", None) or ():
            m = DUE_TAG.match(tag)
            if m:
                try:
                    whens.append(parse_date(m.group(1)))
                except ValueError:
                    print(f"warning: {meta.get('filename')}:{meta.get('lineno')}: #{tag} is not a real date",
                          file=sys.stderr)
        for when in whens:
            title, kind = _policy_title(e)
            key = f"{Path(str(meta.get('filename', ''))).name}:{title}:{when}"
            out.append(Due(when, title, kind, key))
    return out


def next_card_due(due_day: int, today: date) -> date:
    def on(y, m):
        return date(y, m, min(due_day, calendar.monthrange(y, m)[1]))
    d = on(today.year, today.month)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = on(y, m)
    return d


def card_dues(cfg: dict, today: date) -> list[Due]:
    out = []
    for sid, card in sources(cfg, kind="card").items():
        if card.get("due_day"):
            when = next_card_due(int(card["due_day"]), today)
            out.append(Due(when, f"Credit card payment due - {sid}", "card", f"card:{sid}:{when}"))
    return out


@dataclass(frozen=True)
class Reminder:
    event_id: str
    on: date
    due: Due
    days_before: int

    @property
    def summary(self) -> str:
        return f"[{self.days_before}d] {self.due.title} (due {self.due.when:%d %b %Y})"


def plan_reminders(entries, cfg: dict, today: date, horizon: int) -> list[Reminder]:
    gcfg = cfg.get("google", {})
    offsets = gcfg.get("reminder_offsets", [7, 2])
    dues = {d.key: d for d in ledger_dues(entries) + card_dues(cfg, today)}.values()
    out = []
    for due in sorted(dues, key=lambda d: d.when):
        if not (today <= due.when <= today + timedelta(days=horizon)):
            continue
        for n in offsets:
            on = due.when - timedelta(days=n)
            if on < today:
                continue
            # Calendar ids allow [a-v0-9]; hex is a subset.
            eid = "pf" + hashlib.sha1(f"{due.key}|{n}".encode()).hexdigest()[:30]
            out.append(Reminder(eid, on, due, n))
    return out


# --------------------------------------------------------------------------- auth

def credentials(allow_browser: bool = False):
    import keyring
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    cfg = load_config().get("google", {})
    token = keyring.get_password(KEYCHAIN_SERVICE, TOKEN_ITEM)
    creds = Credentials.from_authorized_user_info(json.loads(token), SCOPES) if token else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            keyring.set_password(KEYCHAIN_SERVICE, TOKEN_ITEM, creds.to_json())
            return creds
        except Exception as ex:
            if not allow_browser:
                raise PFError(f"Google sign-in expired ({ex}) - run: google_sync.py auth")
    if not allow_browser:
        raise PFError("no Google sign-in stored - run: google_sync.py auth")
    client = keyring.get_password(cfg.get("client_keychain_service", "salary-invoice"),
                                  cfg.get("client_keychain_item", "calendar-client"))
    if not client:
        raise PFError("no OAuth client in the Keychain - set [google].client_keychain_service/item "
                      "to a stored desktop-app client JSON")
    flow = InstalledAppFlow.from_client_config(json.loads(client), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="Opening browser for Google sign-in...\n")
    keyring.set_password(KEYCHAIN_SERVICE, TOKEN_ITEM, creds.to_json())
    return creds


def service(api: str, version: str):
    from googleapiclient.discovery import build
    return build(api, version, credentials=credentials(), cache_discovery=False)


# --------------------------------------------------------------------------- calendar

def event_body(r: Reminder) -> dict:
    return {
        "id": r.event_id,
        "summary": r.summary,
        "description": f"{r.due.title}\nDue: {r.due.when.isoformat()}\nKind: {r.due.kind}\n"
                       f"Created by personal-finance/google_sync.py",
        "start": {"date": r.on.isoformat()},
        "end": {"date": (r.on + timedelta(days=1)).isoformat()},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 9 * 60}]},
        "transparency": "transparent",
        "status": "confirmed",   # restores an event the user deleted (its id still exists)
    }


def push_reminders(reminders: list[Reminder], calendar_id: str, svc=None) -> dict:
    from googleapiclient.errors import HttpError

    svc = svc or service("calendar", "v3")
    counts = {"created": 0, "updated": 0}
    for r in reminders:
        body = event_body(r)
        try:
            svc.events().insert(calendarId=calendar_id, body=body).execute()
            counts["created"] += 1
        except HttpError as ex:
            if ex.resp.status != 409:        # 409 = this id exists already -> refresh it
                raise PFError(f"Calendar refused {r.summary!r}: {ex}")
            try:
                svc.events().update(calendarId=calendar_id, eventId=r.event_id, body=body).execute()
            except HttpError as ex2:
                raise PFError(f"Calendar refused to refresh {r.summary!r}: {ex2}")
            counts["updated"] += 1
    return counts


# --------------------------------------------------------------------------- drive backup

def backup_files(tmpdir: Path) -> list[tuple[str, Path]]:
    """(name in Drive, local path). sidecar.db is snapshotted with SQLite's backup API."""
    files = [(str(p.relative_to(ledger_dir().parent)), p) for p in sorted(ledger_dir().rglob("*.beancount"))]
    db = sidecar_path()
    if db.exists():
        snap = tmpdir / "sidecar.db"
        src, dst = sqlite3.connect(db), sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        files.append(("data/sidecar.db", snap))
    return files


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def push_backup(files: list[tuple[str, Path]], folder_name: str, svc=None) -> dict:
    from googleapiclient.http import MediaFileUpload

    svc = svc or service("drive", "v3")
    safe = folder_name.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    found = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    folder = found[0]["id"] if found else svc.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute()["id"]
    existing = {f["name"]: f for f in svc.files().list(
        q=f"'{folder}' in parents and trashed = false",
        fields="files(id,name,md5Checksum)", pageSize=1000).execute().get("files", [])}
    counts = {"uploaded": 0, "updated": 0, "unchanged": 0}
    for name, path in files:
        media = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=False)
        cur = existing.get(name)
        if cur and cur.get("md5Checksum") == md5(path):
            counts["unchanged"] += 1
        elif cur:
            svc.files().update(fileId=cur["id"], media_body=media).execute()   # Drive keeps revisions
            counts["updated"] += 1
        else:
            svc.files().create(body={"name": name, "parents": [folder]}, media_body=media,
                               fields="id").execute()
            counts["uploaded"] += 1
    return counts


# --------------------------------------------------------------------------- CLI

def cmd_auth(_args) -> int:
    credentials(allow_browser=True)
    print("Google sign-in stored in the Keychain (personal-finance / google-token)")
    return 0


def cmd_remind(args) -> int:
    cfg = load_config()
    today = parse_date(args.date) if args.date else date.today()
    horizon = args.horizon or cfg.get("google", {}).get("horizon_days", 60)
    entries, _, _ = ledger_io.load()
    plan = plan_reminders(entries, cfg, today, horizon)
    for r in plan:
        print(f"{r.on}  {r.summary}")
    if not plan:
        print(f"no due dates in the next {horizon} days")
        return 0
    if not args.apply:
        print(f"\n{len(plan)} reminder(s) planned - re-run with --apply to write them to Google Calendar")
        return 0
    counts = push_reminders(plan, cfg.get("google", {}).get("calendar_id", "primary"))
    print(f"calendar: {counts['created']} created, {counts['updated']} refreshed")
    return 0


def cmd_backup(args) -> int:
    cfg = load_config()
    folder = cfg.get("google", {}).get("drive_folder", "personal-finance-backup")
    with tempfile.TemporaryDirectory() as tmp:
        files = backup_files(Path(tmp))
        for name, path in files:
            print(f"{name:<40} {path.stat().st_size:>10,} bytes")
        if not args.apply:
            print(f"\n{len(files)} file(s) -> Drive folder {folder!r}; re-run with --apply to upload")
            return 0
        counts = push_backup(files, folder)
    print(f"drive: {counts['uploaded']} new, {counts['updated']} updated, {counts['unchanged']} unchanged")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    r = sub.add_parser("remind")
    r.add_argument("--apply", action="store_true")
    r.add_argument("--horizon", type=int)
    r.add_argument("--date")
    b = sub.add_parser("backup")
    b.add_argument("--apply", action="store_true")
    args = p.parse_args()
    setup_logging(args.verbose)
    return {"auth": cmd_auth, "remind": cmd_remind, "backup": cmd_backup}[args.cmd](args)


if __name__ == "__main__":
    run_cli(main)
