# Personal finance - Beancount for Self, Wife and Mom

Plain-text double-entry books for three people in one ledger, fed by statement PDFs,
grocery receipts and a handful of commands. Browse everything in Fava.

```
config/      config.example.toml (copy to config.toml), canonical_items.toml
data/        raw_statements/  raw_receipts/  sidecar.db  nudges/
ledgers/     main.beancount -> commodities, self, wife, mom, prices; templates/
scripts/     importer  receipt_engine  milestones  analytics  google_sync  ai_nudge  prices
             common (config/secrets/Ollama)  ledger_io (safe append)  sidecar (SQLite)  demo
tests/       65 tests against a synthetic demo tree (incl. one per review finding)
```

## Setup

```bash
cd ~/work/personal-finance
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/config.example.toml config/config.toml      # then edit sources, last4, rules
.venv/bin/python scripts/common.py secret set self.name   # Keychain, never in files
.venv/bin/python scripts/common.py secret set self.dob    # YYYY-MM-DD
.venv/bin/python scripts/common.py secret status
.venv/bin/fava ledgers/main.beancount                     # http://localhost:5000
```

Ollama (for receipts and nudges): `brew install ollama && ollama pull gemma3:4b`, or point
`[ollama].host` at a cloud host and set `OLLAMA_API_KEY`.

## Monthly routine

```bash
S=scripts; PY=.venv/bin/python
$PY $S/receipt_engine.py ingest data/raw_receipts --paid-with Liabilities:Self:CC:HDFC-Regalia
$PY $S/milestones.py amazon-gc --amount 5000 --paid-with Liabilities:Self:CC:HDFC-Regalia --cashback 100
$PY $S/importer.py import --dry-run        # look first
$PY $S/importer.py import                  # all PDFs in data/raw_statements
$PY $S/milestones.py status
$PY $S/analytics.py wallet-drag ; $PY $S/analytics.py price-timeline Tomato
$PY $S/prices.py                            # AMFI NAVs + yfinance closes
$PY $S/google_sync.py remind --apply        # calendar reminders 7d and 2d before dues
$PY $S/google_sync.py backup --apply        # ledgers + sidecar.db to Drive
$PY $S/ai_nudge.py                          # weekly nudge from Gemma
```

Record receipts and gift-card loads **before** importing that month's statements: the
importer then recognises those card lines as already recorded instead of counting them twice.

## How the pieces decide things

**Passwords.** Each `[sources.*]` block names a parser; `[passwords]` lists the patterns
tried for it (`{name4_upper}{ddmm}` for HDFC, `{ddmmyyyy}{last4}` for SBI Card, ...),
filled from Keychain secrets. PDFs are decrypted in memory only. A failure names the
patterns tried and any missing secret, never a password. `--ask-password` prompts.

**Parsing.** One general line rule per card issuer (date ... amount [Cr]) with per-bank
options in `PROFILES` (`importer.py`), and a running-balance rule for savings accounts
that reads direction from how the balance moved. When a real statement parses 0 lines,
run `importer.py import FILE --dump-text --dry-run` and adjust that bank's profile.

**Duplicates.** Each line's fingerprint is stored as `import_id`; re-importing is a no-op.
A line matching a hand-entered transaction (receipt, gift-card load, split; anything
without an `import_id`, except opening balances) on the same account and amount within
±2 days is skipped, and a `note` records the match so that entry is used only once.
A receipt ingested after its statement attaches to the imported line instead.

**Transfers.** Same amount, opposite direction, different accounts, within ±2 days, and a
transfer keyword (NEFT/IMPS/RTGS/BBPS/payment received...; not UPI) on BOTH sides -> one transaction (`Assets:Self:HDFC -> Assets:Mom:Savings`),
tagged per direction from `[transfers.notes]`: `#gift` + s.56(2)(x) note to Mom,
`#spouse-transfer` + s.64(1)(iv) clubbing note to Wife. A transfer whose other half is
not imported yet - whichever statement comes first - waits in `Assets:<Entity>:Clearing`
and is closed off when it arrives. A line with more than one possible partner is flagged `!`.

**Split debits.** `importer.py split --from ACC --total N --part ACC=AMT ...` refuses
parts that do not add up. Template: `ledgers/templates/split_debit.beancount`.

**Receipts.** Gemma reads the photo into JSON; replies are parsed tolerantly and re-asked
with the specific problem (bad JSON, lines not adding up to the total). Items are
normalised (`Lion Dates 500g` -> Dates / Dry Fruits / Lion / 0.5 kg) by
`config/canonical_items.toml`, overridden by anything taught with `receipt_engine.py map`.
`receipt_engine.py review` lists what needs a human. The ledger gets one transaction
with `receipt_id:` metadata and link `^receipt-<id>`.

> Beancount tags cannot contain `:` (`#receipt_id:R1` is a syntax error), so the spec's
> `#receipt_id:<id>` and `#due:YYYY-MM-DD` are written as metadata / links / `#due-YYYY-MM-DD`.

**Due dates.** Any directive with `due: YYYY-MM-DD` (see the `custom "policy"` examples
in `self.beancount`) or a `#due-YYYY-MM-DD` tag, plus each card's `due_day`.

## Google

`google_sync.py auth` once (opens the browser; click Allow). It reuses the desktop OAuth
client stored for salary-invoice and keeps its own token in the Keychain
(`personal-finance` / `google-token`) with scopes `calendar.events` + `drive.file`.
The Drive API must be enabled in that Cloud project. `remind` and `backup` print a plan
unless given `--apply`.

## Demo and tests

```bash
.venv/bin/python scripts/demo.py demo        # synthetic family, encrypted PDFs, receipts
.venv/bin/python -m pytest -q tests
```

The demo's identities are fake (printed by `demo.py`); real statements were not used to
build the parsers, so the first real PDF per bank is the real test.
