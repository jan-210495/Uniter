# Uniter monetary and runtime audit

Scope: recheck recent release fixes; actual form inputs, commission, USSD generation, related debt/payment flow, and startup/build assumptions.
Baseline: main at 2e33776. Baseline suite: 232 passed, 166 warnings. This did NOT prove browser money flows.

## Contract
- All user-entered amounts represent new SYP. Top-up input 50,000 must add/display 50,000 SYP.
- Internal legacy *_cents fields are minor-unit storage, not a conversion to old SYP. Keep storage/API compatibility; consistently convert at input/output boundaries.
- Face-value charge 500 stays 500 in USSD. Commission at configured 5% is 25; customer total 525. Only face value consumes operator credit; commission is shop profit.
- No automatic existing-record rescaling, deletion, or production migrations. Past incorrectly recorded values need explicit reconciliation, not a blanket multiplier.

## Reproduced baseline defects
- /topup actual browser form: 50000 input -> DOM 500. Form submitted unscaled value into amount_cents; renderer divided by100.
- Debt form/payment use same unscaled payload issue; edit form prefills minor units directly.
- POS summary subtracts commission; endpoint checks face value then subtracts face value+commission. In disposable DB, charging exact available500SYP created -2500 minor-unit balance. Next transaction returns400 Insufficient operator balance, with no USSD returned.
- Settlement references #ledger-container tbody but no such markup exists. Payment succeeds, then null dereference blocks summary refresh. Explicit summary lookup shows correct persisted values but ledger fails.
- Port8000 existing PID86697 runs cwd /tmp/uniter-live, not checkout. Left untouched; fresh source != running source.

## Independent in-progress verification
Source branch: fix/syp-input-ussd-audit; no commits, pushes, tags, or releases performed.
Fresh isolated DB: /tmp/uniter-verify-ngf8Wy/verify.db
Fresh source server: http://127.0.0.1:56125 (PID549399), no reload. Initial baseline repro server56124 remains separate.
- Browser English MTN top-up50000 ->50,000.
- Browser POS500 -> *150*65477*0944123456*500#; commission25, customer total525.
- API operator balance4950000 minor units; page after navigation displays49,500.
- SQLite ProfitLedger: amount50000, commission2500, profit2500; integrity_check ok.
- Arabic RTL Syriatel top-up50000 ->50,000; POS500 -> *150*1*67858*1*500*0981234567*0981234567#; customer total525.
- Actual browser debt add500, unchanged edit prefills500 and preserves500. Payment200 persists debt300 (API30000 minor units).
- Settlement missing ledger markup repaired; Arabic browser payment100 then refreshed automatically: charges500, payments300, debt200, three ledger rows, no JS console errors.
- Exact remaining49,500SYP charge succeeded with USSD49500 and balance0. Next1SYP charge returned400 and did not create a transaction; balance never negative.
- All9 pages × English/Arabic returned200.
- Independent full suite after money and settlement fixes:270 passed,204 warnings,13.95 seconds; git diff --check clean. No tests skipped in that run.
- Browser vision/screenshot analysis blocked by SSL EOF; DOM and real form/API checks available. No visual-polish claim.

## Pending audit concerns
- requirements.txt lacks Pillow/pystray; optional imports silently disable tray. Windows runtime cannot be verified on Linux.
- Release workflow rebuilds only UniterSetup.exe; older standalone EXE/deb assets may be stale.
- Uvicorn CLI default remains8000 unless --port56123 supplied; main.py programmatic default does not configure CLI. Earlier advice that CLI inherited main.py port was incorrect.
- desktop and uvicorn default database locations differ; fresh unseeded uvicorn startup not automatically initialized. Independently reproduced fresh temporary DB: health200 but /pos500. Explicit seed required for fresh source-mode database until a separately tested startup fix is approved.
- uniter.spec ignored/untracked: clean checkout build may lack required spec. Independent audit worker checking.

## Operational discipline
User databases untouched. Existing port8000 unchanged. No release publication until runtime/regression verification and explicit approval of exact release target. Use a new version, not force-moving v1.0.0.
