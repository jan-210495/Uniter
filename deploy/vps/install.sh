#!/usr/bin/env bash
# Guided fresh-host Uniter installer for Debian/Ubuntu.
# No firewall, SSH, DNS, Cloudflare, certificate, or public-exposure changes.
set -Eeuo pipefail
IFS=$'\n\t'

REPO_URL="https://github.com/jan-210495/Uniter.git"
INSTALL_ROOT="/opt/uniter"
ENV_DIR="/etc/uniter"
STATE_DIR="/var/lib/uniter"
BACKUP_DIR="/var/backups/uniter"
NGINX_SITE="/etc/nginx/sites-available/uniter"
UNIT_FILE="/etc/systemd/system/uniter.service"
PREFIX="[uniter-installer]"

say() { printf '\n%s %s\n' "$PREFIX" "$*"; }
fail() { printf '\n%s ERROR: %s\n' "$PREFIX" "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "Required command missing: $1"; }
confirm() { local answer; read -r -p "$1 [y/N] " answer; [[ "$answer" == [yY] ]]; }
refuse_path() { [[ ! -e "$1" && ! -L "$1" ]] || fail "Found existing $2 at $1. This is fresh-install only; nothing was overwritten."; }

trap 'rc=$?; printf "\n%s Stopped at line %s (exit %s). No automatic rollback was attempted. Review the last stage before retrying.\n" "$PREFIX" "$LINENO" "$rc" >&2; exit "$rc"' ERR

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "Start with: sudo bash $0"
[[ -r /etc/os-release ]] || fail "Cannot identify this operating system."
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == debian || ${ID:-} == ubuntu || ${ID_LIKE:-} == *debian* ]] || fail "Supported systems are Debian/Ubuntu; found ${PRETTY_NAME:-unknown}."
for cmd in systemctl systemd-analyze install getent id python3 git curl ss useradd runuser; do need "$cmd"; done

say "Welcome. This wizard installs Uniter on a fresh Debian/Ubuntu server."
printf '\n  1) Install on this fresh server\n  2) Check an existing Uniter service (read-only)\n  3) Safety notes\n  4) Exit\n'
read -r -p 'Choose 1-4 [1]: ' choice
choice=${choice:-1}
case "$choice" in
  1) ;;
  2)
    systemctl cat uniter >/dev/null 2>&1 || fail "No Uniter system service is installed."
    systemctl --no-pager --full status uniter || true
    printf '\nLocal health: '; curl --silent --show-error --max-time 3 http://127.0.0.1:8000/api/health || true
    printf '\nListener:\n'; ss -lntp | grep -E '(:8000|:443)[[:space:]]' || true
    exit 0 ;;
  3)
    printf '\nFresh host only. No firewall/DNS/Cloudflare/SSH/certificate changes.\nUvicorn stays on loopback; Nginx remains localhost-only. You choose the admin password in the browser. No upgrade or restore is offered.\n'
    exit 0 ;;
  4) say "Cancelled without changes."; exit 0 ;;
  *) fail "Choose 1, 2, 3, or 4." ;;
esac

# Read-only preflight before asking for install details or changing anything.
[[ -z "$(id -u uniter 2>/dev/null || true)" ]] || fail "The 'uniter' service account already exists; refusing this host."
systemctl cat uniter >/dev/null 2>&1 && fail "An Uniter service already exists; this wizard never upgrades." || true
for path in "$INSTALL_ROOT" "$ENV_DIR" "$STATE_DIR" "$BACKUP_DIR" "$NGINX_SITE" "$UNIT_FILE"; do refuse_path "$path" "Uniter path/configuration"; done
command -v python3.12 >/dev/null 2>&1 || fail "Python 3.12 is required. Install it using your OS's supported packages; this script will not replace system Python or add repositories."
python3.12 -c 'import ssl, sqlite3, venv; print("Python", __import__("sys").version.split()[0], "SQLite", sqlite3.sqlite_version)' || fail "Python 3.12 needs SSL, SQLite, and venv support."

printf '\nRelease to install (tag, e.g. v1.0.5).\n'
read -r -p 'Release tag [v1.0.5]: ' tag
tag=${tag:-v1.0.5}
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Use a version tag like v1.0.5."
printf '\nHostname you plan to use for HTTPS (e.g. uniter.example.com; no scheme/port).\n'
read -r -p 'DNS hostname: ' domain
[[ $domain =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && $domain == *.* && $domain != *..* ]] || fail "Enter a valid DNS hostname."

say "Review this plan:"
printf '  • Install %s from %s\n' "$tag" "$REPO_URL"
printf '  • Create a dedicated non-root service and protected data directory\n'
printf '  • Keep Uniter private on 127.0.0.1; prepare localhost-only Nginx example for %s\n' "$domain"
printf '  • Initialize a brand-new database; no admin account/password will be created here\n'
printf '  • Do NOT change firewall, SSH, DNS, Cloudflare, certificates, or public access\n'
confirm "Proceed with these system changes?" || { say "Cancelled before changes."; exit 0; }
# Repeat path checks immediately before mutation.
[[ -z "$(id -u uniter 2>/dev/null || true)" ]] || fail "The 'uniter' account appeared during preflight; stopping."
systemctl cat uniter >/dev/null 2>&1 && fail "An Uniter service appeared during preflight; stopping." || true
for path in "$INSTALL_ROOT" "$ENV_DIR" "$STATE_DIR" "$BACKUP_DIR" "$NGINX_SITE" "$UNIT_FILE"; do refuse_path "$path" "Uniter path/configuration"; done
refuse_path "$INSTALL_ROOT/current" "active release symlink"

say "Creating the service account and protected directories."
useradd --system --user-group --home-dir "$STATE_DIR" --shell /usr/sbin/nologin uniter
install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
install -d -o root -g root -m 0700 "$ENV_DIR" "$BACKUP_DIR"
install -d -o uniter -g uniter -m 0700 "$STATE_DIR"

release_dir="$INSTALL_ROOT/releases/$tag"
refuse_path "$release_dir" "release directory"
install -d -o root -g uniter -m 0750 "$release_dir"
refuse_path "$INSTALL_ROOT/current" "active release symlink"
say "Fetching and verifying the exact tagged source."
git clone --quiet --no-checkout "$REPO_URL" "$release_dir"
git -C "$release_dir" fetch --quiet --depth 1 origin "refs/tags/$tag:refs/tags/$tag"
git -C "$release_dir" checkout --quiet --detach "$tag"
[[ $(git -C "$release_dir" describe --tags --exact-match HEAD) == "$tag" ]] || fail "Source checkout did not match tag $tag."
commit=$(git -C "$release_dir" rev-parse HEAD)
printf 'Source commit: %s\n' "$commit"

say "Creating the Python environment and installing runtime requirements."
python3.12 -m venv "$release_dir/.venv"
"$release_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r "$release_dir/requirements.txt"
"$release_dir/.venv/bin/python" -m pip check
chown -R root:uniter "$release_dir"
chmod -R u=rwX,g=rX,o= "$release_dir"
ln -s "$release_dir" "$INSTALL_ROOT/current"

say "Installing protected configuration and system service."
install -d -o root -g root -m 0755 "$(dirname "$NGINX_SITE")"
install -o root -g root -m 0600 "$release_dir/deploy/vps/uniter.env.example" "$ENV_DIR/uniter.env"
install -o root -g root -m 0644 "$release_dir/deploy/vps/uniter.service" "$UNIT_FILE"
install -o root -g root -m 0644 "$release_dir/deploy/vps/nginx.conf.example" "$NGINX_SITE"
python3 - "$NGINX_SITE" "$domain" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); domain = sys.argv[2]
text = p.read_text(encoding="utf-8")
if text.count("REPLACE_WITH_DOMAIN") != 4:
    raise SystemExit("Nginx template did not contain the expected four hostname placeholders")
p.write_text(text.replace("REPLACE_WITH_DOMAIN", domain), encoding="utf-8")
PY

say "Initializing a new database only (configuration rows; no admin or sale data)."
[[ ! -e "$STATE_DIR/uniter.db" && ! -L "$STATE_DIR/uniter.db" ]] || fail "Database path exists; refusing overwrite."
runuser -u uniter -- env UNITER_DB="$STATE_DIR/uniter.db" PYTHONDONTWRITEBYTECODE=1 "$release_dir/.venv/bin/python" -m backend.seed
chmod 0600 "$STATE_DIR/uniter.db"

say "Checking the service unit and starting Uniter privately."
systemd-analyze verify "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --now uniter
systemctl --no-pager --full status uniter
for _ in {1..20}; do
  curl --silent --show-error --fail http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
  sleep 1
done
curl --silent --show-error --fail http://127.0.0.1:8000/api/health >/dev/null || fail "Health check failed; see journalctl -u uniter."
setup_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8000/setup)
[[ $setup_code == 200 ]] || fail "Expected fresh /setup status 200; received $setup_code."
ss -lntp | grep -E 'LISTEN[^\n]*127\.0\.0\.1:8000' >/dev/null || fail "Expected Uvicorn listener on loopback 127.0.0.1:8000."
if ss -lntp | grep -E 'LISTEN[^\n]*(0\.0\.0\.0:8000|\[::\]:8000)' >/dev/null; then fail "Uniter listener is not loopback-only; stop and investigate."; fi

say "Fresh install staged successfully. No public access was enabled."
printf '\nNext: review docs/vps-deployment.md for private DNS/TLS/SSH-tunnel steps; open /setup privately and choose your admin password.\n'
printf 'Service: systemctl status uniter\nLogs: journalctl -u uniter -n 80 --no-pager\nHealth: curl --fail http://127.0.0.1:8000/api/health\n'
