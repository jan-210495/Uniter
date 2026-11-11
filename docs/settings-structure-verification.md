# Slice 2.6: Settings documents existing POS formats

Approved source: revised `.planning/phase7/slice-2.6-settings-code-structure.md`
(user-local brief), and unchanged `backend/seed.py:generate_ussd` at base
528a5a81164b5708adc1dc6e3169be4d113ece79. This documents application behavior,
not carrier validation. No generation, API, schema, stored metadata, financial,
navigation, or installability changes.

Settings now shows all four supported operator/service formulas with live domain
context already supplied by settings_page. EN/AR placeholder explanations document
amount_cents / 100 and duplicated Syriatel phone. Legacy prefix cells are removed;
operator/service/status information is preserved. Mobile rows stack with labels;
formulas stay LTR and wrap. Successful existing domain saves update displayed
formula domain spans using the server response; validation/feedback/API remain.

Verification (Python 3.12):
- Baseline full suite: 539 passed, 350 warnings (23.99s).
- Focused Settings/structure/USSD tests: 62 passed, 64 warnings (2.77s).
- Final full suite: 551 passed, 362 warnings (23.21s).
- py_compile backend/*.py, rendered inline JavaScript node --check, git diff
  --check passed. Generator/financial test expectations unchanged.
- 12 new cases compare EN/AR rendered patterns to exact approved literals AND
  real generate_ussd output, default/20-digit overridden domains, amounts 0,
  5000 and 12345, phone occurrence counts, legacy stored metadata unchanged,
  shared navigation and install metadata/help preserved.
- Headless Chromium observed Settings EN/AR at 1440x1000, 390x844, 320x740.
  No horizontal page overflow or page JS exceptions. Real domain form updates
  used 20-digit values including leading zeros; immediate formula refresh,
  reload persistence and rejected-edit preservation passed.
- Inspected desktop formula table and actual RTL 320x740 viewport screenshots.
  Element/full-page captures while scrolled include sticky-toolbar artifacts;
  actual viewport capture confirms no text overlap.
- Browser used only /tmp/uniter-settings-structure-evidence/browser.db on isolated
  loopback port 8766, not the user's review server/database.

Evidence: /tmp/uniter-settings-structure-evidence/ (baseline-tests.txt, focused.txt,
final.txt, browser_check.py, verify.py, screenshots). Browser scripts are local
verification tooling, not runtime dependencies. No physical device/Safari/Firefox
or assistive-technology testing claimed. Existing deprecation warnings remain.
