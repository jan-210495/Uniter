Phase 7 Slice 5 — POS & header refinements (USSD generation, transaction flow, prefix mapping, mobile header, homepage redirect)

Project: /home/jabboud/projects/Uniter
Branch: arena/46c0244b-uniter (this session is pinned to that branch; do NOT create a new branch — commit directly to it).
arena does NOT have access to /home/jabboud/projects/friend-co/ — describe behavior inline.

Stack: FastAPI + SQLAlchemy async + SQLite, Python 3.12 venv at `.venv/`
Run: `source .venv/bin/activate && python3 -m pytest backend/` and `uvicorn backend.main:app` (port 8000)

## Goal

Five refinements across the app:

1. **Mobile header layout**: on small screens the app icon/logo is centered and the burger menu is on the far left.
2. **Fix `/pos` Generate USSD**: the generated USSD code is incorrect — it must match the exact MTN/Syriatel structures from `ussd-structure.md` using stored domain settings (MTN_DOMAIN/SYRIATEL_DOMAIN) with fallback defaults.
3. **Fix POS transaction flow**: transactions created from `/pos` are not appearing on `/transactions` and operator balance is not being deducted. Customer auto-register on Generate USSD works — keep it. The transaction must be created, balance deducted, and ProfitLedger recorded (same atomic path as slice 7.1a).
4. **Phone prefix mapping swap + validation**: MTN → `094`, `095`, `096`; Syriatel → `093`, `098`, `099`. Validation accepts only numbers starting with `093`, `094`, `095`, `096`, `098`, `099` and rejects everything else (currently `098` is not accepted).
5. **Homepage redirect**: `GET /` always redirects to `/pos`.

No changes to existing API response shapes except the USSD string format (already changed in 7.1a) and the new redirect behavior on `/`.

## Backend (`backend/main.py`)

### Homepage redirect
- `GET /` → `RedirectResponse(url="/pos", status_code=302)` (or 307). No HTML rendering on root.

### Phone validation (shared)
- Syrian mobile validation: accept only numbers starting with `093`, `094`, `095`, `096`, `098`, `099` (after normalizing `+963`/`00963`/`963` to `0` prefix). Reject everything else with 422.
- This applies to `validate_syrian_phone` in `backend/validation.py` and any client-side pattern/validation on `/pos`.

### `/pos` transaction creation (fix the bug)
When Generate USSD is pressed on `/pos`:
- Auto-register customer if name filled and no exact name match (already works — keep).
- Create the transaction via `POST /api/transactions` (or equivalent server path) so that:
  - The transaction row is inserted (visible on `/transactions`).
  - Operator balance is deducted atomically.
  - ProfitLedger row is recorded.
  - The response returns the correct USSD string.
- Currently the POS preview generates a USSD string but does not create a persisted transaction or deduct balance. Fix so the preview/flow matches the transaction endpoint behavior.

### USSD generation correctness
- `generate_ussd` must emit exact structures:
  - MTN prepaid: `*150*{MTN_DOMAIN}*{phone}*{amount}#`
  - MTN postpaid: `*154*{MTN_DOMAIN}*{phone}*{amount}#`
  - Syriatel prepaid: `*150*1*{SYRIATEL_DOMAIN}*1*{amount}*{phone}*{phone}#`
  - Syriatel postpaid: `*150*1*{SYRIATEL_DOMAIN}*5*{amount}*{phone}*{phone}#`
- Domains from settings (`MTN_DOMAIN`, `SYRIATEL_DOMAIN`), fall back to `65477` / `67858`.
- Amount in tens of cents (5000 → `500`).

## Frontend (`backend/templates/pos.html`)

### Generate USSD fix
- The USSD output shown on `/pos` must match the server-generated exact structure above (not a client-side approximation).
- When the form submits / preview generates, the code displayed is the real USSD string from the backend.

### Auto-operator prefix mapping (swap)
- `093`, `098`, `099` → Syriatel (`syriatel`)
- `094`, `095`, `096` → MTN (`mtn`)
- Other prefix → operator unchanged
- Only auto-select on first valid prefix match while typing (don't override a manual selection).

### Phone input validation
- Accept only `093`, `094`, `095`, `096`, `098`, `099` prefixes (after normalization). Reject others inline (pattern or validation message).
- Placeholder/hint reflects the accepted prefixes.

## Frontend (`backend/templates/base.html`) — mobile header layout

### Burger + logo positioning (small screens only, below `sm`)
- Burger menu button on the **far left** of the header.
- App icon/logo ("Uniter") **centered** in the header.
- Existing inline nav links hidden below `sm` (unchanged on desktop).
- Burger still toggles the off-canvas drawer from slice 7.2 (all six page links, language switcher, close button, backdrop, Escape, ARIA).
- Desktop (`sm` and up): logo left, nav inline, no burger — unchanged.

## Tests

### `backend/test_slice_5_refinements.py` (or extend existing test files)
- `GET /` → 302 redirect to `/pos`.
- `validate_syrian_phone` accepts `0933...`, `0944...`, `0955...`, `0966...`, `0987...`, `0999...`; rejects `0922...`, `0901...`, `0977...`, `0911...`, non-Syrian formats.
- `generate_ussd` exact strings for all four operator/service combos with stored domains and with fallbacks.
- `POST /api/transactions` from a `/pos`-like flow creates a transaction row, deducts operator balance, records ProfitLedger, returns correct USSD string.
- Insufficient balance → 400, no transaction created (already covered in 7.1a — keep green).
- Auto-operator mapping: `093/098/099` → syriatel; `094/095/096` → mtn; `092` → unchanged.
- `GET /stats`, `/pos`, `/settings`, etc. still 200 (no regressions).

### Template markup tests
- `base.html` renders burger on the left and centered logo on small screens (check markup classes/structure, not pixel position).
- `pos.html` renders the swapped prefix mapping comments/labels and the accepted-prefix hint.

## Verification

- `source .venv/bin/activate && python3 -m pytest backend/` → all tests pass (was 189, should stay green).
- `uvicorn backend.main:app` →
  - `GET /` → 302 to `/pos`.
  - `GET /pos` → 200 en/ar; Generate USSD shows correct MTN/Syriatel structure; transaction appears on `/transactions`; operator balance deducted; ProfitLedger records.
  - Phone input rejects `092...`, `090...`, `097...`; accepts `093...`–`099...` (except `097`).
  - Auto-operator: `0933` → syriatel, `0944` → mtn.
  - Mobile header: burger left, logo centered below `sm`.
- **COMMIT AND PUSH** when done (directly to `arena/46c0244b-uniter` — this session is pinned to it).

## Constraints

- Do not change existing API response shapes except the USSD string format (already done in 7.1a).
- Neutral names only (Ahmad, Rami) in tests and example data.
- No secrets/hardcoded credentials.
- Bilingual en/ar, RTL-aware.
- **COMMIT AND PUSH** completed work immediately after verification.

## Deliverable

Report back: slice 7.5 verified correct, committed, and pushed — or list issues found and fixed.
