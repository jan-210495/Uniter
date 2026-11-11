# Slice 2.5 navigation verification

Base: `9a325f7c910166114b8f2556bb3b2c28222d8116`, fetched target matched exactly.
Parents: `9ed23f1a1462ad9ed820ab150fdd3f383f97c39a`, `6ff9ce86cbba4c344b3ef40d737e329e8dc8e4d3`.
Task branch: `task/phase7-slice2.5-navigation`.

## Implementation

One shared ordered navigation list (POS, Top-up, Transactions, Customers,
Settlement, Stats, Settings, then languages). At 1024px and above it is a
208px fixed start-edge sidebar; below that it is a scrollable modal drawer.
The compact toolbar stays sticky. RTL mirrors the start edge. Focus trapping,
inert background, Escape/backdrop/close restoration, responsive cleanup,
reduced motion, focus styles, skip link and no-JS links are included.
POS/Settlement previously lacked nav_page; template-local markers now provide
correct active state without changing routes. Install metadata and Settings
help/control behavior are unchanged. No runtime dependency or business changes.

## Executed verification

- Python 3.12.16 baseline: 520 passed, 331 warnings, 22.72s.
- Final full suite: 539 passed, 350 warnings, 21.94s.
- `python -m py_compile backend/*.py`, `node --check backend/static/navigation.js`,
  and `git diff --check`: passed. Existing JS harnesses ran through pytest.
- Real headless Chromium with Playwright: 56 layouts (all seven pages, EN/AR,
  widths 320, 360, 390, 1280; height 850). documentElement.scrollWidth matched
  viewport width in every case.
- Keyboard open, focus trap both directions, Escape/focus return, close button,
  backdrop, mobile/desktop resize cleanup, real MTN search + keyboard selection
  to operator route, language navigation, scrolled Settings toolbar/sidebar,
  main-content scroll offset, short 320x480 drawer language reachability passed.
- JavaScript-disabled links at 320 and 1280 passed.
- Loopback app on disposable `/tmp/uniter-navigation-review.db`; root, all seven
  pages, manifest, install.js and declared icons/apple icon returned 200
  (root redirects to POS). No real database used.
- Screenshots inspected: desktop EN/AR, narrow mobile EN, narrow RTL drawer,
  scrolled desktop Settings; baseline desktop EN/AR and mobile EN/RTL drawer.
  All viewport screenshots and browser-results.json saved locally in
  `/tmp/uniter-nav-evidence/`. Test logs: `/tmp/uniter-nav-baseline.txt` and
  `/tmp/uniter-nav-final.txt`.

Re-run browser checks with development-only Playwright and an isolated server:
`CHROMIUM_EXECUTABLE=/path/to/chrome python backend/navigation_browser_check.py http://127.0.0.1:8765 /tmp/nav-evidence`

## Limitations and observations

- Chromium automation only, not physical iOS/Android or Safari/Firefox; no
  assistive-technology user testing. Existing Tailwind/font CDN dependencies
  remain; no-JS links work but utility styling still depends on the existing CDN.
- Existing favicon.ico 404 is recorded explicitly; no new console/JS errors.
- Existing narrow POS service select truncates its label; no new page overflow,
  POS field layout/compaction unchanged.
- An intermediate full-suite run failed the unchanged
  `test_search_lists_one_time_sales_without_treating_them_as_customers` phone
  search assertion (extra transaction match). Final full rerun passed without
  modifying it; possible reference substring collision, not proven here.
- Existing deprecation warnings remain. No integration merge/release performed.
