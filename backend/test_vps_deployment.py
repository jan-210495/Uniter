"""Repository-only deployment checks: never touch service paths or a VPS."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux VPS deployment artifacts')

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'deploy' / 'vps'
spec = importlib.util.spec_from_file_location('vps_backup', BUNDLE / 'sqlite_backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def invoke(*args, ok=True):
    result = subprocess.run([sys.executable, str(BUNDLE / 'sqlite_backup.py'), *map(str, args)],
                            text=True, capture_output=True, timeout=30)
    assert (result.returncode == 0) == ok, result.stderr
    return result


def test_wal_backup_restore_drill_and_representative_values(tmp_path):
    source = tmp_path / 'live.db'
    connection = sqlite3.connect(source)
    try:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE shop (id INTEGER PRIMARY KEY, name TEXT, cents INTEGER)')
        connection.execute('INSERT INTO shop VALUES (1, ?, 12345)', ('متجر <&>',))
        connection.commit()
        # Keep writer connection open: the data really is in an uncheckpointed WAL.
        assert Path(str(source) + '-wal').stat().st_size > 0
        destination = tmp_path / 'backup.db'
        assert invoke('backup', source, destination).stdout.strip() == 'SQLite backup: OK'
        assert destination.stat().st_mode & 0o777 == 0o600
        assert invoke('check', destination).stdout.strip() == 'SQLite check: OK'
        assert invoke('drill', destination).stdout.strip() == 'SQLite drill: OK'
        restored = tmp_path / 'restored.db'
        backup.backup(destination, restored)
        with sqlite3.connect(restored) as check:
            assert check.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            assert check.execute('SELECT * FROM shop').fetchall() == [(1, 'متجر <&>', 12345)]
            assert check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('shop',)]
        # Reading/backing up did not change source data.
        assert connection.execute('SELECT * FROM shop').fetchall() == [(1, 'متجر <&>', 12345)]
        invoke('backup', source, destination, ok=False)
        target = tmp_path / 'alias.db'
        target.symlink_to(destination)
        invoke('backup', source, target, ok=False)
        assert not list(tmp_path.glob('.uniter-backup-*'))
    finally:
        connection.close()


def test_backup_failures_do_not_publish_or_create_source(tmp_path):
    missing = tmp_path / 'missing.db'
    output = tmp_path / 'output.db'
    invoke('backup', missing, output, ok=False)
    assert not missing.exists() and not output.exists()
    corrupt = tmp_path / 'corrupt.db'
    corrupt.write_bytes(b'not a database')
    invoke('backup', corrupt, output, ok=False)
    assert not output.exists() and not list(tmp_path.glob('.uniter-backup-*'))
    empty = tmp_path / 'empty.db'
    sqlite3.connect(empty).close()
    invoke('check', empty, ok=False)
    with sqlite3.connect(tmp_path / 'foreign.db') as db:
        db.executescript('CREATE TABLE p(id INTEGER PRIMARY KEY); CREATE TABLE c(pid REFERENCES p(id)); INSERT INTO c VALUES(9);')
    invoke('backup', tmp_path / 'foreign.db', output, ok=False)
    assert not output.exists()


def test_bundle_env_service_and_proxy_static_contracts():
    env = (BUNDLE / 'uniter.env.example').read_text()
    settings = dict(line.split('=', 1) for line in env.splitlines() if line and not line.startswith('#'))
    assert settings == {'UNITER_DB': '/var/lib/uniter/uniter.db', 'UNITER_COOKIE_SECURE': '1'}
    unit = (BUNDLE / 'uniter.service').read_text()
    for required in ('User=uniter', 'Group=uniter', 'WorkingDirectory=/opt/uniter/current',
                     'EnvironmentFile=/etc/uniter/uniter.env', 'UMask=0077',
                     'ReadWritePaths=/var/lib/uniter', 'StateDirectoryMode=0700',
                     'ProtectSystem=strict', 'NoNewPrivileges=true', 'Restart=on-failure',
                     '--host 127.0.0.1', '--workers 1', '--forwarded-allow-ips=127.0.0.1'):
        assert required in unit
    assert '--reload' not in unit and 'ExecStartPre' not in unit and 'backend.desktop' not in unit
    proxy = (BUNDLE / 'nginx.conf.example').read_text()
    for line in ('listen 443 ssl;', 'allow 127.0.0.1;', 'deny all;',
                 'proxy_pass http://127.0.0.1:8000;', 'proxy_set_header X-Forwarded-Proto $scheme;',
                 'proxy_set_header X-Forwarded-For $remote_addr;', 'proxy_set_header Forwarded "";'):
        assert line in proxy
    assert '$proxy_add_x_forwarded_for' not in proxy and 'listen 8000' not in proxy
    assert 'REPLACE_WITH_DOMAIN' in proxy
    # No broad proxy trust, password value, or bootstrap account in sample config.
    assert 'forwarded-allow-ips=*' not in unit
    assert all('password' not in line.lower() for line in settings)


def test_systemd_syntax_disposable_paths(tmp_path):
    executable = shutil.which('systemd-analyze')
    if not executable:
        pytest.skip('systemd-analyze unavailable; static directive checks still run')
    # verify ExecStart existence without creating any real /opt or /etc files.
    unit = (BUNDLE / 'uniter.service').read_text().replace(
        '/opt/uniter/current/.venv/bin/python', sys.executable)
    path = tmp_path / 'uniter.service'
    path.write_text(unit)
    result = subprocess.run([executable, 'verify', str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_documented_bash_blocks_parse_without_execution(tmp_path):
    doc = (ROOT / 'docs' / 'vps-deployment.md').read_text()
    blocks = re.findall(r'```bash\n(.*?)```', doc, re.S)
    assert len(blocks) >= 6
    for index, block in enumerate(blocks):
        script = tmp_path / f'example-{index}.sh'
        script.write_text(block)
        result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    for text in ('UNITER_DB', 'UNITER_COOKIE_SECURE', 'backend.seed', '127.0.0.1',
                 'code-only rollback', 'foreign keys', 'WAL', 'disposable'):
        assert text.lower() in doc.lower()


def request(port, path, headers=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers=headers or {})
    try:
        response = urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), response.headers


def test_service_web_entrypoint_setup_assets_and_trusted_proxy(tmp_path):
    # Uses the same interpreter/module/arguments as the service with only paths
    # and a disposable loopback port substituted. No systemd/service install.
    db = tmp_path / 'data' / 'uniter.db'
    db.parent.mkdir(mode=0o700)
    env = {**os.environ, 'UNITER_DB': str(db), 'UNITER_COOKIE_SECURE': '1',
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT)}
    subprocess.run([sys.executable, '-m', 'backend.seed'], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=30, check=True)
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT count(*) FROM users').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM operators').fetchone() == (2,)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    args = [sys.executable, '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1',
            '--port', str(port), '--workers', '1', '--proxy-headers',
            '--forwarded-allow-ips=127.0.0.1', '--no-access-log']
    with (tmp_path / 'service.log').open('w+') as log:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    status, body, _ = request(port, '/api/health')
                    if status == 200:
                        break
                except (OSError, TimeoutError):
                    time.sleep(0.1)
            else:
                pytest.fail('loopback app did not start')
            assert process.poll() is None
            assert json.loads(body) == {'status': 'ok'}
            assert request(port, '/setup')[0] == 200
            assert request(port, '/api/transaction-ledger')[0] == 401
            assert request(port, '/static/csrf.js')[0] == 200
            # Trusted loopback forwarding turns the setup CSRF cookie Secure.
            status, _, headers = request(port, '/setup', {'X-Forwarded-Proto': 'https'})
            assert status == 200
            cookies = '; '.join(headers.get_all('Set-Cookie') or [])
            assert 'Secure' in cookies and 'HttpOnly' in cookies and 'SameSite=lax' in cookies
            subprocess.run([sys.executable, '-c', "import backend.main, sys; assert not any(n.startswith(('pystray', 'PIL')) for n in sys.modules)"],
                           cwd=ROOT, env=env, capture_output=True, timeout=10, check=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_pinned_proxy_peer_trust_and_automatic_cookie_scheme(monkeypatch):
    import asyncio
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from backend.auth import cookie_secure

    monkeypatch.delenv('UNITER_COOKIE_SECURE', raising=False)
    seen = []

    async def app(scope, receive, send):
        seen.append((scope['scheme'], scope['client'][0], cookie_secure(Request(scope))))

    middleware = ProxyHeadersMiddleware(app, trusted_hosts='127.0.0.1')
    async def exercise(peer):
        scope = {'type': 'http', 'scheme': 'http', 'method': 'GET', 'path': '/',
                 'client': (peer, 10000), 'server': ('127.0.0.1', 8000),
                 'headers': [(b'x-forwarded-proto', b'https'), (b'x-forwarded-for', b'198.51.100.1')]}
        await middleware(scope, None, None)
    asyncio.run(exercise('127.0.0.1'))
    asyncio.run(exercise('127.0.0.2'))
    assert seen == [('https', '198.51.100.1', True), ('http', '127.0.0.2', False)]
