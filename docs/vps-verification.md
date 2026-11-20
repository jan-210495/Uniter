# Slice 7 — VPS bundle verification

## Baseline / boundaries

Fetched origin first; both `refs/heads/release/1.0.5` and
`refs/heads/release/1.0.5-slice-7-rollback` resolved to required base
`1b650918c4e8126678efc6f40ee549c98bbb605d`. Working/push branch is
`arena/48839799-uniter`. Starting working tree was clean.

**No server was accessed or changed.** No VPS login, provisioning, DNS/firewall
changes, live TLS, deployment, service installation, release merge/tag/publication,
Windows build or version metadata changes were performed. The only app processes
started were disposable loopback subprocesses in tests with temporary databases.
No application/accounting/runtime dependency changes and no unrelated test
assertion changes. The new Linux-only tests skip on non-Linux platforms.

## Files / design findings

- `README.md`: points to separate source-based Linux web deployment path.
- `docs/vps-deployment.md`: owner steps, checklist, prerequisites, private setup,
  TLS/proxy/firewall, persistence, logs, backup/restore/update/rollback/recovery.
- `deploy/vps/uniter.service`: non-root service, immutable release symlink/CWD,
  single loopback worker, explicitly bounded proxy trust, env path, restrictive
  umask/data mode, restart and filesystem restrictions.
- `deploy/vps/uniter.env.example`: only actual app variables, no credentials.
- `deploy/vps/nginx.conf.example`: TLS-only local proxy, private-staging ACL,
  overwritten forwarded scheme/client IP, stripped alternative forwarded headers.
- `deploy/vps/sqlite_backup.py`: stdlib online backup, read-only source, validated
  no-clobber 0600 output, integrity/foreign-key checks and disposable restore drill.
- `backend/test_vps_deployment.py`: artifact checks and disposable operational tests.
- `docs/vps-verification.md`: this evidence.

Source review confirmed:

1. `backend/database.py` uses `UNITER_DB` as a file path (default relative
   `uniter.db`); absolute state directory is supported. It enables foreign keys,
   not WAL by default. SQLite still needs sidecar write permissions in data dir.
2. `backend.seed.main` calls `init_db` and seeds missing operator/service/settings
   configuration; it creates no admin. Fresh initialization is explicit and runs
   as `uniter`; normal service start never runs seed automatically.
3. `backend.main` startup calls `upgrade_existing_database` and can migrate an
   existing schema before requests. Missing DB is not seeded by web startup.
   Therefore updates require a checked pre-start backup and compatibility review.
4. `auth.cookie_secure` uses `UNITER_COOKIE_SECURE` or request scheme; pinned
   Uvicorn 0.28.0 ProxyHeadersMiddleware only applies headers from configured
   peer IPs. Nginx overwrites, rather than appends, client forwarding input; trust
   is restricted to 127.0.0.1. Explicit Secure=1 remains enabled.
5. Sessions/auth/setup state live in DB; there is no application signing secret
   or default password to ship. One-time setup and CSRF behavior are unchanged.
6. `/api/health` is public static liveness, not DB readiness. Assets resolve from
   source-relative paths. Web entry point does not import desktop/tray modules.
7. Runtime requirements, `uniter.spec`, `scripts/build_exe.py` and desktop seed/
   startup were inspected. The desktop executable is not used for VPS startup.
   Runtime dependencies install on Linux; no package split/fix was necessary.

## Exact checks

Python: **3.12.8**. Full backend verification environment uses the declared
runtime requirements plus existing test-only reader requirements.

Focused repository checks (exit 0):

```text
python3 -m pytest backend/test_vps_deployment.py --tb=short -q
7 passed in 4.12s
```

Clean environment created with runtime requirements only (exit 0):

```text
uv venv --python .venv/bin/python .cache/vps-runtime
uv pip install --python .cache/vps-runtime/bin/python -r requirements.txt
.cache/vps-runtime/bin/python -m pytest backend/test_vps_deployment.py --tb=short -q
7 passed in 8.09s
uv pip check --python .cache/vps-runtime/bin/python
All installed packages are compatible
```

Earlier runtime-only environment metadata checks matched every pinned runtime
version and confirmed test-only openpyxl absent. The clean tests run the actual
`backend.seed` and `python -m uvicorn backend.main:app` with service flags (only
port/interpreter/state paths substituted), check public health, first setup,
protected API rejection, assets, Secure/HttpOnly/SameSite cookie, and app import
without pystray/PIL. Proxy middleware tests prove trusted loopback forwarding
changes scheme/client and an untrusted peer's spoofed headers are ignored.

- `bash -n`: **9 documentation Bash blocks** parsed, none executed. No shell
  installer/update script is shipped; owner steps keep privileged decisions
  explicit. Config directives are tested statically and paths remain disposable
  in tests. No real service directories are written by tests.
- `systemd-analyze` **252** available. `verify deploy/vps/uniter.service` reports
  only the expected missing `/opt/uniter/current/.venv/bin/python` executable
  (exit 1; this is an uninstalled template). A temp copy with only ExecStart's
  interpreter substituted by the test interpreter passes `systemd-analyze verify`
  (exit 0). No systemd runtime/service sandbox validation is claimed.
- Nginx executable **not available**. Proxy directives/ACL/TLS/forwarded-header
  expectations are statically asserted; `nginx -t`, certificate validation and
  renewal remain mandatory on the target host. No live TLS test is claimed.
- WAL test leaves a writer connection open, commits representative Arabic text
  and numeric data into an uncheckpointed WAL, performs the online backup and
  restore drill, verifies integrity, schema/table names and exact row values,
  checks 0600 output, confirms no source changes, and refuses missing/corrupt/
  foreign-key-invalid sources and existing/symlink destinations.
- Owner procedure reads the live DB as `uniter` into protected staging so any
  WAL shared-memory sidecars stay service-owned; a second validated copy moves
  the snapshot into root-only retained storage. Recovery quarantines main DB
  and WAL/SHM/journal together with all writers stopped. It never auto-rolls DB
  backward on a code rollback, and never overwrites a destination.

Full required suite (exit 0):

```text
python3 -m pytest backend/ --tb=short -q
892 passed, 724 warnings in 193.36s (0:03:13)
```

Warnings are existing dependency deprecations (FastAPI lifecycle,
Starlette/AnyIO and HTTPX TestClient), not test failures.

`git diff --check` and staged check passed before commit; complete README,
bundle, tests and documentation reviewed against source, including root/service
ownership, read-only code vs writable state, backup no-clobber/failure behavior,
TLS cookie/proxy path, and code-vs-DB rollback boundaries. Test logs are under
`/tmp/slice7-focused.log`, `/tmp/slice7-clean-runtime.log`, `/tmp/slice7-full.log`
and `/tmp/slice7-systemd-original.log` in this sandbox, not versioned data.

## Owner limitations / outstanding checks

No actual systemd start, Nginx parser, target package manager, firewall, DNS,
certificate issuance/renewal, internet reachability, browser acceptance or
production restore was tested. An owner must validate these on the target and
perform the integrated product test. Python 3.12 must be supplied by a maintained
system-wide installation; the bundle does not modify a distribution's Python.
Manual DNS-01 renewal requires repeated owner action; no unattended promise.
Backups need chosen retention, encryption/off-host storage, monitoring, retrieval
and app-level compatibility drills. One host/process/SQLite only; no HA or load
benchmark. No passwords, private hosts, real data or deployment credentials are
included. No server was accessed or changed.
