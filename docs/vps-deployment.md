# Owner-run VPS installation (fresh Ubuntu/Debian, Python 3.12)

This is a **procedure and configuration bundle, not a performed deployment**.
It supports one owner-operated host, one process and one SQLite database. It is
not HA, unattended deployment, or a claim of complete production hardening.
The Windows executable/PyInstaller build is **not** the VPS startup path.
Files: [`deploy/vps/`](../deploy/vps/). Read this entire document before acting.
Never put passwords, DNS API credentials, TLS private keys or real data in Git
or chat. Enter passwords only in the owner's browser/server prompts.

# Guided Debian/Ubuntu VPS installer

The wizard installs a fresh Uniter service. The DNS hostname prompt is optional: leave it blank for a local-only service with no Nginx config, or enter a valid hostname if you intend to configure an HTTPS proxy later. Uniter remains bound to `127.0.0.1:8000` in both cases.

## Existing detailed procedure

Use a fresh dedicated Ubuntu/Debian VPS with systemd, a sudo-capable owner,
sufficient persistent disk/RAM, accurate time, working DNS resolution and a
**maintained Python 3.12 interpreter with venv, pip, SSL and SQLite support**.
Ubuntu 24.04 provides Python 3.12 packages; other Ubuntu/Debian releases may not.
Do not replace the distribution's system Python, install unreviewed PPAs or use
`curl | sh`. If your distribution lacks supported 3.12 packages, provision a
maintained 3.12 installation first using its documented supported mechanism;
that prerequisite is outside this bundle. Do not substitute 3.11/3.13 silently.
The interpreter must be system-wide and readable/executable by `uniter`, outside
home directories (`ProtectHome=true` intentionally blocks home-based pyenv/venvs).
The commands below assume `python3.12` resolves to that interpreter.

Before installing services:

- Set up SSH keys; restrict SSH by owner/VPN addresses and disable password/root
  SSH login only after testing a second session and recovery-console access.
- Review provider AND host firewall policies, including IPv6. Deny unsolicited
  ingress except restricted SSH initially. **Do not open 8000, 80 or 443 yet.**
  Establish SSH allowances before enabling a host firewall; do not lock yourself
  out. No universal `ufw reset`/firewall recipe is safe for every topology.
- Choose a domain you control and arrange A (and only if supported, AAAA) DNS
  records for this VPS. Do not publish an unsupported IPv6 path. Replace every
  `REPLACE_WITH_DOMAIN` below with that domain, without scheme, port or slash.
- This bundle assumes Nginx is the only proxy, directly on the same host. Do not
  put a CDN/load balancer in front without separately reviewing source-IP and
  TLS trust. Local processes can impersonate this loopback proxy: do not give
  untrusted users/workloads access to this host.
- Check for pre-existing services/configuration and conflicts before installing.
  Do not overwrite another application's files or default sites blindly.

On Ubuntu 24.04, for example (owner shell):

```bash
set -euo pipefail
sudo apt-get update
sudo apt-get install python3.12 python3.12-venv git nginx certbot ca-certificates curl
python3.12 --version
python3.12 -c 'import ssl, sqlite3, venv; print(ssl.OPENSSL_VERSION, sqlite3.sqlite_version)'
sudo systemctl stop nginx
```

Package installation may auto-start Nginx; the closed ingress policy must be in
place first. Keep OS security updates, monitoring and reboot policy under owner
control. Do not expose default package sites.

## 2. Paths, code and configuration

| Path | Purpose / permissions |
|---|---|
| `/opt/uniter/releases/COMMIT` | reviewed source + its own `.venv`, root:uniter, no service write |
| `/opt/uniter/current` | root-managed symlink to a selected release |
| `/etc/uniter/uniter.env` | root:root 0600 systemd environment (not sourced as shell) |
| `/var/lib/uniter` | uniter:uniter 0700; database and any SQLite `-wal`/`-shm` files |
| `/var/backups/uniter` | root:root 0700; backup files 0600, sensitive business/auth data |

Run these initial commands as the sudo owner. `VERSION` means the **full reviewed
40-character commit SHA**, not a moving branch or a version guessed from a tag.
The owner chooses it after review. No release publication is performed here.

```bash
set -euo pipefail
read -r -p 'Reviewed full commit SHA: ' VERSION
[[ "$VERSION" =~ ^[0-9a-f]{40}$ ]]
OWNER=$(id -un)
getent passwd uniter >/dev/null || sudo useradd --system --user-group --home-dir /var/lib/uniter --shell /usr/sbin/nologin uniter
getent passwd uniter
# If the account already existed, verify it is dedicated, non-login and unprivileged; stop otherwise.
sudo install -d -o root -g root -m 0755 /opt/uniter /opt/uniter/releases
sudo install -d -o root -g root -m 0700 /etc/uniter /var/backups/uniter
sudo install -d -o uniter -g uniter -m 0700 /var/lib/uniter
RELEASE=/opt/uniter/releases/$VERSION
test ! -e "$RELEASE"
sudo install -d -o "$OWNER" -g "$(id -gn)" -m 0750 "$RELEASE"
git clone --no-checkout https://github.com/jan-210495/Uniter.git "$RELEASE"
git -C "$RELEASE" checkout --detach "$VERSION"
test "$(git -C "$RELEASE" rev-parse HEAD)" = "$VERSION"
python3.12 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/python" -m pip install -r "$RELEASE/requirements.txt"
"$RELEASE/.venv/bin/python" -m pip check
sudo chown -R root:uniter "$RELEASE"
sudo chmod -R u=rwX,g=rX,o= "$RELEASE"
test ! -e /opt/uniter/current && test ! -L /opt/uniter/current
sudo ln -s "$RELEASE" /opt/uniter/current
sudo test ! -e /etc/uniter/uniter.env
sudo install -o root -g root -m 0600 "$RELEASE/deploy/vps/uniter.env.example" /etc/uniter/uniter.env
sudo install -o root -g root -m 0644 "$RELEASE/deploy/vps/uniter.service" /etc/systemd/system/uniter.service
```

If a command fails, **stop and resolve it**, do not skip its guard. No install
helper fetches code or rewrites an existing database. Do not use an existing
Windows/old customer/debt database for this fresh-install release.

The sample sets only real application variables:

- `UNITER_DB=/var/lib/uniter/uniter.db`: absolute SQLite **file path**, not a URL.
  Default without it would be `uniter.db` relative to the process CWD.
- `UNITER_COOKIE_SECURE=1`: forces Secure cookies behind TLS termination. Never
  change it to 0 to work around a proxy/tunnel problem. Plain HTTP browser login
  will not work reliably with Secure cookies; first setup must use HTTPS.

There is no configured application signing secret/default password to generate:
sessions, password hashes and setup state are stored in the database. Future
secrets must be created directly on the server, in appropriately protected files,
not in tracked examples or shell history. systemd reads the root-only env file
before dropping privileges; do not print it in diagnostics.

## 3. Explicit initialization, service and local checks

Uvicorn startup **does not create/seed a fresh database**. Only for the fresh,
missing file, initialize explicitly as the service user (no account is created):

```bash
set -euo pipefail
sudo test ! -e /var/lib/uniter/uniter.db
sudo test ! -L /var/lib/uniter/uniter.db
sudo -u uniter sh -c 'cd /opt/uniter/current && exec env UNITER_DB=/var/lib/uniter/uniter.db PYTHONDONTWRITEBYTECODE=1 /opt/uniter/current/.venv/bin/python -m backend.seed'
sudo chmod 0600 /var/lib/uniter/uniter.db
sudo systemd-analyze verify /etc/systemd/system/uniter.service
sudo systemctl daemon-reload
sudo systemctl start uniter
sudo systemctl status uniter --no-pager
sudo journalctl -u uniter -n 80 --no-pager
curl --fail http://127.0.0.1:8000/api/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/setup
sudo ss -ltnp
```

Expect health `{"status":"ok"}`, setup 200 on a new seeded database, and Uvicorn
listening **only on 127.0.0.1:8000**. Health is public **liveness only**; it does
not query the DB or prove setup/readiness. `/setup` rendering, and later an
actual signed-in workflow, provide additional checks. Do not expose the host if
these fail. No admin exists until the one-time setup POST succeeds.

The unit uses one worker, safe umask, explicit env/CWD, restart-on-failure, a
persistent StateDirectory, read-only code/system paths and a writable state
directory. systemd sandboxing can differ by host; inspect logs on the target.
It is not a complete isolation boundary. Nginx terminates TLS; Uvicorn trusts
forwarded headers **only from 127.0.0.1**, never `*`. No desktop/tray/browser
launcher is imported; the service uses `python -m uvicorn backend.main:app`.
Static/templates resolve relative to `backend/main.py`, not the data directory.

## 4. TLS, proxy and private first setup

Obtain a trusted certificate before configuring the supplied TLS server. With
closed inbound ports, a DNS-01 challenge is appropriate. A generic **manual**
example is below; follow Certbot prompts and create the challenge record yourself
with your DNS provider. This package does not perform DNS changes for you.
DNS API credentials, if used with a reviewed provider plugin instead, belong in
root-only server files. Never pass them in chat/Git or log their values.

```bash
set -euo pipefail
read -r -p 'Your domain (hostname only): ' DOMAIN
sudo certbot certonly --manual --preferred-challenges dns -d "$DOMAIN"
```

Manual certificates **do not renew unattended**. Schedule owner reminders well
before expiry and repeat the DNS challenge (`certbot renew` alone cannot complete
an unaided manual challenge). Prefer a reviewed DNS plugin with restricted API
credentials if you need scheduled renewal; configure and test its renewal hooks
on the target. After any renewal: `sudo nginx -t && sudo systemctl reload nginx`.
Monitor expiry and failures; do not promise unattended renewal from this bundle.

As owner, edit a copy of `deploy/vps/nginx.conf.example`: replace all four
`REPLACE_WITH_DOMAIN` strings with your domain and verify the certificate paths
exist. Install it as `/etc/nginx/sites-available/uniter` (root:root 0644), and
symlink it into `sites-enabled`. Keep `allow 127.0.0.1; deny all;` for now. Review
all enabled sites; on a *fresh dedicated host only*, disable the package default
site after confirming it serves nothing needed. Never blindly replace a running
proxy configuration. There is deliberately no port-80 HTTP listener in this
example, so only DNS-01 certificate issuance is described.

For example, copy the template to a new protected file and edit it directly
with the server editor; the certificate key remains separate and is never copied
into the config:

```bash
set -euo pipefail
sudo test ! -e /etc/nginx/sites-available/uniter
sudo install -o root -g root -m 0644 /opt/uniter/current/deploy/vps/nginx.conf.example /etc/nginx/sites-available/uniter
sudoedit /etc/nginx/sites-available/uniter
sudo test ! -e /etc/nginx/sites-enabled/uniter
sudo ln -s /etc/nginx/sites-available/uniter /etc/nginx/sites-enabled/uniter
sudo nginx -t
sudo systemctl start nginx
sudo ss -ltnp
curl --fail --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/health"
```

`nginx -t` must succeed before any start/reload. The example rejects unexpected
Host names and overwrites X-Forwarded-For with the connecting address and
X-Forwarded-Proto with Nginx's actual scheme. It strips alternative forwarded
host/port/Forwarded headers. It does not append an untrusted client chain.
Uvicorn's pinned middleware accepts these headers only from the loopback peer;
`auth.cookie_secure` uses the resulting request scheme (and the explicit Secure
override above). App access logs and Nginx access logs are off to reduce phone/
filter leakage; error logs/journal may still be sensitive. Protect and rotate
logs using host policy, and never post unredacted diagnostics.

For first setup from the owner's workstation, keep public HTTPS ingress closed.
Open an SSH local forward through your already restricted SSH access:

```bash
ssh -N -L 8443:127.0.0.1:443 OWNER_SSH_DESTINATION
```

`OWNER_SSH_DESTINATION` means your own SSH user/host or SSH-config alias, not a
literal value from this guide. On the workstation temporarily resolve your domain
to loopback (e.g. an owner-managed hosts-file entry) and browse
`https://YOUR_DOMAIN:8443/setup`. The domain must match the certificate. Do not
bypass browser TLS warnings. Nginx sees a loopback connection via this tunnel.
Do not forward HTTP 8000 for browser setup; Secure cookies require HTTPS.
Choose the initial admin password yourself, then sign out and back in. Confirm:

- TLS is valid; cookies are Secure, HttpOnly, SameSite=Lax.
- EN/AR pages/assets render; `/setup` no longer offers account creation.
- Signed-out protected APIs return 401, not data (for example
  `curl --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/transaction-ledger"`
  on the server). No CSRF bypass was configured; an authenticated state-changing
  request without its token must still be rejected.
- Existing settings/Operators/POS/history/export workflows operate as intended;
  do not create fake production financial entries just for deployment testing.

Only after the owner confirms authentication/TLS/proxy behavior privately:
remove the two Nginx allow/deny lines, run `nginx -t`, reload it, and open TCP 443
in the provider/host firewall to the intended client networks (or publicly only
if that is the owner's policy). Keep SSH restricted and 8000 blocked. The example
listens on IPv4 only: do not advertise/open IPv6 unless you explicitly configure
and review it. Remove temporary workstation DNS overrides/SSH tunnel afterwards.
Enable service startup after acceptance: `sudo systemctl enable uniter nginx`.
No public reachability or browser acceptance is claimed by this repository slice.

## 5. Backups and a disposable restore drill

Backups contain credentials/session state and private financial data. Keep them
root-only, encrypt off-host storage/transport using owner-managed keys, and test
that you can retrieve/decrypt them. Do **not** copy only the main SQLite file
while running: committed changes may reside in WAL. The application does not
currently turn WAL on itself; this procedure remains correct if WAL is enabled.
Use SQLite's online backup API (the helper uses read-only source mode and an
exclusive, 0600 destination), or stop every writer and copy the complete state
safely. Never delete active `-wal`/`-shm` files to fix a lock.

Run on the server, with ample free space. This backs up without stopping service:

```bash
set -euo pipefail
BACKUP=/var/backups/uniter/uniter-$(date -u +%Y%m%dT%H%M%SZ).db
STAGING=$(sudo -u uniter mktemp -d /var/lib/uniter/backup-XXXXXX)
sudo -u uniter /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py backup /var/lib/uniter/uniter.db "$STAGING/snapshot.db"
sudo /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py backup "$STAGING/snapshot.db" "$BACKUP"
sudo /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py check "$BACKUP"
sudo /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py drill "$BACKUP"
sudo stat -c '%U:%G %a %n' "$BACKUP"
sudo rm -- "$STAGING/snapshot.db"
sudo rmdir -- "$STAGING"
```

The live source is read as `uniter`, not root, so any WAL shared-memory sidecars
SQLite needs remain service-owned. The second copy publishes the validated,
closed snapshot as root-only backup storage; `uniter` cannot remove retained
backups. A failed step leaves its private staging directory for owner inspection;
clean only the exact generated directory after resolving the failure.

Each action must report `SQLite ACTION: OK`; any nonzero exit means **stop**.
The helper refuses overwrite/symlink destinations; it checks integrity and
foreign keys before publishing the backup. `drill` restores to a private,
disposable temp directory, checks integrity, tables and row counts, then removes
only its scratch files. It never replaces a production path. Repository tests
also verify representative row values from committed, uncheckpointed WAL data.
This drill validates SQLite recovery, **not** full application compatibility.
For release changes also start that release against a disposable backup copy,
run app-level checks privately and compare expected tables/values.

Choose and document a retention policy (e.g. recent daily copies plus weekly/
monthly history sized to your requirements). Copy verified backups off-host,
verify the off-host copy (checksum and periodic retrieval/drill), and keep a
pre-update backup with its exact code SHA. Preserve `/etc/uniter` separately
with the same secret protections; back up code identity, unit/proxy config and
certificate renewal instructions too. Prune **only named verified backup files**
after checking off-host recovery; no automatic broad deletion is supplied.
Monitor backup freshness, disk space and failures. Schedule under owner control;
this bundle does not silently install timers or promise unattended backups.

## 6. Updates, code rollback and database recovery

Maintenance requires exclusive owner control: stop other scripts/writers too.
Never run seeding on an existing production database as an update shortcut.
`backend.main` calls `upgrade_existing_database()` on **every web startup**;
it can change an existing schema before serving requests. `backend.seed` also
runs schema initialization/upgrades, so it is deliberately fresh-install-only
in this guide. No customer/debt backfill is supplied.

1. Review the new commit, release schema compatibility notes and dependency
   changes. Record `readlink -f /opt/uniter/current` and its full commit SHA.
   Close/restrict ingress for maintenance, stop `uniter`, confirm no other writer.
2. Take and check/drill a **pre-update** backup using section 5 while stopped.
   If backup/validation fails, stop the update; leave the old code selected.
3. Create a **new** `/opt/uniter/releases/NEW_FULL_SHA` and venv using section 2's
   clone/check/build/permissions steps only. Do not rerun its fresh path/env/DB
   commands. Leave old release, DB, environment, account state and TLS keys intact.
4. Make a separate disposable copy of the verified backup with `sqlite_backup.py
   backup BACKUP NEW_TEMP_DB`. As an unprivileged test user, set `UNITER_DB` to
   that copy and start the candidate using the same command on a different
   loopback-only port. It will run its startup migrations on the **copy**. Check
   startup, integrity and protected app workflows privately; never point the
   candidate test at the live DB. Remove only your disposable test artifacts.
5. With service still stopped and backup verified, switch the code symlink:

```bash
set -euo pipefail
# Set NEW_RELEASE to the reviewed, built, root-owned absolute release directory.
read -r -p 'New absolute release directory: ' NEW_RELEASE
[[ "$NEW_RELEASE" =~ ^/opt/uniter/releases/[0-9a-f]{40}$ ]]
sudo test -d "$NEW_RELEASE/.venv"
sudo ln -s "$NEW_RELEASE" /opt/uniter/current.next
sudo mv -T /opt/uniter/current.next /opt/uniter/current
sudo systemctl start uniter
sudo systemctl status uniter --no-pager
sudo journalctl -u uniter -n 80 --no-pager
curl --fail http://127.0.0.1:8000/api/health
```

6. If any command/check fails, **keep ingress closed**, stop the service, inspect
   logs without exposing secrets, and follow recovery below. A process can fail
   *after* some startup migration work: never assume failure implies unchanged
   DB. On success, check authentication, correct DB/workflows and backups before
   restoring intended ingress. Keep old code and pre-update backups until an
   explicit owner decision.

**Code-only rollback:** only when the old version is confirmed compatible with
all changes already made to the current DB, stop the service and switch the
symlink back to the recorded old release (same `current.next` procedure), then
start/check privately. Do not reseed, remove schema markers or reverse migrations.
Do not assume changing Git code reverses a database upgrade.

**Database recovery:** if old code is incompatible, either fix forward with the
new schema or explicitly restore a verified compatible pre-update backup **and
its matching code**, accepting loss of all writes since the backup. Never do
this automatically. Preserve the failed/current DB for investigation first:

- Stop `uniter` and every writer; ensure no restart is running. Check/drill the
  chosen backup. Record its matching code SHA and timestamp.
- As root, create a new private quarantine directory outside `/var/lib/uniter`.
  Move `/var/lib/uniter/uniter.db` **and any** `uniter.db-wal`/`uniter.db-shm`/`uniter.db-journal`
  there together while all writers are stopped; preserve ownership/modes. Do
  not delete them, and do not leave old sidecars beside a replacement main DB.
- With the original path absent, run `sqlite_backup.py backup CHOSEN_BACKUP
  /var/lib/uniter/uniter.db` (it refuses overwrite). Set the restored file to
  `uniter:uniter`, mode `0600`; directory remains `0700`. Run helper `check`.
- Select the recorded compatible code release, keep Secure-cookie env unchanged,
  start privately and verify integrity and app/account/financial workflows.
  Restoring an old DB also restores its account/password/session state: treat
  old sessions/credentials as sensitive and review access before reopening.
- If validation fails, stop; retain quarantine and backups and escalate for
  local expert recovery. Do not initialize a new blank DB over evidence.

A guarded command sequence for the **explicitly chosen database-recovery** path
is below. It intentionally does not start the app: first select the matching
compatible code, then perform the private checks described above. Do not execute
it for a routine code-only rollback. Quarantine is preserved, not auto-deleted.

```bash
set -euo pipefail
read -r -p 'Absolute path of verified compatible backup: ' CHOSEN_BACKUP
[[ "$CHOSEN_BACKUP" = /var/backups/uniter/*.db ]]
sudo /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py check "$CHOSEN_BACKUP"
sudo /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py drill "$CHOSEN_BACKUP"
read -r -p 'This loses post-backup writes. Type RESTORE to confirm: ' CONFIRM
test "$CONFIRM" = RESTORE
sudo systemctl stop uniter
if sudo systemctl is-active --quiet uniter; then exit 1; fi
# Owner must also confirm all manual/candidate/backup writers are stopped.
sudo test -f /var/lib/uniter/uniter.db
QUARANTINE=$(sudo mktemp -d /var/backups/uniter/quarantine-XXXXXX)
for SUFFIX in '' -wal -shm -journal; do
    if sudo test -e "/var/lib/uniter/uniter.db$SUFFIX"; then
        sudo mv "/var/lib/uniter/uniter.db$SUFFIX" "$QUARANTINE/"
    fi
done
sudo /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py backup "$CHOSEN_BACKUP" /var/lib/uniter/uniter.db
sudo chown uniter:uniter /var/lib/uniter/uniter.db
sudo chmod 0600 /var/lib/uniter/uniter.db
sudo -u uniter /opt/uniter/current/.venv/bin/python /opt/uniter/current/deploy/vps/sqlite_backup.py check /var/lib/uniter/uniter.db
# STOP HERE until matching code is selected and the owner is ready for private validation.
```

## 7. Troubleshooting / owner checklist

- Missing tables or persistent setup errors: confirm explicit fresh initialization
  and absolute `UNITER_DB`; do not seed an existing live DB to hide the problem.
- `Permission denied` / read-only DB: inspect `namei -l`, `stat`, service user,
  state directory and sidecar ownership; do not use `chmod 777` or run as root.
- Secure cookies/login loop: use the real certificate hostname through HTTPS;
  inspect proxy scheme, allowed peer and env setting. Never disable CSRF/auth
  or set Secure=0. Forwarded-for errors can also group login rate limits wrongly.
- Nginx 502: check unit status/journal, loopback listener and startup schema errors.
  Nginx 403 during private staging is expected outside the permitted tunnel.
- Health 200 but unusable app: health is not readiness; check DB/setup/login.
- SQLite locked / disk full: stop maintenance writers, free space safely and
  inspect storage; do not remove WAL/journal files or reset accounts.
- Migration/schema incompatibility: leave ingress closed, preserve the DB and
  recover with its compatible code/backup; no destructive reversal scripts.
- Lost admin password: follow README's local administrator recovery only after
  a verified backup and stopped service; never reopen setup or add a default user.

Before public availability the owner checks: [ ] reviewed immutable code/runtime;
[ ] dedicated user and restrictive state/env modes; [ ] loopback-only Uvicorn;
[ ] trusted local proxy overwrites forwarding headers; [ ] valid TLS/renewal plan;
[ ] private first admin setup and secure cookies/CSRF; [ ] both-language workflows;
[ ] firewall/SSH/IPv6 scope; [ ] verified off-host backup and disposable restore;
[ ] recorded update/rollback compatibility and monitoring/retention responsibility.

### Source evidence and sandbox verification

`backend/database.py`: `DATABASE_URL` reads `UNITER_DB`; `init_db` creates schema;
`upgrade_existing_database` guards existing schema changes. `backend/seed.py`:
`main` calls `init_db` and `seed_data`, not an admin/password creator.
`backend/main.py`: startup upgrades existing DB; `/api/health` is a static liveness
response; assets use paths relative to this module. `backend/auth.py`:
`PUBLIC_PATHS`, setup/session/CSRF gate and `cookie_secure` implement the described
behavior. Uvicorn **0.28.0** `ProxyHeadersMiddleware` checks the connecting peer
against `forwarded_allow_ips` before applying forwarded scheme/client values.
`requirements.txt`, `uniter.spec`, `scripts/build_exe.py` and desktop startup were
inspected; desktop tray imports are not part of the web service entry point.

See [VPS verification evidence](vps-verification.md) for exact repository checks
and limitations. No VPS, DNS, firewall, systemd service or live certificate was
accessed/changed by this work. Target-host testing and the final integrated
browser/product acceptance remain the owner's responsibility.
