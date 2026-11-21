# Uniter - Mobile Shop Charging & Units Management

A bilingual (Arabic/English) web application for managing mobile units charging (Pre-Paid) and bill payments (Post-Paid) for MTN and Syriatel in a local Syrian shop, featuring USSD string generation/copying, transaction ledgers, and responsive design for big screens and mobile devices.

## Tech Stack
- **Backend**: FastAPI (Python 3.12) + SQLAlchemy + SQLite
- **Frontend**: Responsive HTML5 / Tailwind CSS (Monochrome Precision theme) + Vanilla JS with Arabic RTL support (Noto Sans Arabic)
- **Features**: Dual operators (MTN, Syriatel), Pre-Paid & Post-Paid charging, USSD quick-copy workflow, transaction logs, SIM balances, and sales/profit reporting.

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

## Sale identity
- Enter the phone manually in POS. The optional name is a per-sale snapshot,
  trimmed on commit (blank becomes NULL), not a customer account or lookup.
- Transaction history and search use that stored name and phone. The phone
  filter works directly on sale rows.
- Customer directories, debt ledgers and settlement workflows are no longer
  supported. Global search covers transactions and operators only.
- This release is for fresh installs. No customer/debt data migration or
  snapshot backfill is provided; historical migration code/tests are retained
  only to verify earlier schema and accounting changes.

## Operators and SIM cards
- The **Operators** page (side menu) lists MTN first and Syriatel second. MTN and Syriatel are the two supported companies; their services and USSD settings are unchanged. Each company section shows:
  - **Legacy company credit**: the company's former shop credit from before SIM balances, with its last top-up, shown read-only and labelled "Not used for new sales" (this replaces the former Top-up page; `/topup` now opens the Operators page). It is kept exactly as it was until a migration policy is chosen: it cannot be topped up, new sales are never charged to it, and its money is never split, copied or moved onto a SIM card.
  - **SIM cards**: the individual SIM/card accounts of that company you can use, each with a display name, phone number, owner (for private cards), private/shared status, active/archived status and its own **balance**. A fresh install starts with no SIM cards.
- Who sees and manages SIM cards (enforced by the server on every page and API call):
  - A seller sees shared SIM cards and their own private SIM cards. Other sellers' private SIM cards are never shown or returned; asking for one by id answers "not found".
  - Admins see every SIM card, including archived ones (under **Archived SIM cards** in each company section).
  - A seller can add a private SIM card for themselves or a shared one. An admin can add a private SIM card for any active account, or a shared one.
  - The owner of a private SIM card, or an admin, can edit its name and phone number, archive it and restore it. Only an admin can edit, archive or restore a shared SIM card (even one a seller added), or change a SIM card's owner or private/shared status. The company of a SIM card never changes.
  - SIM cards are archived, never deleted: an archived card keeps its identity and owner and can be restored. Sellers do not see archived shared SIM cards.
- **SIM balances (v1.0.4 Slice 4.1).** Every SIM card holds its own balance, in the same minor units as all other amounts:
  - A new SIM card starts at 0. Money is added only with its **Top up** form on the Operators page; each top-up is recorded once (a repeated or retried submit never adds it twice) in an append-only SIM ledger with who did it, when, the amount, the resulting balance and a `SIMTOPUP-…` reference. Archived SIM cards cannot be topped up.
  - You can use (sell from, top up, make your default) your own active private SIM cards and the active shared ones; an admin can use every active SIM card.
  - **Your default**: each account can save one default SIM card per company ("Make my default" on the Operators page, or "Make default" in POS). Archiving a SIM card clears it as a default; when a SIM card's owner or private/shared status changes, accounts that can no longer use it lose it as their default.
  - **POS**: under the company, pick the SIM card the sale is charged to; your default is preselected and changing the company switches the choices. Without a usable SIM card for the company, POS asks you to add or select one and records nothing — it never falls back to the legacy company credit. Preparing a code still records nothing; Copy or Dialer records the sale and debits exactly its face value from the chosen SIM card, once (commission, profit and the customer amount are unchanged). A repeated copy returns the original sale and SIM card. If the SIM card's balance is too low, nothing is recorded.
  - **Failed sales**: marking a sale failed returns the stored original debit to the same SIM card exactly once (also when the card has since been archived), with a `REFUND-…` ledger row; removing a failed sale never refunds again. Sales recorded before SIM balances show "Legacy company credit" on the Transactions page and cannot be marked failed, so no SIM card is ever guessed for them.
- **Upgrading:** the SIM card table is added automatically on start-up (recorded as `v1.0.4-sim-cards` in `schema_migrations`). No SIM card is created and no money is moved: every existing shop credit balance, top-up, refund and sale stays attached to its company exactly as before. If a database already contains a `sim_cards` table with a different structure, Uniter refuses to start and leaves the file unchanged.
- **Upgrading to SIM balances:** the `sim_balances`, `sim_ledger` and `sim_defaults` tables and the nullable `transactions.sim_card_id` column are added automatically on start-up (recorded as `v1.0.4-sim-balances` in `schema_migrations`). Existing sales keep an empty SIM card, no SIM card is created and no money is moved; every legacy credit balance, top-up, refund and sale keeps its exact value. If those tables or that column already exist with a different structure, Uniter refuses to start and leaves the file unchanged.

## Owner-operated Linux VPS (fresh install)

For the Python 3.12 web-service path (not the Windows executable), see
[the VPS installation and recovery guide](docs/vps-deployment.md) and the
[owner-run configuration bundle](deploy/vps/). It covers private first setup,
HTTPS/proxy trust, persistent SQLite state, backups, updates and recovery.
These files do not deploy or modify any server automatically.

## Windows release packaging

For v1.0.5 installer metadata, portable input checks and the separate
**non-publishing** Windows build verification path, see
[Windows release readiness](docs/windows-release.md). Tag creation/publication
remains a separately approved final action; v1.0.4 stays immutable.
