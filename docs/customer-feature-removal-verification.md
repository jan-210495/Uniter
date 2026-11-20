# Customer/debt removal verification

## Base and scope

- Actual working/push branch: `arena/48839799-uniter`.
- Required base and parent: `ce158cccb6ecabacf43fcf9a7d37607e26daebf0`.
- `git fetch origin` completed before edits. The restored worktree matched the
  required commit exactly; the local Arena branch was aligned to it before edits.
- `git ls-remote origin refs/heads/release/1.0.5 refs/heads/release/1.0.5-slice-2-rollback`
  confirmed both refs at the required base. Neither ref was changed.
- Python: `Python 3.12.8`, built from CPython v3.12.8 sources because the sandbox
  default is 3.11.2. SSL/SQLite/zlib were built outside the repository. Runtime
  used OpenSSL 3.0.16 and SQLite 3.49.1. No build artifacts or dependency changes
  are included in Git.
- Requirements installed with:
  `~/.local/bin/uv venv --python /tmp/pybuild/python/python .venv` and
  `~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt`.
- Tests below ran after `source .venv/bin/activate`.

## Behavior and intentional test changes

- Removed Customer/DebtLedger models, tables and relationships from fresh
  metadata; removed the transaction customer ID column/FK and API output field.
- Removed customer/debt/settlement pages, routes, request/response models,
  helpers, navigation, directory search groups, and directory suggestion/info UI.
- Deleted the unused legacy dashboard template, which embedded customer/debt
  management and stale charging code. Root still redirects to POS.
- Preserved transaction phone/name snapshots and direct phone filtering. Search
  now returns only transactions and operators. Names remain trim-normalized;
  blank names remain NULL. POS keeps manual phone entry, validation, optional
  names, SIM selection/defaults and prefix-based operator selection.
- Removed only debt totals from register accounting. Sales-only register open,
  close and summaries remain, tested with successful and refunded failed sales.
- Kept debit, commission/profit, refund, idempotency/concurrency, attribution,
  statistics, SIM/top-up and permission tests. Shared security assertions now
  target remaining transaction/SIM/register routes rather than retired routes.
- Deleted customer/debt/settlement-only test modules and test cases. Shared POS,
  money-formatting, navigation, search and accounting tests were rewritten to
  use transaction snapshots, not removed directory fixtures. Exact API shape
  assertions omit customer_id and the global customers group by design.
- Fresh schema tests check both ORM metadata and physical SQLite tables/FKs.
  Added route-absence, EN/AR/RTL, manual phone/name DOM-handler, snapshot search,
  direct phone filter, forbidden client attribution and sales-only register tests.
- Historical migration tests still verify prior schemas and unchanged money.
  `historical_test_support.py` loads customer/debt DDL only from the immutable
  checked-in v1.0.4 SQL dump into explicitly historical fixtures; it is never
  imported by application initialization. Other current fixtures do not recreate
  removed models. Historical-vs-fresh comparisons intentionally exclude the
  removed column/FK and compare all surviving column definitions.
- The historical migration planner recognizes the fresh directory-free schema
  as already current. It adds no customer/debt tables and performs no removal
  or backfill migration. Old-schema preservation tests and fail-closed tests
  remain in the suite.
- Historical planning/evidence docs are marked superseded, with README as the
  supported-workflow reference. Remaining customer terms in sale labels and
  customer_name/customer_phone fields refer to transaction data, not accounts.

## Focused verification

Command (exit 0):

```sh
python3 -m pytest backend/test_customer_feature_removal.py backend/test_database.py backend/test_seed.py backend/test_initial_setup.py backend/test_auth.py backend/test_auth_access.py backend/test_user_management.py backend/test_pos.py backend/test_pos_customer_registration.py backend/test_pos_recognition.py backend/test_pos_template.py backend/test_pos_ussd_result.py backend/test_transactions_list.py backend/test_transactions_actions.py backend/test_global_search.py backend/test_operator_page.py backend/test_operator_sims.py backend/test_sim_balances.py backend/test_topup.py backend/test_commission.py backend/test_ussd_profit.py backend/test_transaction_lifecycle.py backend/test_transaction_status.py backend/test_operator_credit_refund.py backend/test_transaction_attribution.py backend/test_stats.py --tb=short -q
```

Complete captured output (trailing whitespace normalized):

```text
........................................................................ [ 13%]
........................................................................ [ 27%]
........................................................................ [ 40%]
........................................................................ [ 54%]
........................................................................ [ 67%]
........................................................................ [ 81%]
........................................................................ [ 95%]
..........................                                               [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/starlette/testclient.py:41
  /home/user/Uniter/.venv/lib/python3.12/site-packages/starlette/testclient.py:41: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    [], typing.ContextManager[anyio.abc.BlockingPortal]

backend/main.py:100
  /home/user/Uniter/backend/main.py:100: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    @app.on_event("startup")

.venv/lib/python3.12/site-packages/fastapi/applications.py:4495
  /home/user/Uniter/.venv/lib/python3.12/site-packages/fastapi/applications.py:4495: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    return self.router.on_event(event_type)

backend/test_customer_feature_removal.py: 27 warnings
backend/test_initial_setup.py: 35 warnings
backend/test_auth.py: 28 warnings
backend/test_auth_access.py: 27 warnings
backend/test_user_management.py: 51 warnings
backend/test_pos.py: 13 warnings
backend/test_pos_customer_registration.py: 29 warnings
backend/test_pos_recognition.py: 1 warning
backend/test_pos_template.py: 1 warning
backend/test_transactions_list.py: 9 warnings
backend/test_global_search.py: 6 warnings
backend/test_operator_page.py: 9 warnings
backend/test_operator_sims.py: 40 warnings
backend/test_sim_balances.py: 121 warnings
backend/test_topup.py: 11 warnings
backend/test_ussd_profit.py: 8 warnings
backend/test_transaction_lifecycle.py: 20 warnings
backend/test_transaction_status.py: 23 warnings
backend/test_operator_credit_refund.py: 30 warnings
backend/test_transaction_attribution.py: 55 warnings
backend/test_stats.py: 8 warnings
  /home/user/Uniter/.venv/lib/python3.12/site-packages/httpx/_client.py:680: DeprecationWarning: The 'app' shortcut is now deprecated. Use the explicit style 'transport=WSGITransport(app=...)' instead.
    warnings.warn(message, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
530 passed, 555 warnings in 175.10s (0:02:55)
```

## Full verification

Command (exit 0):

```sh
python3 --version
python3 -m pytest backend/ --tb=short -q
```

Version output: `Python 3.12.8`.

Complete captured pytest output (trailing whitespace normalized):

```text
........................................................................ [  8%]
........................................................................ [ 16%]
........................................................................ [ 25%]
........................................................................ [ 33%]
........................................................................ [ 42%]
........................................................................ [ 50%]
........................................................................ [ 58%]
........................................................................ [ 67%]
........................................................................ [ 75%]
........................................................................ [ 84%]
........................................................................ [ 92%]
................................................................         [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/starlette/testclient.py:41
  /home/user/Uniter/.venv/lib/python3.12/site-packages/starlette/testclient.py:41: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    [], typing.ContextManager[anyio.abc.BlockingPortal]

backend/main.py:100
backend/main.py:100
  /home/user/Uniter/backend/main.py:100: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    @app.on_event("startup")

.venv/lib/python3.12/site-packages/fastapi/applications.py:4495
.venv/lib/python3.12/site-packages/fastapi/applications.py:4495
  /home/user/Uniter/.venv/lib/python3.12/site-packages/fastapi/applications.py:4495: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    return self.router.on_event(event_type)

.venv/lib/python3.12/site-packages/httpx/_client.py:680: 1 warning
backend/test_amount_display.py: 1 warning
backend/test_auth.py: 28 warnings
backend/test_auth_access.py: 27 warnings
backend/test_customer_feature_removal.py: 27 warnings
backend/test_global_search.py: 6 warnings
backend/test_initial_setup.py: 35 warnings
backend/test_installability.py: 3 warnings
backend/test_money_boundaries.py: 19 warnings
backend/test_navigation.py: 15 warnings
backend/test_operator_credit_refund.py: 30 warnings
backend/test_operator_page.py: 9 warnings
backend/test_operator_sims.py: 40 warnings
backend/test_pos.py: 13 warnings
backend/test_pos_customer_registration.py: 29 warnings
backend/test_pos_recognition.py: 1 warning
backend/test_pos_template.py: 1 warning
backend/test_pos_vouchers.py: 5 warnings
backend/test_settings_page.py: 41 warnings
backend/test_settings_structure.py: 12 warnings
backend/test_sim_balances.py: 121 warnings
backend/test_slice_5_refinements.py: 19 warnings
backend/test_stats.py: 8 warnings
backend/test_topup.py: 11 warnings
backend/test_transaction_attribution.py: 55 warnings
backend/test_transaction_lifecycle.py: 20 warnings
backend/test_transaction_status.py: 23 warnings
backend/test_transactions_list.py: 9 warnings
backend/test_user_management.py: 51 warnings
backend/test_ussd_profit.py: 8 warnings
backend/test_voucher_serialization.py: 27 warnings
  /home/user/Uniter/.venv/lib/python3.12/site-packages/httpx/_client.py:680: DeprecationWarning: The 'app' shortcut is now deprecated. Use the explicit style 'transport=WSGITransport(app=...)' instead.
    warnings.warn(message, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
856 passed, 700 warnings in 214.37s (0:03:34)
```

## Diff and stale-reference review

- `git diff --check`: exit 0, no output.
- Reviewed the complete diff, especially sale debit/profit/refund paths and
  shared security/accounting test changes. Financial changes are limited to
  removing the debt contribution from register totals.
- Searched the repository for Customer/DebtLedger symbols, customer_id,
  customer/debt/settlement routes, directory templates, scripts and docs.
- An assertion-based search over main.py, database.py, seed.py and every active
  HTML template found no removed model definitions, transaction customer-ID
  references/FKs, retired routes, customer API calls or customer navigation.
- Remaining historical references are intentional: migrations.py, immutable SQL
  history, migration-only fixtures/tests, explicitly historical docs, and
  negative assertions that reject retired APIs. No active dependency remains.
- No screenshots or real-browser checks were produced. UI verification used
  HTTP-rendered templates and existing Node DOM-handler harnesses.

## Limitations and publication

Fresh-install release only: no migration/backfill/removal of deployed seller
customer data is provided. The new application is not offered as an old-data
upgrade in this slice. Deprecation warnings remain; no test is skipped or
xfail-ed to obtain these results. Initial regression runs exposed obsolete
fixtures/assertions; those were corrected before the successful final runs.
No release/version metadata, tags, installer versions, deployment, XLSX export
or unified top-up ledger was changed. No integration/main merge was performed.
The commit and confirmed remote SHA are reported in the accompanying handoff.

## Changed files (Git status before commit)

`M` modified, `D` removed, `??` newly added:

```text
 M README.md
 M arena-prompt-phase-7-slice-2-mobile-menu-timezone.md
 M arena-prompt-phase-7-slice-3-global-search-operator-subpage.md
 M arena-prompt-phase-7-slice-4-stats-page.md
 M arena-prompt-phase-7-slice-6-amount-display-syp.md
 M backend/database.py
 M backend/main.py
 M backend/migrations.py
 M backend/money_form_harness.cjs
 M backend/navigation_browser_check.py
 M backend/pos_result_harness.cjs
 M backend/pos_voucher_harness.cjs
 D backend/settlement_dom_harness.cjs
 M backend/templates/base.html
 D backend/templates/customers.html
 D backend/templates/index.html
 M backend/templates/pos.html
 D backend/templates/settlement.html
 M backend/templates/transactions.html
 M backend/test_amount_display.py
 M backend/test_auth.py
 M backend/test_auth_access.py
 D backend/test_customers_debt_page.py
 D backend/test_customers_page.py
 M backend/test_database.py
 M backend/test_global_search.py
 M backend/test_initial_setup.py
 M backend/test_money_boundaries.py
 M backend/test_navigation.py
 M backend/test_operator_credit_refund.py
 M backend/test_operator_page.py
 M backend/test_operator_sims.py
 M backend/test_pos.py
 M backend/test_pos_customer_registration.py
 M backend/test_pos_recognition.py
 M backend/test_pos_template.py
 M backend/test_pos_ussd_result.py
 M backend/test_settings_page.py
 D backend/test_settlement_dom.py
 M backend/test_sim_balances.py
 M backend/test_slice_5_refinements.py
 M backend/test_stats.py
 M backend/test_transaction_attribution.py
 M backend/test_transaction_lifecycle.py
 M backend/test_transaction_migration.py
 M backend/test_transaction_status.py
 M backend/test_transactions_actions.py
 M backend/test_transactions_list.py
 M backend/test_user_management.py
 M backend/test_ussd_profit.py
 M backend/test_voucher_serialization.py
 M backend/transactions_actions_harness.cjs
 M docs/navigation-verification.md
 M docs/syp-runtime-audit.md
 M roadmap.md
?? backend/historical_test_support.py
?? backend/test_customer_feature_removal.py
?? docs/customer-feature-removal-verification.md
```
