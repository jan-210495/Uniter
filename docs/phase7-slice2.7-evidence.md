# Slice 2.7 — voucher catalog and Syriatel USSD serialization

## Scope and base

Based on verified origin/main `feb4255c9e36cf41a69cc67bd55891eee2ca773b`, in isolated task worktree `/home/jabboud/projects/Uniter-slice2.7`, branch `task/phase7-slice2.7-vouchers`.

Both user-supplied UTF-8 voucher files were read and compared line-for-line (ignoring blank lines) with the implementation brief before edits: MTN 46 unique entries; Syriatel 48 unique entries; exact order and spelling matched. The input files are not edited or committed.

Only production changes: exact string catalogs in `backend/templates/pos.html`, and Syriatel's amount segment in the shared `backend/seed.py::generate_ussd`. Both preview and commit call that generator. MTN serialization is unchanged. Settings formulas and domains are unchanged; their generator tests now expect the new Syriatel units.

## Display versus USSD

Syriatel voucher `432.69` remains that exact input string, displays `432.69` in the amount summary, and posts/stores `43269` cents. Only its generated USSD amount segment is `43269`. Likewise `1.92 -> 192`, `4.80 -> 480`, `62.50 -> 6250`, and `3846.15 -> 384615`. Both prepaid mode 1 and postpaid mode 5 preserve domain, segment placement, and duplicated phone.

The rule applies to custom amounts too: `12.34 -> 1234`, `12.3 -> 1230`, and whole `12 -> 1200`. This serializes integer cents, not arbitrary removal of a dot from unpadded custom input. No float conversion is used. Existing input parsing accepts at most two decimal places and rejects unsafe JavaScript integers; API bounds remain unchanged. Existing summary formatting may omit trailing zeros while voucher buttons/input strings retain them.

Accounting still uses unchanged cents and nearest-even commission rounding (e.g. 6250 cents at 5% yields 312 commission cents). No schema, migration, historical-row rewrite, release metadata, authentication, or tenancy changes are included.

## Executed verification

Python 3.12.16, Node v24.15.0. The worktree used the existing Python environment via a temporary local symlink, without package installs or changes to that environment.

- Focused suite: `test_voucher_serialization.py`, `test_pos_vouchers.py`, `test_ussd.py`, `test_ussd_profit.py`, `test_money_boundaries.py`, `test_pos_ussd_result.py`: **147 passed**, 77 dependency deprecation warnings.
- `.venv/bin/python -m pytest backend/ --tb=short -q`: **600 passed**, 389 dependency deprecation warnings, 28.22 seconds.
- `.venv/bin/python -m compileall -q backend`: passed.
- `node --check`: passed for rendered EN/AR POS scripts, all six backend DOM harness scripts, `backend/static/install.js`, and `backend/static/navigation.js`.
- `git diff --check`: passed.

New tests exercise exact EN/AR HTTP-rendered catalogs, every voucher's generator output for both service modes, representative voucher and custom-amount summaries/payloads, API validation, identical preview/commit codes, persisted amounts/codes, preview without side effects, idempotent commit, exact operator debit and profit ledger entries, customer pricing, and failure refund. Existing Copy/Dialer DOM tests, denomination, migration, navigation, Settings/PWA and accounting tests pass in the full suite.

Tests use disposable temporary databases. The main checkout, user inputs, and user databases were not changed. No server was started, stopped, or contacted. A final read-only socket check found no listener on `127.0.0.1:56246`; the brief's review server was therefore not available for verification.

## Review limits

No physical browser/device or real carrier dialer/USSD was used. Rendered JavaScript behavior is covered by Node DOM doubles and APIs by test clients; no carrier certification is claimed. Work remains unmerged for independent review. No tag or release was created.
