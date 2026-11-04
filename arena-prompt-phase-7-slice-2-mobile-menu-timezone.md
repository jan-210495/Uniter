Phase 7 Slice 2 — Mobile burger menu + timezone selector (frontend UI only)

Project: /home/jabboud/projects/Uniter
Branch: create `phase-7-mobile-menu-timezone` from `arena/46c0244b-uniter` (current HEAD on origin, includes slices 7.1a–7.1c).
arena does NOT have access to /home/jabboud/projects/friend-co/ — describe behavior inline.

Stack: FastAPI + SQLAlchemy async + SQLite, Python 3.12 venv at `.venv/`
Run: `source .venv/bin/activate && python3 -m pytest backend/` and `uvicorn backend.main:app` (port 8000)

## Goal

Add two frontend-only enhancements shared across all pages via `backend/templates/base.html`:

1. **Mobile burger menu**: a hamburger toggle that opens a side drawer on small screens listing all nav links (POS, Settlement, Transactions, Customers, Settings, Top-up) plus language switcher. On desktop the existing inline nav stays as-is.
2. **Timezone selector**: a dropdown in the settings page (`/settings`) letting the user pick a timezone, persisted via existing settings mechanism, and displayed in the header.

No backend changes required for the burger menu. For the timezone selector, reuse the existing `settings` table / `set_setting` pattern — no new endpoints unless you find a gap.

## Frontend (`backend/templates/base.html`)

### Burger menu
- Add a hamburger button (`<button>`) visible only on small screens (hidden `sm:` and up, shown `sm:hidden`). Place it in the header, to the right of the "Uniter" logo.
- Clicking the burger toggles a side drawer (off-canvas panel) that slides in from the left on LTR / right on RTL.
- Drawer contains: all nav links (POS, Settlement, Transactions, Customers, Settings, Top-up), the language switcher (English / العربية), and a close button (or clicking backdrop closes).
- Drawer is hidden by default; `aria-expanded` toggles true/false on the burger button; drawer has `role="navigation"` and `aria-modal="true"`.
- Escape key closes the drawer.
- Clicking any nav link closes the drawer and navigates.
- Bilingual labels matching existing nav text (`نقطة البيع` / POS, etc.), RTL-aware, Monochrome Precision styling (navy/blue/slate).

### Timezone selector (in `/settings` via base.html or settings.html)
- Add a "Timezone" row in the settings page (`/settings`): label "Timezone", select dropdown with common timezone options (e.g. UTC, Europe/Damascus, Asia/Damascus, America/New_York), prefilled with current value.
- On submit: PUT or POST to persist the timezone setting — reuse the existing settings update pattern (`set_setting` / existing settings endpoints). If no dedicated timezone endpoint exists, add one following the same shape as the domain PUT endpoints from slice 7.1b.
- Display current timezone in the header (small text, e.g. "UTC+3") next to the language switcher.

## Frontend (`backend/templates/settings.html`)
- Add timezone select row matching existing settings card styling (Monochrome Precision).
- Feedback via role="status" live region: "Timezone updated" / error on failure.

## Backend (minimal)
- If adding a timezone endpoint: `PUT /api/settings/timezone` with body `{timezone: str}`, validation non-empty string, upsert into `settings` table key `timezone`. Same pattern as `/api/settings/mtn-domain`.
- No other backend changes.

## Tests (`backend/test_settings_page.py` or new `backend/test_settings_timezone.py`)
- `/settings` renders 200 in en and ar, shows timezone select with current value.
- PUT `/api/settings/timezone` with valid value → 200; subsequent GET reflects it.
- PUT with empty value → 422, state unchanged.
- Base template renders burger button and drawer markup (check via TestClient on `/pos` and `/settings`).
- Existing page tests still pass.

## Verification
- `source .venv/bin/activate && python3 -m pytest backend/` → all tests pass (was ~150, should stay green).
- `uvicorn backend.main:app` → GET `/pos`, `/settings`, `/customers` 200 in en and ar; burger visible on narrow viewport; drawer opens/closes; timezone persisted and shown in header.
- **COMMIT AND PUSH** when done.

## Constraints
- Do not change existing API response shapes except adding the timezone PUT endpoint if needed.
- Neutral names only (Ahmad, Rami) in tests and example data.
- No secrets/hardcoded credentials.
- Bilingual en/ar, RTL-aware.
- **COMMIT AND PUSH** completed work immediately after verification.

## Deliverable
Report back: slice 7.2 verified correct, committed, and pushed — or list issues found and fixed.
