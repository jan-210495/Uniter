# Uniter guided Debian/Ubuntu installer (source only)

The CLI installer is `deploy/vps/install.sh`. It is intentionally **fresh-host-only**. It checks OS/Python/prerequisites, offers Install / Read-only check / Safety notes / Exit, asks for a release tag and DNS hostname, prints the scope, and requires confirmation before making changes. It installs a dedicated non-root service, protected files, a venv, and a fresh SQLite database; it does not create an admin user. First admin password is chosen in the browser.

## Run

The wizard script is **not included in the published v1.0.5 tag**. Do not use
this tag to fetch the installer. After a reviewed follow-up tag is published,
replace `<reviewed-tag>` below with that exact tag, inspect the download, and
run only on a fresh disposable Debian/Ubuntu server:

```bash
curl -fsSLO https://raw.githubusercontent.com/jan-210495/Uniter/<reviewed-tag>/deploy/vps/install.sh
less install.sh
sudo bash install.sh
```

On Debian/Ubuntu, the wizard checks which Python version the OS supports: Debian 13/Trixie can use its native Python 3.13 (`python3` and `python3-venv`); releases with supported Python 3.12 packages can use those. It lists and asks before installing missing packages from the configured repositories, and never replaces the system Python or adds a PPA. If that OS doesn't provide Python 3.12 or newer from its current repositories, it stops and explains what's needed.

Do not run this on TrueNAS or any host with data/services you need. The current prototype lives on the pushed feature branch and is not included in v1.0.5; it has mock/preflight tests but not a real disposable-VM installation yet.

## What it automates

- Dedicated `uniter` system user, protected `/opt/uniter`, `/etc/uniter`, `/var/lib/uniter`, `/var/backups/uniter` paths.
- Exact tagged source checkout, isolated Python 3.12 environment and pinned runtime requirements.
- Root-only environment file; non-root systemd unit; one-time fresh schema/operator/settings seed.
- Nginx HTTPS example with the entered hostname; the shipped localhost-only staging policy remains.
- Service start plus local liveness, setup-route and loopback-listener checks.

## What remains owner-controlled

The existing Cloudflared public SSH route at `ssh.abboud.cloud` points to
TrueNAS's own `localhost:22`; the domain is expired. Do not use or modify that
hostname until an owned-domain decision is made. Cloudflare private-network
routing is separate: it uses a WARP-enrolled client plus a private route for the
VM subnet/IP (for this single VM, prefer `192.168.1.20/32` if the dashboard
supports host routes), and the connector must advertise/reach that subnet. Keep
TrueNAS managed by its LAN IPs `192.168.1.31` / `192.168.3.31`; SSH to both
systems uses port 22 but distinct IPs. Before adding a private route, inspect
current route conflicts and connector network settings in the Zero Trust
Dashboard; do not assume a route configured there implies the client is using
WARP. No Cloudflare settings are changed by this project package.


## Safety and recovery

- Existing service, service user, install/config/state/backup directories, service file, Nginx site, or database: **refuse and stop**.
- The wizard selects Python 3.12 or newer. Debian 13/Trixie uses its supported `python3`/`python3-venv` packages; on other supported Debian/Ubuntu releases it offers only interpreter packages known to exist in that release's configured repositories. If a suitable interpreter is unavailable, it stops before installing Uniter and explains the prerequisite; it never changes the system default Python or adds a third-party repository.
- If a command fails after creation starts, stop and inspect its last completed stage. The script does **not** claim a whole-host transactional rollback: preserve generated files for inspection and use the documented manual recovery. Never blindly rerun after a partial install.
- No admin password, API token, DNS key, or private key is prompted for, embedded, or logged.

The installer has not been run against TrueNAS or a production VPS. Its tests must use disposable Debian/Ubuntu guests or mocked commands only.
