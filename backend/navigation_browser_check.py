"""Optional real-browser regression: run against a disposable loopback server.

Usage: python navigation_browser_check.py http://127.0.0.1:8765 /tmp/nav-evidence
Requires development-only Playwright; never targets a production database.
Set CHROMIUM_EXECUTABLE to use an existing Chromium installation.
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright


def main():
    base, output = sys.argv[1:3]
    assert urlparse(base).hostname in ('127.0.0.1', 'localhost', '::1')
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    routes = ['/pos', '/topup', '/transactions', '/customers', '/settlement', '/stats', '/settings']
    evidence = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_EXECUTABLE'), headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda msg: errors.append({'text': msg.text, 'url': msg.location.get('url')}) if msg.type == 'error' else None)
        for width in [320, 360, 390, 1280]:
            for lang in ['en', 'ar']:
                page.set_viewport_size({'width': width, 'height': 850})
                for route in routes:
                    response = page.goto(f'{base}{route}?lang={lang}')
                    assert response.status == 200
                    page.wait_for_timeout(350)
                    assert page.evaluate('document.documentElement.scrollWidth === innerWidth'), (width, lang, route)
                    links = page.locator('#app-drawer .nav-pages a')
                    assert links.evaluate_all('(els) => els.map(e => e.getAttribute("href"))') == routes
                    assert page.locator('#app-drawer [aria-current="page"]').get_attribute('href') == route
                    evidence.append({'width': width, 'lang': lang, 'route': route, 'overflow': False})
                page.goto(f'{base}/pos?lang={lang}')
                page.wait_for_timeout(500)
                page.screenshot(path=str(out / f'final-{width}-{lang}.png'))
                if width < 1024:
                    toggle = page.locator('#nav-toggle')
                    toggle.focus()
                    page.keyboard.press('Enter')
                    assert toggle.get_attribute('aria-expanded') == 'true'
                    assert page.locator('#app-drawer').get_attribute('aria-modal') == 'true'
                    assert page.locator('#app-drawer-close').evaluate('(e) => e === document.activeElement')
                    assert page.locator('#app-main').evaluate('(e) => e.closest(".shell-content").inert')
                    page.keyboard.press('Shift+Tab')
                    assert page.locator('#app-drawer a[lang="ar"]').evaluate('(e) => e === document.activeElement')
                    page.keyboard.press('Tab')
                    assert page.locator('#app-drawer-close').evaluate('(e) => e === document.activeElement')
                    page.wait_for_timeout(250)  # capture the completed slide, not an animation frame
                    page.screenshot(path=str(out / f'final-drawer-{width}-{lang}.png'))
                    page.keyboard.press('Escape')
                    assert toggle.evaluate('(e) => e === document.activeElement')
                    assert toggle.get_attribute('aria-expanded') == 'false'
                    toggle.click()
                    page.locator('#app-drawer-close').click()
                    toggle.click()
                    page.mouse.click(2 if lang == 'ar' else width - 2, 200)
                    assert toggle.get_attribute('aria-expanded') == 'false'
                    toggle.click()
                    page.set_viewport_size({'width': 1280, 'height': 850})
                    page.wait_for_function('!document.body.classList.contains("navigation-open")')
                    assert not page.locator('body').evaluate('(e) => e.classList.contains("navigation-open")')
                    assert not page.locator('#app-header').evaluate('(e) => e.inert')
                    assert page.locator('#app-drawer').get_attribute('aria-modal') is None
                    page.set_viewport_size({'width': width, 'height': 850})
                    page.wait_for_function('document.activeElement.id === "nav-toggle"')
                    assert toggle.evaluate('(e) => e === document.activeElement')
                    toggle.click()
                # Exercise real search API, result keyboard selection, and destination.
                search = page.locator('#drawer-global-search' if width < 1024 else '#global-search')
                search.fill('MTN')
                page.locator('#global-search-results [role="option"]').first.wait_for()
                search.press('ArrowDown')
                search.press('Enter')
                page.wait_for_url('**/operators/mtn')
                page.goto(f'{base}/settings?lang={lang}')
                page.wait_for_timeout(300)
                page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
                assert page.locator('#app-header').bounding_box()['y'] == 0
                if width >= 1024:
                    assert page.locator('#app-drawer').bounding_box()['y'] == 64
                page.screenshot(path=str(out / f'final-scrolled-{width}-{lang}.png'))
                page.locator('#app-main').evaluate('(e) => { e.focus(); e.scrollIntoView(); }')
                assert page.locator('#app-main').bounding_box()['y'] >= 64
        # Narrow/short drawer must scroll to language links, not clip them.
        page.set_viewport_size({'width': 320, 'height': 480})
        page.goto(base + '/pos')
        page.locator('#nav-toggle').click()
        page.locator('#app-drawer a[lang="ar"]').focus()
        box = page.locator('#app-drawer a[lang="ar"]').bounding_box()
        assert 0 <= box['y'] and box['y'] + box['height'] <= 480
        page.locator('#app-drawer a[lang="ar"]').click()
        page.wait_for_url('**/pos?lang=ar')
        assert page.locator('html').get_attribute('dir') == 'rtl'
        for path in ['/', '/manifest.webmanifest', '/static/install.js']:
            assert page.request.get(base + path).status == 200
        manifest = page.request.get(base + '/manifest.webmanifest').json()
        for icon in manifest['icons']:
            assert page.request.get(base + icon['src']).status == 200
        assert page.request.get(base + '/static/icons/apple-touch-icon.png').status == 200
        for width in [320, 1280]:
            context = browser.new_context(java_script_enabled=False, viewport={'width': width, 'height': 850})
            plain = context.new_page()
            plain.goto(base + '/pos')
            for link in plain.locator('#app-drawer .nav-pages a').all():
                assert link.is_visible()
            plain.locator('#app-drawer a[href="/topup"]').click()
            assert urlparse(plain.url).path == '/topup'
            context.close()
        # The existing app has no /favicon.ico; retain that known baseline warning.
        new_errors = [e for e in errors if not isinstance(e, dict) or e['url'] != base + '/favicon.ico']
        assert not new_errors, new_errors
        (out / 'browser-results.json').write_text(json.dumps({'checks': evidence, 'console_errors': errors}, indent=2))
        print(f'{len(evidence)} page/viewport/language layouts passed; keyboard, search, resize, scrolling, assets and no-JS checks passed; no new console errors (baseline favicon 404 recorded).')
        browser.close()


if __name__ == '__main__':
    main()
