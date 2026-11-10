# Uniter browser installation (Slice 2.4)

- Manifest: `/manifest.webmanifest` (`application/manifest+json`), stable ID `/`, launch `/pos`, scope `/`, standalone display.
- Local PNG icons: `/static/icons/uniter-192.png`, `uniter-512.png`, `uniter-maskable-512.png`; Apple touch icon: `apple-touch-icon.png` (180px). Blue/white U follows the desktop tray identity. The opaque maskable design keeps the monogram within the central 80%-diameter safe circle.
- Quiet, native `<details>` help lives at the bottom of Settings, in English and Arabic. It remains readable/keyboard-operable with JavaScript disabled. There is no POS banner, automatic prompt, or inert visible install button.
- `/static/install.js` enables the Install button only when the browser supplies a deferred `beforeinstallprompt` event in a secure context. Explicit click consumes it once. Acceptance, dismissal, prompt rejection, installed events and standalone/iOS standalone states are handled. Other pages simply load the enhancement without changing their UI.

## Browser and origin contract

MDN's current [Making PWAs installable](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable) documentation explicitly says a service worker is **not required** for installation; Chromium requires the manifest identity, 192/512 icons, launch URL and display mode. Installation requires HTTPS or trusted localhost/loopback contexts. Browser promotion remains subject to browser policy, engagement, prior dismissal and platform support; a manifest does not guarantee an event.

**No service worker is installed or registered.** No app cache, fetch interception, offline pages, background sync, financial replay or private/API caching is introduced. App pages remain network-backed. The pre-existing UI includes Tailwind/font CDN references; this slice adds no external dependencies and does not promise offline UI styling.

For iPhone/iPad the server-rendered help directs users to Safari → Share → Add to Home Screen (Open as Web App when shown). On Android/laptops it describes browser-menu installation only if available, otherwise ordinary web use. A plain HTTP LAN IP is not trusted localhost on a phone; use a securely deployed HTTPS origin. The existing desktop URL remains `http://127.0.0.1:56123`; the desktop launcher must remain running for an installed shortcut to reach the server. HTTPS termination, certificates, port and launcher behavior are unchanged.

## Serving, CSP and packaging

The existing app defines no CSP/security-header middleware. This slice does not add or relax any security policy. Install behavior uses only a same-origin external deferred script; deployments providing a CSP should permit `manifest-src 'self'`, `script-src 'self'` and local image assets. No worker policy is needed.

The manifest and static assets resolve alongside templates, including `sys._MEIPASS/backend` in the existing frozen runtime. `uniter.spec` includes the entire `backend/static` directory; `scripts/build_exe.py` and the Windows installer workflow already build that spec and package its result. No runtime image library is required. The packaging test evaluates the actual spec data declarations, copies them to a temporary extracted-bundle layout, then verifies manifest/icons/JS HTTP responses from a fresh process using `_MEIPASS` and an unrelated working directory, without creating a database. This verifies the asset contract, not a built Windows executable.

## Reproducible verification

Use Python 3.12, a non-user `UNITER_DB` path and the existing isolated test fixtures:

```
UNITER_DB=/tmp/uniter-install-tests-unused.db /path/to/python3.12 -m pytest backend/test_installability.py -q
UNITER_DB=/tmp/uniter-install-tests-unused.db /path/to/python3.12 -m pytest backend/ --tb=short -q
/path/to/python3.12 -m py_compile backend/*.py
node --check backend/static/install.js
node --check backend/install_dom_harness.cjs
git diff --check
```

The Python tests execute the **served** install script in Node against IDs/data attributes parsed from actual EN/AR Settings HTML. The deterministic harness proves event/click state transitions; it is not evidence of a browser-generated install prompt or physical-device installation. Real Chromium/Safari/device installation and Windows executable testing remain independent review checks. No review server is started by these tests.
