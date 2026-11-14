# Uniter - Mobile Shop Charging & Units Management

A bilingual (Arabic/English) web application for managing mobile units charging (Pre-Paid) and bill payments (Post-Paid) for MTN and Syriatel in a local Syrian shop, featuring USSD string generation/copying, transaction ledgers, and responsive design for big screens and mobile devices.

## Tech Stack
- **Backend**: FastAPI (Python 3.12) + SQLAlchemy + SQLite
- **Frontend**: Responsive HTML5 / Tailwind CSS (Monochrome Precision theme) + Vanilla JS with Arabic RTL support (Noto Sans Arabic)
- **Features**: Dual operators (MTN, Syriatel), Pre-Paid & Post-Paid charging, USSD quick-copy workflow, transaction logs, debt/cash ledger.

## Sign-in and accounts
- Every page and API requires signing in; only the sign-in page, static assets, the web manifest and `/api/health` are public.
- **First run:** the first time an initialised database is opened (desktop launch, `python -m backend.seed`, or web startup on an existing database), a single bootstrap account is created: username `admin`, temporary password `admin`. Signing in with it immediately requires choosing a new password (at least 8 characters) before anything else is reachable. The temporary password then stops working, and the bootstrap account is never recreated or reset on later startups.
- Supported roles are `admin` and `seller`. Admins manage accounts on the **Users** page (side menu, admins only): create seller accounts, reset a seller's password, and activate or deactivate accounts. Usernames are matched without regard to case or surrounding spaces.
- New and reset accounts get a generated temporary password that is shown **once** to the admin, stored only as a hash, and must be changed at first sign-in. Accounts are deactivated rather than deleted; deactivating or resetting an account signs it out everywhere at once. Reactivated accounts keep their existing password and must sign in again. The last active admin cannot be deactivated, and admins cannot deactivate themselves.
- Only admins can change the commission rate, the MTN and Syriatel USSD domains, and the timezone; sellers see those settings read-only. Everything else, including the currency denomination and all sales workflows, is available to both roles.
- Sessions are server-side (the browser holds an HttpOnly, `SameSite=Lax` cookie; the database stores only its SHA-256 digest). Sessions end after 4 hours idle or 12 hours total, and on sign-out or password change. The cookie is marked `Secure` automatically over HTTPS; override with `UNITER_COOKIE_SECURE=1` or `0` (e.g. behind a TLS proxy).
- State-changing requests require the session's CSRF token (sent automatically by the app's pages). Repeated failed sign-ins are throttled: 5 per username or 20 per client address within 15 minutes.
