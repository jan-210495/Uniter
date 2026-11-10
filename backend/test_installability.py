"""Installability contracts: isolated HTTP, actual assets, rendered DOM and JS."""
import struct
import zlib
from pathlib import Path


def png_pixels(data):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    width, height, depth, color = struct.unpack('>IIBB', data[16:26])
    assert (depth, color) == (8, 2)  # opaque RGB icons
    pos, chunks = 8, []
    while pos < len(data):
        size = struct.unpack('>I', data[pos:pos+4])[0]
        kind, chunk = data[pos+4:pos+8], data[pos+8:pos+8+size]
        assert struct.unpack('>I', data[pos+8+size:pos+12+size])[0] == zlib.crc32(kind+chunk)
        if kind == b'IDAT':
            chunks.append(chunk)
        pos += size + 12
    raw = zlib.decompress(b''.join(chunks))
    stride = width * 3 + 1
    assert len(raw) == stride * height
    assert all(raw[y*stride] == 0 for y in range(height))
    return width, height, [tuple(raw[y*stride+1+x*3:y*stride+4+x*3])
                           for y in range(height) for x in range(width)]

from backend.test_pos import client  # disposable seeded database

ROOT = Path(__file__).resolve().parent.parent


def test_packaging_datas_serve_assets_from_frozen_bundle(tmp_path):
    import json
    import os
    import shutil
    import subprocess
    import sys

    # Evaluate the actual spec data declaration, without running PyInstaller.
    namespace = {}
    exec((ROOT / 'uniter.spec').read_text().split('hiddenimports =')[0], namespace)
    datas = namespace['datas']
    assert any(destination == 'backend/static' for source, destination in datas)
    for source, destination in datas:
        source = Path(source)
        assert source.is_dir()
        shutil.copytree(source, tmp_path / destination)
    script = """
import sys
sys._MEIPASS = sys.argv[1]
from backend.main import app, _static_dir
from fastapi.testclient import TestClient
assert str(_static_dir).startswith(sys.argv[1])
client = TestClient(app)  # no lifespan or database access needed for assets
manifest = client.get('/manifest.webmanifest')
assert manifest.status_code == 200
assert manifest.headers['content-type'] == 'application/manifest+json'
for icon in manifest.json()['icons']:
    response = client.get(icon['src'])
    assert response.status_code == 200
    assert response.content[:8] == b'\\x89PNG\\r\\n\\x1a\\n'
assert client.get('/static/install.js').status_code == 200
assert client.get('/static/icons/apple-touch-icon.png').status_code == 200
"""
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'UNITER_DB': str(tmp_path / 'unused.db')}
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=tmp_path,
                            env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'unused.db').exists()



def test_manifest_and_local_icons_are_served(client):
    response = client.get('/manifest.webmanifest')
    assert response.status_code == 200
    assert response.headers['content-type'].split(';')[0] == 'application/manifest+json'
    manifest = response.json()
    assert manifest['name'] == manifest['short_name'] == 'Uniter'
    assert manifest['id'] == '/'
    assert manifest['start_url'] == '/pos'
    assert manifest['scope'] == '/'
    assert manifest['display'] == 'standalone'
    assert manifest['theme_color'] == '#1e3a8a'
    assert manifest['background_color'] == '#f8fafc'
    assert {i['sizes'] for i in manifest['icons']} >= {'192x192', '512x512'}
    assert any(i.get('purpose') == 'maskable' for i in manifest['icons'])
    for icon in manifest['icons']:
        assert icon['src'].startswith('/static/icons/')
        assert icon['type'] == 'image/png'
        asset = client.get(icon['src'])
        assert asset.status_code == 200
        assert asset.headers['content-type'] == 'image/png'
        width, height, pixels = png_pixels(asset.content)
        assert f'{width}x{height}' == icon['sizes']
        assert (255, 255, 255) in pixels  # visible monogram, not a blank square
        # All branding is inside the maskable central safe circle (radius 40%).
        if icon.get('purpose') == 'maskable':
            for i, pixel in enumerate(pixels):
                if pixel != (30, 58, 138):
                    y, x = divmod(i, width)
                    assert (x-width/2)**2 + (y-height/2)**2 <= (width*.4)**2
    assert client.get('/static/../main.py').status_code == 404


def test_shared_metadata_and_quiet_bilingual_help(client):
    from html.parser import HTMLParser

    class Markup(HTMLParser):
        def __init__(self, html):
            super().__init__()
            self.tags = []
            self.feed(html)

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

    for lang in ('en', 'ar'):
        for route in ('/', '/pos', '/transactions', '/settings'):
            response = client.get(route, params={'lang': lang})
            assert response.status_code == 200
            tags = Markup(response.text).tags
            assert [a['href'] for t, a in tags if t == 'link' and a.get('rel') == 'manifest'] == ['/manifest.webmanifest']
            assert [a['content'] for t, a in tags if t == 'meta' and a.get('name') == 'theme-color'] == ['#1e3a8a']
            apple = [a['href'] for t, a in tags if t == 'link' and a.get('rel') == 'apple-touch-icon']
            assert apple == ['/static/icons/apple-touch-icon.png']
            asset = client.get(apple[0])
            assert asset.status_code == 200
            assert png_pixels(asset.content)[:2] == (180, 180)
            assert any(t == 'script' and a.get('src') == '/static/install.js' and 'defer' in a for t, a in tags)
            if route == '/settings':
                assert f'<html lang="{lang}" dir="{"rtl" if lang == "ar" else "ltr"}">' in response.text
                button = next(a for t, a in tags if a.get('id') == 'install-button')
                assert button['type'] == 'button' and 'hidden' in button
                assert any(t == 'details' and a.get('id') == 'install-help' for t, a in tags)
                for copy in (('Install Uniter', 'Add to Home Screen', 'HTTPS', 'online') if lang == 'en'
                             else ('تثبيت Uniter', 'إضافة إلى الشاشة الرئيسية', 'HTTPS', 'اتصال')):
                    assert copy in response.text
            else:
                assert 'id="install-button"' not in response.text
    assert client.get('/static/install.js').status_code == 200


def test_actual_install_script_against_rendered_dom(client):
    import json
    import shutil
    import subprocess
    from html.parser import HTMLParser
    import pytest

    if not shutil.which('node'):
        pytest.skip('Node.js required for install DOM harness')

    class Elements(HTMLParser):
        def __init__(self, html):
            super().__init__()
            self.elements = {}
            self.feed(html)

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get('id') in ('install-button', 'install-status'):
                self.elements[attrs['id']] = {
                    'hidden': 'hidden' in attrs, 'textContent': '',
                    'dataset': {k[5:]: v for k, v in attrs.items() if k.startswith('data-')},
                }

    for lang in ('en', 'ar'):
        elements = Elements(client.get('/settings', params={'lang': lang}).text).elements
        assert set(elements) == {'install-button', 'install-status'}
        result = subprocess.run(['node', str(ROOT / 'backend/install_dom_harness.cjs')],
                                input=json.dumps({'elements': elements, 'script': client.get('/static/install.js').text}),
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert 'install harness passed' in result.stdout
