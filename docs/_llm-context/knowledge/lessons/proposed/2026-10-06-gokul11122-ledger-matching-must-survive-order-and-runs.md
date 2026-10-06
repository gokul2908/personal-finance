---
id: 2026-10-06-gokul11122-ledger-matching-must-survive-order-and-runs
author: gokul11122
date: 2026-10-06
type: lesson
evidence:
  - tests/test_review_regressions.py (one test per confirmed bug)
  - scripts/importer.py build(), recorded_index(), open_clearing_items()
tags:
  - "beancount"
  - "dedup"
  - "import-order"
supersedes: null
---
**Mistake**: The importer matched statement lines using rules that only looked at one batch: a keyword on ONE side to pair transfers, Clearing only for card credits, occurrence counters across the whole run, and matched hand entries forgotten after each run. 43 green tests on one happy-path demo (all statements imported together, receipts before statements) missed 15 real double-count and drop bugs that a fresh reviewer proved in ~8 minutes.
**Pattern**: Any rule that matches money across sources must hold in EVERY import order and across separate runs. Make matches durable in the ledger itself (a note carrying matched_entry, or Clearing that is netted per account) and require evidence on BOTH sides before pairing. Test by permuting order and splitting runs: bank-then-card, card-then-bank, receipt-after-statement, the same PDF twice, and the same amount in two months.
