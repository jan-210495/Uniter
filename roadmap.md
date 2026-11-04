# Uniter Project Roadmap

## Project
- **Repo:** `/home/jabboud/projects/Uniter`
- **Stack:** FastAPI + SQLAlchemy async + SQLite, Python 3.12 venv at `.venv/`
- **Run:** `source .venv/bin/activate && python3 -m pytest backend/` and `uvicorn backend.main:app` (port 8000)
- **GitHub:** https://github.com/jan-210495/Uniter

## Architecture
- **Pages:** `/` (root), `/pos` (point of sale / USSD preview), `/settlement` (debt payments), `/customers` (CRUD + debt)
- **API:** customers, operators/balance, operators/topup, transactions, debt/ledger, debt/payment, currency/denomination, settings/commission, register
- **Models:** Customer, Operator, Service, Transaction, DebtLedger, OperatorBalance, OperatorTopup, CashRegister, CurrencyDenomination, Setting

## Phase 6 Roadmap

| # | Slice | Branch | Prompt file | Status |
|---|-------|--------|-------------|--------|
| 1 | Customers page (CRUD + debt balance) | `phase-6-customers-page` | slice-1 | ✅ Done (main) |
| 1.b | Remove operator balance UI from customers page | — | slice-1b | ✅ Done (`86db600`) |
| 2 | /pos improvements (auto-recognize + search customer) | `slice-2-pos-customer-recognition` | slice-2 | ✅ Done (`e152107` + `673d7df` fix) |
| 3 | Transactions list (operator/service/status filters) | `phase-6-transactions-list` | slice-3 | ✅ Done (`e7bb048`) |
| 4 | Settings page UI (commission / denomination / codes) | `phase-6-settings-page` | slice-4 | ✅ Done (`b8f7442`) |
| 5 | Balance topup (per-operator remaining balance) | `phase-6-balance-topup` | slice-5 | ✅ Done (`22a647c`) |

## Phase 6 Summary

All five slices complete and merged into `main`:
- Slice 1: Customers page (CRUD + debt) — `d551b69`
- Slice 1.b: Remove operator balance UI from customers page — `86db600`
- Slice 2: /pos customer recognition — `e152107` + `673d7df` fix
- Slice 3: Transactions list with filters — `e7bb048`
- Slice 4: Settings page UI — `b8f7442`
- Slice 5: Balance topup page at /topup — `22a647c`

Test suite: **84 passed, 2 pre-existing failures** (`test_models_and_relationships`, `test_old_unique_constraint_requires_migration`).

## Pre-existing Test Failures (not caused by current work)
- `backend/test_database.py::test_models_and_relationships` — missing `currency_denomination` table in test DB
- `backend/test_seed.py::test_old_unique_constraint_requires_migration` — legacy services unique constraint

## Local Test Data
- `example.db` (gitignored, not tracked): seeded with 5 customers — Ahmad (7,500 debt), Rami (20,000 debt), Leila/Omar/Sara (0 debt); operators MTN & Syriatel; open cash register with 50,000 opening balance
- Run: `UNITER_DB=example.db uvicorn backend.main:app`

## Rules & Constraints
- Neutral names only (Ahmad, Rami) — never use political figure names as examples
- No secrets, passwords, API keys, or hardcoded credentials in code, Git, logs, or chat
- Bilingual en/ar, RTL support, Noto Sans Arabic with Tahoma fallback
- Monochrome Precision styling (navy/blue/slate), not dark control-room or decorative premium
- Snobby writes prompts and reviews; arena/Codex agents implement. Snobby does not directly implement project work going forward.
- Agents must commit and push completed work to the branch immediately after verification — never leave finished work uncommitted.

## Phase 7 Plan (refinements)

All slices complete:

| # | Slice | Focus | Status |
|---|-------|-------|--------|
| 7.1a | USSD structure + ProfitLedger + transaction flow (backend) | ✅ Done (`685f157`) |
| 7.1b | Settings domain fields (MTN_DOMAIN / SYRIATEL_DOMAIN) + PUT endpoints | ✅ Done (`779307f`) |
| 7.1c | POS auto-operator selector + auto-register customer | ✅ Done (`b86c506`) |
| 7.2 | Mobile side menu (burger) + timezone selector | Frontend UI | ✅ Done (`6e1a3de`) |
| 7.3 | Global search bar + operator transaction subpage | Frontend + subpage | ✅ Done (`0477b6f`) |
| 7.4 | Stats page (profit, transaction volume over time) | Capstone | ✅ Done (`6e946bc`) |
| 7.5 | POS & header refinements (USSD generation, transaction flow, prefix swap, mobile header, homepage redirect) | ✅ Done (`c36bdef`) |

## Phase 6 Summary

All five slices complete and merged into `main`:
- Slice 1: Customers page (CRUD + debt) — `d551b69`
- Slice 1.b: Remove operator balance UI from customers page — `86db600`
- Slice 2: /pos customer recognition — `e152107` + `673d7df` fix
- Slice 3: Transactions list with filters — `e7bb048`
- Slice 4: Settings page UI — `b8f7442`
- Slice 5: Balance topup page at /topup — `22a647c`

Test suite: **213 passed** (after slice 7.5), no regressions.

## Open Questions
- Arena prompt files for slices 2–5 recreated in the repo (one per slice).
- USSD code structure saved at `ussd-structure.md` — MTN: `*MODE*DOMAIN*NUM*AMOUNT#` (MODE 150=prepaid/154=postpaid, DOMAIN=65477); Syriatel: `*150*1*DOMAIN*MODE*AMOUNT*NUM*NUM#` (DOMAIN=67858, MODE 1=prepaid/5=postpaid). Implemented in 7.1a/7.5.
