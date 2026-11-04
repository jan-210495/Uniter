Phase 7 Slice 4 — Stats page (profit + transaction volume over time)

Project: /home/jabboud/projects/Uniter
Branch: create `phase-7-stats-page` from `arena/46c0244b-uniter` (current HEAD on origin, includes slices 7.1a–7.3 commit 0477b6f).
arena does NOT have access to /home/jabboud/projects/friend-co/ — describe behavior inline.

Stack: FastAPI + SQLAlchemy async + SQLite, Python 3.12 venv at `.venv/`
Run: `source .venv/bin/activate && python3 -m pytest backend/` and `uvicorn backend.main:app` (port 8000)

## Goal

Capstone slice: a stats dashboard page (`/stats`) showing profit and transaction volume over time, backed by a new `GET /api/stats/summary` JSON endpoint. No changes to existing API response shapes.

Data sources already in the DB:
- `ProfitLedger` (transaction_id, operator_code, amount_cents, commission_cents, profit_cents, created_at) — commission profit per transaction
- `Transaction` (status, amount_cents, created_at, ussd_string, reference, customer_id, service_id) — transaction volume
- `Operator` / `Service` — labels for breakdowns

## Backend (`backend/main.py`)

### New endpoint: `GET /api/stats/summary`

Query params:
- `period` (optional, default `7`, int 1–90): number of days for the daily time series
- `operator_code` (optional): filter everything to one operator

Response shape:

```json
{
  "period_days": 7,
  "operator_code": null,
  "totals": {
    "profit_cents": int,
    "commission_cents": int,
    "volume_cents": int,
    "transaction_count": int
  },
  "by_operator": [
    {"operator_code": str, "operator_name": str, "profit_cents": int, "commission_cents": int, "volume_cents": int, "transaction_count": int}
  ],
  "by_status": [
    {"status": str, "count": int, "volume_cents": int}
  ],
  "daily": [
    {"date": "YYYY-MM-DD", "profit_cents": int, "volume_cents": int, "transaction_count": int}
  ]
}
```

- `totals`: aggregate over ProfitLedger (profit/commission) and Transaction (volume, count) in the window. When `operator_code` is given, restrict to that operator.
- `by_operator`: one row per operator with profit, commission, volume, transaction count. Sorted by profit_cents desc.
- `by_status`: pending/completed/failed with count and volume.
- `daily`: one row per calendar day (UTC date of `created_at`) for the last `period_days` days, including zero days (dates with no activity show 0s). Sorted oldest → newest.
- Window: `created_at >= now - period_days days` using UTC. `period` clamped to 1–90; invalid → 422.
- No auth, no rate limiting.

### New route: `GET /stats` (HTML page)

- Render `backend/templates/stats.html` with context `{lang, nav_page: "stats"}`.
- Bilingual en/ar, RTL-aware.

### No changes to existing endpoints

`ProfitLedger`, `Transaction`, `/api/operators/balance`, `/api/transactions` all stay as-is.

## Frontend

### `backend/templates/base.html` — nav link

- Add a "Stats" / "الإحصائيات" link to the header nav (`href="/stats"`), with `aria-current="page"` when `nav_page == "stats"`. Desktop: inline with other links; mobile: inside the burger drawer from slice 7.2.

### `backend/templates/stats.html` — stats dashboard

- Extends `base.html`. Monochrome Precision cards (navy/blue/slate), matching `transactions.html`/`settings.html` styling.
- **Stat cards** (top row, responsive grid): Total Profit, Total Commission, Total Volume, Transaction Count — each with a large number and a label, bilingual.
- **Per-operator table**: operator, profit, commission, volume, transactions — same table styling as `/transactions` (8-column style not required; 5 columns here).
- **By-status summary**: pending / completed / failed with counts and volume (badges matching the colors used in `transactions.html`: amber/green/red).
- **Daily time series table**: date, profit, volume, transactions — one row per day in the period, oldest first.
- Empty states: "No data yet" when everything is zero.
- Numbers formatted with `toLocaleString('en-US')` for cents; currency suffix ل.س (ar) / SYP (en).
- Client-side fetch to `/api/stats/summary?period=7` on load, debounced not needed (single fetch). Show a loading state and error state via `role="status"` live region.
- `dir="auto"` or appropriate direction on numeric/label cells; RTL-aware layout.

### Linking

- `/stats` linked from base.html nav. No other page changes required.

## Tests (`backend/test_stats.py`)

- `GET /api/stats/summary` → 200 with the full shape above; defaults period to 7.
- `period=1` → `period_days: 1`, daily has exactly 1 entry.
- `period=90` → accepted; `period=91` → 422; `period=0` → 422; `period=abc` → 422.
- `operator_code=mtn` → only MTN rows in `by_operator`; totals restricted to MTN.
- `operator_code=unknown` → 200 with empty `by_operator` and zero totals (not 404 — operator filter is permissive).
- Profit/commission/volume/count match hand-computed values from inserted ProfitLedger + Transaction rows.
- `daily` includes zero-filled days for dates with no activity, sorted oldest→newest.
- `by_status` includes pending/completed/failed even when some have zero count.
- `GET /stats` → 200 en and ar; shows "الإحصائيات" / "Stats" in title and nav highlighted.
- Template markup test: stat cards present, per-operator table, daily table, by-status badges, fetch to `/api/stats/summary`.

## Verification

- `source .venv/bin/activate && python3 -m pytest backend/` → all tests pass (was 181, should stay green).
- `uvicorn backend.main:app` →
  - GET `/api/stats/summary` → 200 with correct shape; `?period=30` and `?operator_code=mtn` work; invalid period → 422.
  - GET `/stats` → 200 en/ar; nav link highlighted; stat cards, operator table, daily table render.
  - `/stats` link present in base.html nav on `/pos`, `/settings`, `/customers` in both languages.
- **COMMIT AND PUSH** when done.

## Constraints

- Do not change existing API response shapes except adding `GET /api/stats/summary` and the `/stats` HTML route.
- Neutral names only (Ahmad, Rami) in tests and example data.
- No secrets/hardcoded credentials.
- Bilingual en/ar, RTL-aware.
- **COMMIT AND PUSH** completed work immediately after verification.

## Deliverable

Report back: slice 7.4 verified correct, committed, and pushed — or list issues found and fixed.
