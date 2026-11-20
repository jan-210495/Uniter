# Slice 4 — filtered XLSX transaction export

## Baseline / scope

- Fetched origin first. Remote `refs/heads/release/1.0.5` and
  `refs/heads/release/1.0.5-slice-4-rollback` both resolved to
  `eed59ec10de1711265bb52b25eeddecd6a3d34cd` (required parent).
- Working/push branch: `arena/48839799-uniter`.
- The restored workspace's tracked changes and five untracked prior-slice files
  were verified against the required base before aligning the local branch/index
  to that base. No file changes from that restore were discarded.
- No accounting/schema/migration, release metadata, deployment, integration
  merge, tags, publication, CSV/PDF/import or customer/debt work.

## Implementation

`GET /api/transaction-ledger/export` uses the same `_ledger_selection`
FastAPI dependency as the listing. It accepts record_type, operator_code,
service_code, performed_by, customer_phone, status, plus `lang=en|ar`.
The listing keeps its existing limit/offset validation and response shape.
Export intentionally ignores extra limit/offset query parameters; the UI omits
both. Sale-only filter misuse remains 422; `status=failed` produces a header-only
workbook. `_ordered_ledger` shares the timestamp/source/id descending order.

One unpaginated ordered key query captures the complete selected identity set.
`_hydrate_ledger` is shared with listing, including original snapshot and SIM
redaction logic. Export hydrates these fixed keys in 200-key batches (not offset
pages), avoiding bind-count limits and offset shifts from newly inserted rows.
No accounting or other data is written. Authentication uses the existing global
gate; this read-only GET needs no CSRF exception. Response headers include the
XLSX MIME type, a fixed-pattern UTC-date attachment filename, no-store, nosniff.
No user text is interpolated into response headers.

The localized native download link uses the same JS query builder as listing,
without pagination, preserving language, type and all active applicable filters.
It refreshes its URL when filters change and again on click, so keyboard/native
navigation uses session cookies, not another unauthenticated data path.

Workbook: neutral `Transactions` sheet, EN/AR headers and type/source/status
labels, RTL sheet view for Arabic, frozen header, auto-filter, practical widths,
readable UTC timestamps (text, explicitly labelled UTC), bold headers. SYP money
is numeric OOXML with built-in two-decimal format, written from exact Decimal
minor-units/100, not a Python binary float. No mixed sale/top-up totals are added.
Non-applicable cells are blank; legacy operator top-ups do not acquire a user.
Hidden private SIM metadata never reaches the writer.

## Dependency / package decision

- Runtime dependencies unchanged. `backend/xlsx.py` uses only `zipfile`,
  `xml.etree.ElementTree`, `Decimal`, `datetime`, `io`, `re`.
- The data-only workbook has six fixed OOXML package parts, inline strings,
  numeric cells and two simple styles; no formulas/macros/external links/images.
- All text cells are explicitly `inlineStr`, including dangerous prefixes after
  whitespace. This is the formula-injection defense; no stored data is altered.
  ElementTree escapes markup. Invalid XML 1.0 controls/surrogates are removed
  from exported text only, and cells are capped at Excel's 32,767-character limit.
- `requirements-test.txt` includes runtime requirements and pins `openpyxl==3.1.5`
  solely as an independent standards-based test reader. CI test workflow installs
  this file. Application and release build do not import/package openpyxl.
- Inspected `uniter.spec`, `scripts/build_exe.py`, and Windows release workflow.
  Static stdlib/application imports are discoverable by PyInstaller: no hidden
  imports/data changes are needed. No actual Windows build is claimed.

## Verification

Python **3.12.8** (source-built in the sandbox because the initial system
interpreter was 3.11). Installed the declared runtime requirements and the
separately declared test reader; no runtime requirement was added.

Focused (exit 0):

```text
python3 -m pytest backend/test_transaction_export.py backend/test_transaction_ledger.py backend/test_transactions_list.py backend/test_transactions_actions.py backend/test_transaction_attribution.py --tb=short -q
97 passed, 91 warnings in 22.74s
```

Full required command (exit 0):

```text
python3 -m pytest backend/ --tb=short -q
885 passed, 724 warnings in 192.71s (0:03:12)
```

Warnings are FastAPI lifecycle, Starlette/AnyIO and HTTPX TestClient dependency
deprecations. Full logs: `/tmp/slice4-focused.log`, `/tmp/slice4-full.log`
(sandbox only).

Clean runtime installation (exit 0):

```text
uv venv --python .venv/bin/python .cache/xlsx-runtime
uv pip install --python .cache/xlsx-runtime/bin/python -r requirements.txt
```

In that clean environment, verified every pinned runtime version with
`importlib.metadata`, verified `openpyxl` is absent, imported the app, generated a
workbook and validated its ZIP. Also installed `requirements-test.txt` into the
verification environment and ran `uv pip check --python .venv/bin/python`:
`All installed packages are compatible`.

Full diff review includes all new OOXML/XML construction, exact numeric text,
string-cell types, static ZIP paths/relationships, filter dependency/ordering,
private-SIM projection, query batching, UI link, headers, tests and CI changes.
`git diff --check` passed; staged check passed before commit.

Coverage includes ZIP/OOXML validity and standard-reader opening, numeric cents,
formatting/filter/freeze/RTL/header inspection, Arabic/escaping, all matching
records beyond 50 and 200 across deterministic ties, filter cross-products,
422 parity, failed exclusion, literal formula payloads for every dangerous
prefix after whitespace, safe filename inputs, auth, seller SIM redaction,
no changes to sales/SIM/operator balances/top-up/profit/cash/refund rows, workbook
row limits, and UI filter/offset/download-link behavior via the Node DOM harness.
No existing test assertions were weakened/deleted for this slice; the harness
only gains href capture so export-link behavior can be asserted.

## Limitations

- The UI remains live offset pagination; rows can shift between page calls.
  Export selects keys once and does not inherit the UI's offset drift. It is not
  a database-wide frozen historical snapshot across concurrent edits/status
  changes while details are being loaded.
- Export builds the workbook in server memory; very large histories need
  proportionate RAM. Excel's single-sheet limit is enforced with 413 above
  1,048,575 data rows. No stress benchmark is claimed.
- Excel itself represents numeric cells with its normal precision limits; exact
  base-10 XML avoids introducing binary-float errors in our writer.
- No browser/screenshots/visual product acceptance, Windows executable build,
  Linux deployment, or production performance test was performed. Boss's full
  integrated browser/product test remains separate.
