# Slice 3 — combined successful transaction history

## Baseline and scope

- Python: 3.12.8, `.venv/bin/python3`; all pinned versions in `requirements.txt`
  verified with `importlib.metadata` (the environment does not contain pip).
- Fetched origin before implementation. Starting HEAD and parent:
  `4111e61793260db6c4282edb7c3af2c99f8e559b`.
- Remote `refs/heads/release/1.0.5` and
  `refs/heads/release/1.0.5-slice-3-rollback` both verified at that SHA
  using `git ls-remote origin '*release/1.0.5*'`.
- Feature/push ref: `refs/heads/arena/48839799-uniter`.
- No schema, accounting, top-up creation, status/refund/delete implementation,
  release metadata, deployment, publication, or integration-branch changes.

## Listing contract

`GET /api/transaction-ledger` is an authenticated, read-only page projection.
The existing `/api/transactions` remains the sale-management API, including its
historical failed-sale access; it is not the new combined page endpoint.

- Sources: non-deleted successful Transaction sales, persisted OperatorTopup
  rows, and persisted SimLedger rows with `kind=topup`. No attempts, sale debits,
  or refunds are projected as top-ups. Persisted top-up rows mean successful
  credit, rather than an invented attempt status.
- `record_type`: `all` (default), `sale`, `topup`. Items carry `record_type`,
  `source`, source-local `id`, reference, timestamp, integer `amount_cents`,
  successful status and operator labels. Stable identity is `(source, id)`.
- SQL UNION ALL applies shared operator filtering, then one count and one
  chronological page ordered by timestamp DESC, source DESC, id DESC. Bounded
  detail queries hydrate only page IDs, preserving existing sale SIM masking.
- `limit`: default 50, range 1–200. `offset`: default 0, nonnegative.
- `service_code`, `performed_by`, and `customer_phone` require explicit
  `record_type=sale`; otherwise 422. The UI disables and clears these controls
  outside Sales only, with translated help text.
- `status=failed` returns zero items/count. Invalid statuses are 422. The page
  offers only successful status. Failed sales never belong in the combined view.
- Original sale name snapshot, phone, service, performer, USSD and permitted SIM
  details remain. Operator top-ups have null performer fields: no invented user.
  SIM top-ups use stored attribution. Private/archived-shared SIM names and IDs
  follow sale-list visibility rules. No SIM phone, idempotency key or secrets
  are returned in top-up objects.
- Top-ups have translated type badges and no sale actions. Their DOM IDs are
  source-qualified; both action handlers additionally require sale type.
  Marking a sale failed retains the existing endpoint/refund behavior, then
  removes it from the successful-only page. Delete API guards remain unchanged;
  failed-row deletion is intentionally no longer offered in this page.
- No combined money total is shown: adding top-up funding to sales could imply
  revenue. Each amount keeps the existing display-only SYP conversion.

## Verification

Focused command (exit 0):

```text
python3 -m pytest backend/test_transaction_ledger.py backend/test_transactions_list.py backend/test_transactions_actions.py backend/test_transaction_attribution.py backend/test_topup.py backend/test_operator_sims.py backend/test_sim_balances.py backend/test_transaction_status.py backend/test_operator_credit_refund.py backend/test_auth.py backend/test_auth_access.py backend/test_navigation.py --tb=short -q
309 passed, 370 warnings in 106.91s (0:01:46)
```

Full required command (exit 0):

```text
python3 -m pytest backend/ --tb=short -q
863 passed, 708 warnings in 218.38s (0:03:38)
```

Warnings are dependency deprecations (FastAPI lifecycle, Starlette/AnyIO,
HTTPX TestClient), not failures.

New tests cover both persisted top-up sources, source-local identity, integer
money, successful-only status, explicit type/sales-only filtering, operator
filtering across types, same-time cross-source ordering, count and complete
page traversal without loss/duplication, idempotent top-up replay, unchanged
stored balances/ledgers/sales/profit rows, seller SIM privacy, authentication,
CSRF, translated type controls/RTL/pagination and no top-up sale buttons.
Existing sale-name snapshot, attribution, failed-status/refund/delete tests
remain; old UI failed-row removal assertions were replaced because those rows
are now intentionally absent, not because the server guards were relaxed.

## Review and limitations

- Reviewed the complete changed API/template/harness/test diff; `git diff --check`
  and staged diff check passed before commit.
- UI execution uses the rendered-page Node DOM harness, not a browser. No visual
  browser inspection, screenshots, production deployment or performance
  benchmark is claimed. Existing responsive table/RTL/navigation structures
  remain in place.
- Offset pagination reflects live history, not a frozen snapshot: concurrent
  new entries may shift later pages between requests. No fixed-size JS merging
  or history-wide detail fetch is used.
- Full captured test logs are under `/tmp/slice3-focused.log` and
  `/tmp/slice3-full.log` in this sandbox; only summaries are versioned.
