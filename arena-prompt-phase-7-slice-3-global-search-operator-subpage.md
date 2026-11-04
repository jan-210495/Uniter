Phase 7 Slice 3 — Global search bar + operator transaction subpage

Project: /home/jabboud/projects/Uniter
Branch: create `phase-7-global-search-operator-subpage` from `arena/46c0244b-uniter` (current HEAD on origin, includes slices 7.1a–7.2 commit 6e1a3de).
arena does NOT have access to /home/jabboud/projects/friend-co/ — describe behavior inline.

Stack: FastAPI + SQLAlchemy async + SQLite, Python 3.12 venv at `.venv/`
Run: `source .venv/bin/activate && python3 -m pytest backend/` and `uvicorn backend.main:app` (port 8000)

## Goal

Two frontend-facing additions shared across the app:

1. **Global search bar** in `base.html` header — a single search input that debounces (200-250ms, `AbortController`) to a new `GET /api/search?q=` endpoint and shows a dropdown of matching customers, transactions, and operators. Clicking a result navigates to the right page.
2. **Operator transaction subpage** — a dedicated HTML page per operator (`/operators/{operator_code}`) showing the operator's balance, recent transactions, and a top-up action. Linked from the global search, the settings page operator list, and the transactions page operator filter.

No changes to existing API response shapes. Keep everything bilingual (en/ar), RTL-aware, Monochrome Precision styling (navy/blue/slate).

## Backend (`backend/main.py`)

### New endpoint: `GET /api/search?q=`

Query params: `q` (min_length 1, max_length 255). Returns a JSON object:

```json
{
  "customers": [{"id": int, "phone": str, "name": str, "balance_cents": int, "debt_cents": int}],
  "transactions": [{"id": int, "reference": str, "amount_cents": int, "status": str, "customer_phone": str, "customer_name": str, "operator_code": str, "operator_name": str}],
  "operators": [{"code": str, "name": str, "balance_cents": int}]
}
```

- Customers: case-insensitive match on `name` OR `phone` fragment (ilike `%q%`), limit 10, reuse `list_customers_with_debt`-style aggregation for debt.
- Transactions: match on `reference` (ilike `%q%`) OR customer phone fragment, limit 10, newest first, join customer/operator/service for labels.
- Operators: match on `code` OR `name` fragment, limit 10, include current balance from `OperatorBalance` (0 when no row).
- Empty `q` → return empty arrays, 200.

### New route: `GET /operators/{operator_code}` (HTML page)

- Look up `Operator` by code; 404 if not found or inactive.
- Render `backend/templates/operator.html` with context `{lang, nav_page: None, operator, balance_cents, transactions}`.
- `transactions`: latest 20 transactions for that operator (join through Service→Operator), newest first, same fields as `TransactionListItem`.
- `balance_cents`: from `OperatorBalance` for that operator, 0 when no row.
- Bilingual page title and labels.

### No changes to existing endpoints

`GET /api/transactions`, `GET /api/operators/balance`, `GET /api/customers/search` all stay as-is.

## Frontend

### `backend/templates/base.html` — global search bar

- Add a search input in the header navigation area (desktop: inline next to language switcher; mobile: inside the burger drawer from slice 7.2).
- Placeholder: "Search customers, transactions, operators" / "البحث عن العملاء والمعاملات والمشغّلين".
- Debounced fetch (200-250ms) with `AbortController` to `/api/search?q=`.
- Results dropdown (`role="listbox"`, `aria-expanded` toggling) grouped by section: Customers, Transactions, Operators. Each result shows a label and a subtitle (e.g. customer phone, transaction reference + customer, operator balance).
- Clicking a result:
  - Customer → navigate to `/customers` (the customers list; could also highlight via query param but keep simple: just navigate to /customers)
  - Transaction → navigate to `/transactions` with `?customer_phone=` or `?operator_code=` as appropriate to surface it
  - Operator → navigate to `/operators/{code}`
- Escape closes the dropdown; clicking outside closes it; arrow-key navigation + Enter to select.
- `aria-expanded` / `aria-activedescendant` / `role="listbox"` / `role="option"` for accessibility.
- RTL-aware: input `dir="auto"` or appropriate direction; dropdown aligned to start edge.

### `backend/templates/operator.html` — operator subpage

- Extends `base.html`.
- Header: operator name + code badge, balance ("رصيد: X ل.س" / "Balance: X SYP"), top-up button linking to `/topup`.
- Transactions table: same 8 columns as `/transactions` (Reference, Date/time, Customer, Operator, Service, Amount, Status, USSD), filtered to this operator, newest first.
- Empty state when no transactions.
- Bilingual en/ar, RTL-aware, Monochrome Precision cards/tables matching `transactions.html` styling.

### Linking

- On `/settings`, each operator in the operator list gets a link to `/operators/{code}`.
- On `/transactions`, the operator filter select keeps working; optionally the operator name in table rows links to `/operators/{code}` (nice-to-have, not required).
- Global search operator results link directly to `/operators/{code}`.

## Tests (`backend/test_global_search.py` and `backend/test_operator_page.py`)

### Search tests
- `GET /api/search?q=` with missing/empty `q` → 200, `{"customers": [], "transactions": [], "operators": []}`.
- Search by customer name fragment returns matching customers with balance/debt.
- Search by customer phone fragment returns matching customers.
- Search by transaction reference returns matching transactions with operator/customer labels.
- Search by operator code or name returns matching operators with balance.
- Mixed query returns results across all three sections.
- Results limited to 10 per section.
- `GET /search` page? No — API only.

### Operator page tests
- `GET /operators/mtn` → 200, renders operator.html, shows MTN name/code/balance.
- `GET /operators/syriatel` → 200, same for Syriatel.
- `GET /operators/unknown` → 404.
- `GET /operators/mtn` in en and ar (200, correct `lang`/`dir`).
- Page contains operator name, balance, transactions table header (8 columns).
- Transactions on the page are filtered to that operator only.

### Template markup tests (like existing `test_transactions_template_markup`)
- base.html renders search input + dropdown markup in en and ar.
- operator.html renders operator header, balance, table with 8 columns.

## Verification

- `source .venv/bin/activate && python3 -m pytest backend/` → all tests pass (was 165, should stay green).
- `uvicorn backend.main:app` → 
  - GET `/api/search?q=ahmad` returns customers/transactions/operators JSON.
  - GET `/operators/mtn` and `/operators/syriatel` → 200 en/ar.
  - GET `/operators/unknown` → 404.
  - Global search input present on `/pos`, `/settings`, `/customers` in both languages.
  - Operator subpage linked from search results and settings.
- **COMMIT AND PUSH** when done.

## Constraints

- Do not change existing API response shapes except adding the new `GET /api/search?q=` endpoint and the new `/operators/{operator_code}` HTML route.
- Neutral names only (Ahmad, Rami) in tests and example data.
- No secrets/hardcoded credentials.
- Bilingual en/ar, RTL-aware.
- **COMMIT AND PUSH** completed work immediately after verification.

## Deliverable

Report back: slice 7.3 verified correct, committed, and pushed — or list issues found and fixed.
