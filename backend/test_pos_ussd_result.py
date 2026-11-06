"""Slice 2, part A/B: copying the generated USSD code and the dialer handoff.

The rendered /pos script runs in a DOM double (``pos_result_harness.cjs``) so
the real copy paths, the optional "tel:" handoff and the operator confirmation
flow are exercised without a browser. Clipboard behaviour is simulated per
run; every network call is captured instead of sent.
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
GENERATE = [{"values": {"phone": "0944123456"}, "id": "charge-form", "event": "submit"}]


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


def patch_requests(result):
    return [request for request in result["requests"] if request["method"] == "PATCH"]


def copy_once(**options):
    return run(GENERATE + [{"id": "copy-ussd", "event": "click"}], **options)


# --- Part A: copy paths ----------------------------------------------------


def test_copy_uses_async_clipboard_api_in_a_secure_context() -> None:
    result = copy_once(clipboard="modern", secureContext=True)
    assert result["clipboardWrites"] == [CODE]
    assert result["execCommandCalls"] == 0
    assert result["helperCount"] == 0
    assert result["elements"]["copy-status"]["text"] == "Code copied."


def test_copy_falls_back_to_a_hidden_field_when_the_api_is_rejected() -> None:
    result = copy_once(clipboard="reject", secureContext=True, execCommand="ok")
    # The API was attempted, rejected, and the legacy path reported success.
    assert result["clipboardWrites"] == [CODE]
    assert result["execCommandCalls"] == 1
    assert result["execCommandArgs"] == "copy"
    assert result["elements"]["copy-status"]["text"] == "Code copied."
    # The temporary field is removed again; nothing is left behind in the DOM.
    assert result["helperCount"] == 1
    assert result["helperFields"] == []
    assert result["bodyChildren"] == 0
    assert result["lastFocused"] == "copy-ussd"


def test_copy_skips_the_api_outside_a_secure_context() -> None:
    # Plain HTTP shop addresses never expose a usable Clipboard API.
    result = copy_once(clipboard="modern", secureContext=False, execCommand="ok")
    assert result["clipboardWrites"] == []
    assert result["execCommandCalls"] == 1
    assert result["elements"]["copy-status"]["text"] == "Code copied."


def test_copy_reports_a_truthful_failure_and_selects_the_code() -> None:
    result = copy_once(clipboard="none", secureContext=True, execCommand="fail")
    assert result["elements"]["copy-status"]["text"] == (
        "Could not copy automatically. Select the code and copy it manually."
    )
    assert result["bodyChildren"] == 0
    # The code is selected so the operator can copy it by hand.
    assert result["elements"]["ussd-output"]["focused"] is True
    assert result["elements"]["ussd-output"]["selected"] is True


def test_copy_failure_when_exec_command_throws() -> None:
    result = copy_once(clipboard="none", execCommand="throw")
    assert result["elements"]["copy-status"]["text"].startswith("Could not copy automatically")
    assert result["bodyChildren"] == 0


def test_copy_failure_without_any_clipboard_support() -> None:
    result = copy_once(clipboard="none", execCommand="missing")
    assert result["elements"]["copy-status"]["text"].startswith("Could not copy automatically")
    assert result["execCommandCalls"] == 0


def test_copy_success_message_is_arabic() -> None:
    result = copy_once(lang="ar", clipboard="reject", execCommand="ok")
    assert result["elements"]["copy-status"]["text"] == "تم نسخ الرمز."


def test_copy_failure_message_is_arabic() -> None:
    result = copy_once(lang="ar", clipboard="none", execCommand="fail")
    assert result["elements"]["copy-status"]["text"] == (
        "تعذر النسخ تلقائياً. حدد الرمز وانسخه يدوياً."
    )


def test_copy_never_claims_success_without_a_real_copy() -> None:
    for options in ({"clipboard": "none", "execCommand": "fail"},
                    {"clipboard": "reject", "execCommand": "throw"}):
        result = copy_once(**options)
        assert result["elements"]["copy-status"]["text"] != "Code copied."


# --- Part B: dialer handoff ------------------------------------------------


def test_dialer_handoff_is_never_offered_before_a_code_exists() -> None:
    result = run([], mobile=True, secureContext=True)
    assert result["dial"]["hidden"] is True
    assert result["dial"]["href"] is None


def test_dialer_handoff_is_mobile_only_and_encodes_the_hash() -> None:
    mobile = run(GENERATE, mobile=True, secureContext=True)
    assert mobile["dial"]["hidden"] is False
    # Only the trailing "#" is percent-encoded (a literal "#" would become a
    # URL fragment); "*" must stay literal for dialers.
    assert mobile["dial"]["href"] == "tel:*150*65477*0944123456*50%23"

    desktop = run(GENERATE, mobile=False, secureContext=True)
    assert desktop["dial"]["hidden"] is True
    assert desktop["dial"]["href"] is None


def test_dialer_handoff_requires_a_user_tap_and_places_no_request() -> None:
    # Rendering the link is the only effect: no call, no USSD session, no API.
    result = run(GENERATE, mobile=True, secureContext=True)
    assert patch_requests(result) == []
    assert [request for request in result["requests"] if request["method"] == "POST"] == [
        {"url": "/api/transactions", "method": "POST",
         "body": {"customer_phone": "0944123456", "operator_code": "mtn",
                  "service_code": "prepaid", "amount_cents": 5000}}
    ]
    html = render()
    assert 'id="dial-ussd"' in html
    assert "Device and browser support varies" in html
    assert "so check the code and confirm the call yourself" in html


def test_dialer_handoff_caveat_is_arabic() -> None:
    html = render("ar")
    assert "يفتح هذا الزر لوحة الاتصال" in html
    assert 'id="dial-ussd"' in html
    assert "فتح لوحة الاتصال" in html


# --- Part C: the confirmation flow is explicit -----------------------------


def test_generating_a_code_leaves_the_transaction_pending() -> None:
    result = run(GENERATE)
    assert patch_requests(result) == []
    assert result["elements"]["result"]["hidden"] is False
    assert result["badge"]["text"] == "Pending"
    assert result["elements"]["status-actions"]["hidden"] is False
    assert result["elements"]["status-confirm"]["hidden"] is True
    assert result["elements"]["status-feedback"]["text"] == ""


def test_copying_and_opening_the_dialer_leave_the_transaction_pending() -> None:
    result = run(
        GENERATE + [{"id": "copy-ussd", "event": "click"}],
        clipboard="modern",
        mobile=True,
        secureContext=True,
    )
    assert patch_requests(result) == []
    assert result["badge"]["text"] == "Pending"
    assert result["elements"]["status-actions"]["hidden"] is False


def test_choosing_a_result_asks_for_confirmation_first() -> None:
    result = run(GENERATE + [{"id": "status-completed", "event": "click"}])
    assert patch_requests(result) == []
    assert result["elements"]["status-confirm"]["hidden"] is False
    assert result["elements"]["status-actions"]["hidden"] is True
    assert result["elements"]["status-confirm-text"]["text"] == (
        f"Confirm that this charge succeeded? TXN-harness"
    )
    assert result["badge"]["text"] == "Pending"


def test_cancelling_the_confirmation_saves_nothing() -> None:
    result = run(
        GENERATE
        + [
            {"id": "status-failed", "event": "click"},
            {"id": "status-confirm-no", "event": "click"},
        ]
    )
    assert patch_requests(result) == []
    assert result["elements"]["status-confirm"]["hidden"] is True
    assert result["elements"]["status-actions"]["hidden"] is False
    assert result["elements"]["status-feedback"]["text"] == "No result was saved."
    assert result["badge"]["text"] == "Pending"


def test_confirming_success_saves_one_completed_status() -> None:
    result = run(
        GENERATE
        + [
            {"id": "status-completed", "event": "click"},
            {"id": "status-confirm-yes", "event": "click"},
        ]
    )
    assert patch_requests(result) == [
        {"url": "/api/transactions/42/status", "method": "PATCH", "body": {"status": "completed"}}
    ]
    assert result["badge"]["text"] == "Completed"
    assert "bg-green-100" in result["badge"]["classes"]
    assert result["elements"]["status-feedback"]["text"] == "Transaction recorded as completed."
    # A final status cannot be changed again from the UI.
    assert result["elements"]["status-actions"]["hidden"] is True
    assert result["elements"]["status-confirm"]["hidden"] is True


def test_confirming_failure_saves_one_failed_status() -> None:
    result = run(
        GENERATE
        + [
            {"id": "status-failed", "event": "click"},
            {"id": "status-confirm-yes", "event": "click"},
        ]
    )
    assert patch_requests(result) == [
        {"url": "/api/transactions/42/status", "method": "PATCH", "body": {"status": "failed"}}
    ]
    assert result["badge"]["text"] == "Failed"
    assert "bg-red-100" in result["badge"]["classes"]
    assert result["elements"]["status-feedback"]["text"] == "Transaction recorded as failed."
    assert result["elements"]["status-actions"]["hidden"] is True


def test_a_rejected_status_update_is_reported_and_stays_retryable() -> None:
    result = run(
        GENERATE
        + [
            {"id": "status-completed", "event": "click"},
            {"id": "status-confirm-yes", "event": "click"},
        ],
        patch={"status": 409},
    )
    assert result["elements"]["status-feedback"]["text"] == (
        "This transaction already has a final status."
    )
    assert result["elements"]["status-actions"]["hidden"] is False
    assert result["elements"]["status-confirm-yes"]["disabled"] is False
    assert result["badge"]["text"] == "Pending"


def test_missing_transaction_is_reported() -> None:
    result = run(
        GENERATE
        + [
            {"id": "status-completed", "event": "click"},
            {"id": "status-confirm-yes", "event": "click"},
        ],
        patch={"status": 404},
    )
    assert result["elements"]["status-feedback"]["text"] == "Transaction not found."


def test_status_flow_labels_are_arabic() -> None:
    result = run(
        GENERATE + [{"id": "status-failed", "event": "click"}],
        lang="ar",
    )
    assert result["elements"]["status-confirm-text"]["text"].startswith("تأكيد فشل هذه المعاملة؟")
    assert result["badge"]["text"] == "قيد الانتظار"

    saved = run(
        GENERATE
        + [
            {"id": "status-failed", "event": "click"},
            {"id": "status-confirm-yes", "event": "click"},
        ],
        lang="ar",
    )
    assert saved["badge"]["text"] == "فاشلة"
    assert saved["elements"]["status-feedback"]["text"] == "تم تسجيل المعاملة كفاشلة."


# --- Markup contracts ------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_result_panel_markup_is_accessible(lang: str) -> None:
    html = render(lang)
    for element_id in (
        "result-status-badge",
        "dial-actions",
        "dial-ussd",
        "dial-hint",
        "status-actions",
        "status-completed",
        "status-failed",
        "status-confirm",
        "status-confirm-text",
        "status-confirm-yes",
        "status-confirm-no",
        "status-feedback",
    ):
        assert f'id="{element_id}"' in html
    # The feedback regions are polite live regions.
    feedback = re.search(r'<p id="status-feedback"[^>]*>', html)
    assert feedback is not None
    assert 'role="status"' in feedback.group(0)
    assert 'aria-live="polite"' in feedback.group(0)
    # The dialer hint is attached to the link, and the actions are real buttons.
    assert 'aria-describedby="dial-hint"' in html
    assert html.count('data-status-action="completed"') == 1
    assert html.count('data-status-action="failed"') == 1
    # The dial action starts hidden so nothing is offered before a code exists.
    assert re.search(r'<div id="dial-actions" hidden', html)


def test_script_calls_the_status_endpoint_exactly_once_per_confirmation() -> None:
    script = page_script(render())
    assert "'/api/transactions/' + transaction.id + '/status'" in script
    assert "method: 'PATCH'" in script
    assert script.count("fetch('/api/transactions/' + transaction.id + '/status'") == 1
    # Generation only ever records the server's pending transaction; the final
    # state comes from the confirmed PATCH above and nothing else.
    assert "activeTransaction = { id: data.id, reference: data.reference, status: 'pending' }" in script


# --- Slice 1 regression ----------------------------------------------------


def test_voucher_selection_still_drives_the_payload_before_confirming() -> None:
    html = render()
    assert html.count('class="voucher-button') == 59
    result = run(
        [
            {"voucher": {"operator": "mtn", "amount": "3000"}},
            {"values": {"phone": "0944123456"}, "id": "charge-form", "event": "submit"},
            {"id": "status-completed", "event": "click"},
            {"id": "status-confirm-yes", "event": "click"},
        ]
    )
    post = next(request for request in result["requests"] if request["method"] == "POST")
    assert post["body"]["amount_cents"] == 300_000
    pressed = [voucher for voucher in result["vouchers"] if voucher["pressed"] == "true"]
    assert [(voucher["operator"], voucher["amount"]) for voucher in pressed] == [("mtn", "3000")]
    assert result["badge"]["text"] == "Completed"
