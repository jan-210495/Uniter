"""Slice 2.3: preparing a code, and committing the sale from Copy / dialer.

The rendered /pos script runs in a DOM double (``pos_result_harness.cjs``) so the
real copy paths, the optional "tel:" handoff and the commit request are
exercised without a browser. Clipboard behaviour is simulated per run; every
network call is captured instead of sent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ENV = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
HARNESS = Path(__file__).with_name("pos_result_harness.cjs")

CODE = "*150*65477*0944123456*50#"
COMMIT_KEY = "previewkey0000000000000000001"
SECOND_COMMIT_KEY = "previewkey0000000000000000002"
GENERATE = [{"values": {"phone": "0944123456"}, "id": "charge-form", "event": "submit"}]
COPY = [{"id": "copy-ussd", "event": "click"}]


def render(lang: str = "en") -> str:
    return ENV.get_template("pos.html").render(lang=lang)


def page_script(html: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    return next(script for script in scripts if "charge-form" in script)


def run(steps, lang: str = "en", **options):
    """Execute the rendered /pos script and return its end state."""
    if not shutil.which("node"):
        pytest.skip("Node.js required to execute the rendered /pos handlers")
    html = render(lang)
    payload = {"html": html, "script": page_script(html), "steps": steps}
    payload.update(options)
    result = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def commits(result):
    return result["commits"]


# --- Preparing a code records nothing -------------------------------------


def test_generate_only_previews_and_persists_nothing() -> None:
    result = run(GENERATE)
    assert [request["url"] for request in result["previews"]] == ["/api/transactions/preview"]
    preview = result["previews"][0]["body"]
    assert preview == {
        "customer_phone": "0944123456",
        "operator_code": "mtn",
        "service_code": "prepaid",
        "amount_cents": 5000,
    }
    # No commit, no status PATCH: generating is not a sale.
    assert commits(result) == []
    assert [request for request in result["requests"] if request["method"] == "PATCH"] == []
    assert result["elements"]["ussd-output"]["value"] == CODE
    assert result["elements"]["result"]["hidden"] is False
    assert result["elements"]["result-badge"]["text"] == "Not recorded yet"
    assert "text-blue-900" in result["elements"]["result-badge"]["classes"]
    assert result["elements"]["commit-status"]["text"] == ""


def test_preview_rejects_insufficient_balance_without_showing_a_code() -> None:
    result = run(GENERATE, preview="insufficient")
    assert result["previews"] and commits(result) == []
    assert result["elements"]["result"]["hidden"] is True
    assert result["elements"]["register-status"]["text"] == "Insufficient operator balance."
    assert result["elements"]["ussd-output"]["value"] == ""


def test_preview_without_commit_key_is_treated_as_a_failure() -> None:
    result = run(GENERATE + COPY, preview="missingKey")
    assert result["elements"]["result"]["hidden"] is True
    assert result["elements"]["register-status"]["text"] == "Could not prepare the code. Try again."
    assert commits(result) == []


# --- Copy commits the sale exactly once -----------------------------------


def test_copy_commits_the_sale_once_with_the_preview_key() -> None:
    result = run(GENERATE + COPY)
    assert result["clipboardWrites"] == [CODE]
    assert result["elements"]["copy-status"]["text"] == "Code copied."
    assert len(commits(result)) == 1
    commit = commits(result)[0]
    assert commit["body"] == {
        "customer_phone": "0944123456",
        "operator_code": "mtn",
        "service_code": "prepaid",
        "amount_cents": 5000,
        "commit_key": COMMIT_KEY,
    }
    assert result["elements"]["result-badge"]["text"] == "Recorded"
    assert "text-green-800" in result["elements"]["result-badge"]["classes"]
    assert result["elements"]["commit-status"]["text"] == (
        "Sale recorded and charged to the operator balance. TXN-harness"
    )
    # Accounting is not re-applied through the status endpoint.
    assert [request for request in result["requests"] if request["method"] == "PATCH"] == []


def test_copy_through_the_legacy_fallback_still_commits_once() -> None:
    result = run(GENERATE + COPY, clipboard="none", execCommand="ok")
    assert result["execCommandCalls"] == 1
    assert result["helperCount"] == 1 and result["helperFields"] == []
    assert result["bodyChildren"] == 0
    assert len(commits(result)) == 1
    assert result["elements"]["result-badge"]["text"] == "Recorded"


def test_copy_failure_records_nothing_and_offers_manual_copy() -> None:
    result = run(GENERATE + COPY, clipboard="reject", execCommand="fail")
    assert commits(result) == []
    assert result["elements"]["copy-status"]["text"] == (
        "Could not copy automatically. Select the code and copy it manually."
    )
    assert result["elements"]["commit-status"]["text"] == (
        "Nothing was recorded: copy the code by hand and send it to the customer."
    )
    assert result["elements"]["result-badge"]["text"] == "Not recorded yet"
    assert result["elements"]["ussd-output"]["selected"] is True
    assert result["lastFocused"] == "ussd-output"
    assert result["bodyChildren"] == 0


def test_copy_failure_without_any_fallback_records_nothing() -> None:
    result = run(GENERATE + COPY, clipboard="none", execCommand="missing")
    assert commits(result) == []
    assert result["elements"]["copy-status"]["text"] == (
        "Could not copy automatically. Select the code and copy it manually."
    )
    assert result["elements"]["result-badge"]["text"] == "Not recorded yet"


def test_double_tap_and_repeat_taps_send_one_commit() -> None:
    double = run(GENERATE + [{"clickTwice": "copy-ussd"}])
    assert len(commits(double)) == 1
    repeat = run(GENERATE + COPY + COPY + COPY)
    assert len(commits(repeat)) == 1


def test_a_new_preview_gets_a_new_key() -> None:
    result = run(
        GENERATE
        + COPY
        + [{"values": {"phone": "0933123456"}, "id": "charge-form", "event": "submit"}]
        + COPY,
        preview="ok",
    )
    assert len(result["previews"]) == 2
    assert len(commits(result)) == 2
    keys = [commit["body"]["commit_key"] for commit in commits(result)]
    assert keys == [COMMIT_KEY, SECOND_COMMIT_KEY]
    # A new preview is a new sale: it is not deduplicated against the old key.
    assert commits(result)[1]["body"]["customer_phone"] == "0933123456"


def test_commit_failure_keeps_the_sale_unrecorded() -> None:
    result = run(GENERATE + COPY, commit="error")
    assert len(commits(result)) == 1
    assert result["elements"]["result-badge"]["text"] == "Not recorded yet"
    assert result["elements"]["commit-status"]["text"] == "Could not commit the sale"
    assert result["elements"]["copy-status"]["text"] == "Code copied."


def test_commit_failure_for_insufficient_balance_is_translated() -> None:
    result = run(GENERATE + COPY, commit="insufficient")
    assert result["elements"]["commit-status"]["text"] == "Insufficient operator balance."
    assert result["elements"]["result-badge"]["text"] == "Not recorded yet"


# --- Dialer handoff -------------------------------------------------------


def test_dialer_is_not_offered_before_a_code_exists() -> None:
    result = run([])
    assert result["dial"]["hidden"] is True
    assert result["dial"]["href"] is None
    assert result["navigation"]["href"] == ""


def test_dialer_href_encodes_the_trailing_hash_and_never_dials_on_render() -> None:
    result = run(GENERATE)
    assert result["dial"]["hidden"] is False
    assert result["dial"]["href"] == "tel:*150*65477*0944123456*50%23"
    assert result["navigation"]["href"] == ""


def test_dialer_is_hidden_on_desktop() -> None:
    result = run(GENERATE, mobile=False)
    assert result["dial"]["hidden"] is True
    assert result["dial"]["href"] is None


def test_dialer_tap_commits_once_then_opens_the_dialer() -> None:
    result = run(GENERATE + [{"id": "dial-ussd", "event": "click"}])
    assert len(commits(result)) == 1
    assert commits(result)[0]["body"]["commit_key"] == COMMIT_KEY
    assert result["elements"]["result-badge"]["text"] == "Recorded"
    assert result["navigation"]["href"] == "tel:*150*65477*0944123456*50%23"


def test_dialer_tap_without_a_preview_does_not_commit_or_navigate() -> None:
    result = run([{"id": "dial-ussd", "event": "click"}])
    assert commits(result) == []
    assert result["navigation"]["href"] == ""


# --- Markup, language and accessibility -----------------------------------


def test_markup_has_no_status_panel_and_a_commit_live_region() -> None:
    html = render()
    assert 'id="commit-status" role="status" aria-live="polite"' in html
    assert 'id="result-badge"' in html
    assert "Not recorded yet" in html
    assert "Copy &amp; record" in html or "Copy & record" in html
    assert "Open dialer &amp; record" in html or "Open dialer & record" in html
    # The Slice 2 pending/completed panel is gone.
    assert 'id="status-actions"' not in html
    assert 'id="status-confirm"' not in html
    assert "data-status-action" not in html
    assert 'id="copy-status" role="status" aria-live="polite"' in html
    assert '<button id="copy-ussd" type="button"' in html
    assert 'aria-describedby="dial-hint"' in html


def test_arabic_page_is_translated_and_rtl() -> None:
    html = render("ar")
    assert 'dir="rtl"' in html
    assert "لم تُسجَّل بعد" in html
    assert "نسخ وتسجيل" in html
    assert "Not recorded yet" not in html
    result = run(GENERATE + COPY, lang="ar")
    assert result["elements"]["result-badge"]["text"] == "مسجّلة"
    assert result["elements"]["commit-status"]["text"] == "تم تسجيل البيع وخصمه من رصيد المشغّل. TXN-harness"


def test_arabic_copy_failure_feedback() -> None:
    result = run(GENERATE + COPY, lang="ar", clipboard="reject", execCommand="fail")
    assert result["elements"]["commit-status"]["text"] == (
        "لم يُسجَّل أي شيء: انسخ الرمز يدوياً ثم أرسله للعميل."
    )
    assert result["elements"]["copy-status"]["text"] == (
        "تعذر النسخ تلقائياً. حدد الرمز وانسخه يدوياً."
    )
