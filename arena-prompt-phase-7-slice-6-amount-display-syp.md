Phase 7 Slice 6 — Amount display in SYP units (no raw cents)

Project: /home/jabboud/projects/Uniter
Branch: arena/46c0244b-uniter (this session is pinned to that branch — commit directly to it).
arena does NOT have access to /home/jabboud/projects/friend-co/ — describe behavior inline.

Stack: FastAPI + SQLAlchemy async + SQLite, Python 3.12 venv at `.venv/`
Run: `source .venv/bin/activate && python3 -m pytest backend/` and `uvicorn backend.main:app` (port 8000)

## Goal

Amounts are stored internally in cents (e.g. 200 SYP = 20000 cents) but displayed to users as raw cents (e.g. `20000` instead of `200`). Fix every place amounts are rendered so they show in SYP units: divide cents by 100, format with `toLocaleString('en-US')`, no trailing `.00` for whole amounts, two decimals only when needed (e.g. `200.50` for 20050 cents). Currency suffix `ل.س` (ar) / `SYP` (en) stays as-is.

No backend changes needed — the DB and API already store/return cents correctly. This is a frontend display fix only across the templates.

## Scope — fix amount display in SYP everywhere

### `backend/templates/transactions.html`
- `formatCents` (line ~157): change to `formatAmount` that does `cents / 100`, formats with `toLocaleString('en-US')`, strips trailing `.00` for whole numbers, keeps decimals when needed.
- Applied at: per-row amount (line ~328), transaction amount cell (line ~367), running total (lines ~392, ~395).

### `backend/templates/operator.html`
- Per-row amount cell (line ~70): `{{ "{:,}".format(transaction.amount_cents) }}` → convert to SYP units server-side (`amount_cents / 100`) and format without trailing zeros.

### `backend/templates/settlement.html`
- Amount cell (line ~107): `${entry.amount_cents}` → SYP units.

### `backend/templates/stats.html`
- `formatCents` (line ~142): change to `formatAmount` SYP conversion.
- Applied at: stat cards profit/commission/volume (lines ~176-179), per-operator profit/volume, per-status volume, daily series amounts (lines ~206-229).

### `backend/templates/customers.html`
- `formatCents` (line ~281): change to `formatAmount` SYP conversion.
- Applied at: customer info balance/debt (line ~537), total balance/debt cards (lines ~385-386), debt ledger amounts (lines ~576-578).
- Server-side `"{:,}".format(total_balance_cents)` (lines ~25, ~29) → SYP units.

### `backend/templates/topup.html`
- `formatCents` (line ~74): change to `formatAmount` SYP conversion.
- Server-side `"{:,}".format(operator.balance_cents)` (line ~35) → SYP units.

### `backend/templates/pos.html`
- `formatMoney` (line ~256-257) already divides correctly — verify it strips trailing `.00` and uses SYP suffix. Customer info balance/debt display (lines ~272, ~274) uses `formatMoney` — make consistent.
- The `formatCents` at line ~199-200 already divides by 100n correctly — keep, but ensure consistent with the shared `formatAmount`.

## Shared behavior (define once, reuse)

Add a single `formatAmount(cents)` helper per template (or a shared snippet) that:
1. `syp = cents / 100`
2. `integerPart = Math.floor(syp)`, `fraction = syp - integerPart`
3. If fraction is 0 → `integerPart.toLocaleString('en-US')` (no decimals)
4. Else → `(syp / 100).toFixed(2)` then strip unnecessary trailing zeros, keep at least one decimal? No — user wants `200.50` for 20050 cents, `200` for 20000 cents. So: format to 2 decimals, then strip trailing zeros but keep at least 1 decimal digit if fractional. Actually simplest: `syp.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 })` gives `200` and `200.5`. That works.
5. Append currency suffix `ل.س` (ar) / `SYP` (en) as a separate span, matching existing styling.

## Tests (`backend/test_amount_display.py` or extend template tests)

These are template-rendering tests (no DB needed for pure display):

- Template `formatAmount(20000)` → `"200"` (en) / `"200"` (ar)
- Template `formatAmount(20050)` → `"200.5"` (en/ar)
- Template `formatAmount(5000)` → `"50"` (en/ar)
- Template `formatAmount(0)` → `"0"` (en/ar)
- Template `formatAmount(1234567)` → `"12,345.67"` (en/ar)
- `transactions.html` renders amount cell with SYP value not raw cents
- `customers.html` renders balance/debt in SYP units
- `stats.html` renders stat cards in SYP units
- `topup.html` renders operator balance in SYP units
- `operator.html` renders transaction amount in SYP units
- `settlement.html` renders amount in SYP units
- `pos.html` customer info balance/debt in SYP units
- All templates render `ل.س` (ar) / `SYP` (en) suffix next to amounts
- Both languages render 200 status

## Verification

- `source .venv/bin/activate && python3 -m pytest backend/` → all tests pass (was 213, should stay green).
- `uvicorn backend.main:app` →
  - `/transactions` shows `200` SYP not `20000` for a 20000-cent transaction
  - `/customers` shows balance/debt in SYP
  - `/stats` shows profit/volume in SYP
  - `/topup` shows operator balance in SYP
  - `/operators/mtn` shows transaction amounts in SYP
  - `/settlement` shows amounts in SYP
  - `/pos` customer info shows balance/debt in SYP
  - en/ar both render correctly with proper currency suffix
- **COMMIT AND PUSH** when done (directly to `arena/46c0244b-uniter`).

## Constraints

- Do not change backend, DB schema, or API response shapes — storage stays in cents.
- Do not change the POS input behavior (entering `200` still stores 20000 cents — that's correct).
- Only change *display* formatting: cents → SYP units.
- Neutral names only (Ahmad, Rami) in tests and example data.
- No secrets/hardcoded credentials.
- Bilingual en/ar, RTL-aware.
- **COMMIT AND PUSH** completed work immediately after verification.

## Deliverable

Report back: slice 7.6 verified correct, committed, and pushed — or list issues found and fixed.
