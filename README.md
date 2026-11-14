# Uniter - Mobile Shop Charging & Units Management

A bilingual (Arabic/English) web application for managing mobile units charging (Pre-Paid) and bill payments (Post-Paid) for MTN and Syriatel in a local Syrian shop, featuring USSD string generation/copying, transaction ledgers, and responsive design for big screens and mobile devices.

## Tech Stack
- **Backend**: FastAPI (Python 3.12) + SQLAlchemy + SQLite
- **Frontend**: Responsive HTML5 / Tailwind CSS (Monochrome Precision theme) + Vanilla JS with Arabic RTL support (Noto Sans Arabic)
- **Features**: Dual operators (MTN, Syriatel), Pre-Paid & Post-Paid charging, USSD quick-copy workflow, transaction logs, debt/cash ledger.

## Sign-in and accounts
- Every page and API requires signing in; only the sign-in page, the one-time setup page, static assets, the web manifest and `/api/health` are public.
- **First run:** there is no default account or password. On a new database, opening the app (any page) leads to a one-time **Set up Uniter** page (`/setup`, English or Arabic) where you choose the password for the administrator account, username `admin` (at least 8 characters, at most 256, not the username, entered twice). Submitting it creates the account, signs you in and records the database as initialised, all in one step; until then every other page redirects to setup and every API returns 401. Setup works exactly once: afterwards `/setup` sends you to sign-in and can never change a password, even if two people submit it at the same moment (the second sees “already set up”).
- **Existing databases** never see setup and keep their accounts unchanged. A database that already has accounts is recorded as initialised on its next start (desktop launch, `python -m backend.seed`, or web startup). An account left over from an earlier v1.0.4 review build that still has its original short bootstrap password cannot sign in, because sign-in only accepts passwords of at least 8 characters; recover it as described below.
- Supported roles are `admin` and `seller`. Admins manage accounts on the **Users** page (side menu, admins only): create seller accounts, reset a seller's password, and activate or deactivate accounts. Usernames are matched without regard to case or surrounding spaces.
- New and reset accounts get a generated temporary password that is shown **once** to the admin, stored only as a hash, and must be changed at first sign-in. Accounts are deactivated rather than deleted; deactivating or resetting an account signs it out everywhere at once. Reactivated accounts keep their existing password and must sign in again. The last active admin cannot be deactivated, and admins cannot deactivate themselves.
- Only admins can change the commission rate, the MTN and Syriatel USSD domains, and the timezone; sellers see those settings read-only. Everything else, including the currency denomination and all sales workflows, is available to both roles.
- Each new sale records the account that committed it, taken from the signed-in session only (a commit request that tries to name a user is rejected). Sales recorded before this version show **Legacy / unknown** and are never back-filled. The Transactions page has a **Performed by** filter listing every account, including deactivated ones; it narrows the view only — both roles see all transactions and can filter by anyone. `GET /api/transactions?performed_by=<user id>` applies the same filter (an unknown id returns an empty list; a value that is not a positive integer is rejected with 422).
- Sessions are server-side (the browser holds an HttpOnly, `SameSite=Lax` cookie; the database stores only its SHA-256 digest). Sessions end after 4 hours idle or 12 hours total, and on sign-out or password change. The cookie is marked `Secure` automatically over HTTPS; override with `UNITER_COOKIE_SECURE=1` or `0` (e.g. behind a TLS proxy).
- State-changing requests require the session's CSRF token (sent automatically by the app's pages). Repeated failed sign-ins are throttled: 5 per username or 20 per client address within 15 minutes.

### Recovering administrator access
There is no recovery screen and no reset password. If an initialised database has no usable active admin (every admin deactivated or removed, or the only admin's password lost), the app refuses to run: pages show **Sign-in is unavailable** and the APIs return 503. Setup does not reopen, so nobody can take the database over from the browser. Recovery needs local access to the database file:

1. Stop Uniter and copy the database file somewhere safe.
2. If an admin account still exists but is deactivated and you know its password, reactivate it: `sqlite3 <database file> "UPDATE users SET is_active = 1 WHERE username = 'admin';"`
3. If the password is unknown, set a new one from the project directory. The prompt does not echo the password, and only the hash is written:
   `python -c "import getpass, sqlite3, sys; from backend.auth import hash_password; db = sqlite3.connect(sys.argv[1]); db.execute('UPDATE users SET password_hash = ?, must_change_password = 1, is_active = 1 WHERE username = ?', (hash_password(getpass.getpass('New password: ')), 'admin')); db.commit()" <database file>`
   The account must choose a new password at its next sign-in.
4. Start Uniter again and sign in.
