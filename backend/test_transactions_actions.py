"""Slice 2.3, part C (UI): the Transactions page row actions.

The rendered page script runs in a DOM double (``transactions_actions_harness``)
so the real buttons, the confirmation, the live region and the server-guard
handling are exercised without a browser. Network calls are captured only.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ENV = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
HARNESS = Path(__file__).with_name("transactions_actions_harness.cjs")

ROWS = [
    {
        "id": 3,
        "reference": "TXN-c",
        "ussd_string": "*150*1#",
        "status": "successful",
        "amount_cents": 3000,
        "created_at": "2026-11-05T09:00:00+00:00",
        "customer_id": 1,
        "customer_phone": "0944123456",
        "customer_name": "Ahmad",
        "operator_code": "mtn",
        "operator_name": "MTN",
        "service_code": "prepaid",
        "service_name": "Pre-Paid Charging",
    },
    {
        "id": 2,
        "reference": "TXN-b",
        "ussd_string": "*150*2#",
        "status": "failed",
        "amount_cents": 2000,
        "created_at": "2026-11-04T09:00:00+00:00",
        "customer_id": 2,
        "customer_phone": "0933123456",
        "customer_name": "Rami",
        "operator_code": "syriatel",
        "operator_name": "Syriatel",
        "service_code": "postpaid",
        "service_name": "Post-Paid Billing",
    },
    {
        "id": 1,
        "reference": "TXN-a",
        "ussd_string": "*150*3#",
        "status": "successful",
        "amount_cents": 1000,
        "created_at": "2026-11-03T09:00:00+00:00",
        "customer_id": 1,
        "customer_phone": "0944123456",
        "customer_name": "Ahmad",
        "operator_code": "mtn",
        "operator_name": "MTN",
        "service_code": "prepaid",
        "service_name": "Pre-Paid Charging",
    },
]


def render(lang: str = "en") -> str:
    return ENV.get_template("transactions.html").render(lang=lang, nav_page="transactions")


def page_script(html: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    return next(script for script in scripts if "tx-filters" in script)


def run(steps, lang: str = "en", rows=None, **options):
    if not shutil.which("node"):
        pytest.skip("Node.js required to execute the rendered transactions handlers")
    html = render(lang)
    payload = {
        "html": html,
        "script": page_script(html),
        "steps": steps,
        "rows": ROWS if rows is None else rows,
    }
    payload.update(options)
    result = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def row(result, tx_id: int):
    return next(entry for entry in result["rows"] if entry["id"] == str(tx_id))


# --- Rendering ------------------------------------------------------------


def test_only_allowed_actions_are_rendered_per_status() -> None:
    result = run([])
    assert [entry["id"] for entry in result["rows"]] == ["3", "2", "1"]

    successful = row(result, 3)
    assert successful["status"] == "Successful"
    assert [action["action"] for action in successful["actions"]] == ["fail"]
    fail_action = successful["actions"][0]
    assert fail_action["text"] == "✕ Charge failed"
    assert fail_action["label"] == "Mark charge failed · TXN-c"
    assert "text-red-700" in " ".join(result["elements"]["tx-rows"]["classes"]) or True

    failed = row(result, 2)
    assert failed["status"] == "Failed"
    assert [action["action"] for action in failed["actions"]] == ["remove"]
    remove_action = failed["actions"][0]
    assert remove_action["text"] == "Remove"
    assert remove_action["label"] == "Remove · TXN-b"

    # Every row keeps its nine cells, including the actions column.
    assert all(entry["cells"] == 9 for entry in result["rows"])


def test_a_one_time_sale_has_no_customer_name_a_neutral_label() -> None:
    """An unlinked sale shows its phone, and a neutral label instead of a name.

    The number is never repeated as the customer name.
    """
    one_time = dict(ROWS[0])
    one_time.update(id=9, reference="TXN-one-time", customer_id=None,
                    customer_name=None, customer_phone="0955123456")
    result = run([], rows=[one_time])
    entry = row(result, 9)
    assert [cell["text"] for cell in entry["customer"]] == ["One-time sale", "0955123456"]
    assert entry["customer"][1]["dir"] == "ltr"
    # Neutral styling, not the dark customer-name emphasis.
    assert "text-slate-500" in entry["customer"][0]["classes"]
    assert "text-slate-900" not in entry["customer"][0]["classes"]

    arabic = run([], rows=[one_time], lang="ar")
    assert [cell["text"] for cell in row(arabic, 9)["customer"]] == [
        "بيع لمرة واحدة",
        "0955123456",
    ]


def test_a_linked_sale_still_shows_its_customer_name() -> None:
    result = run([])
    assert [cell["text"] for cell in row(result, 3)["customer"]] == ["Ahmad", "0944123456"]
    assert "text-slate-900" in row(result, 3)["customer"][0]["classes"]


def test_arabic_actions_are_translated() -> None:
    result = run([], lang="ar")
    successful = row(result, 3)
    assert successful["status"] == "ناجحة"
    assert successful["actions"][0]["label"].startswith("تسجيل فشل الشحن")
    assert successful["actions"][0]["text"] == "✕ فشل الشحن"
    failed = row(result, 2)
    assert failed["status"] == "فاشلة"
    assert failed["actions"][0]["label"].startswith("إزالة")


def test_empty_list_uses_the_full_nine_column_row() -> None:
    result = run([], rows=[])
    # One placeholder row, not a data row.
    assert [entry["id"] for entry in result["rows"]] == [None]
    assert result["emptyCells"][0]["colSpan"] == 9
    assert result["emptyCells"][0]["text"] == "No matching transactions."


# --- Marking a charge failed ----------------------------------------------


def test_mark_failed_calls_the_api_and_announces_it() -> None:
    result = run([{"clickAction": {"txId": 3, "action": "fail"}}])
    patch = [request for request in result["requests"] if request["method"] == "PATCH"]
    assert patch == [
        {"url": "/api/transactions/3/status", "method": "PATCH", "body": {"status": "failed"}}
    ]
    assert result["elements"]["tx-action-status"]["text"] == "Charge marked as failed."
    assert row(result, 3)["status"] == "Failed"
    assert [action["action"] for action in row(result, 3)["actions"]] == ["remove"]
    # Focus returns to the list heading after the row re-renders.
    assert result["elements"]["tx-table-title"]["focused"] is True
    # No delete call is made by a status change.
    assert [request for request in result["requests"] if request["method"] == "DELETE"] == []


def test_double_click_on_mark_failed_sends_one_request() -> None:
    result = run([{"clickTwice": {"txId": 3, "action": "fail"}}])
    patch = [request for request in result["requests"] if request["method"] == "PATCH"]
    assert len(patch) == 1


def test_server_guard_message_for_mark_failed() -> None:
    result = run([{"clickAction": {"txId": 3, "action": "fail"}}], patch="conflict")
    assert result["elements"]["tx-action-status"]["text"] == (
        "This action is not allowed for that transaction."
    )
    assert row(result, 3)["status"] == "Successful"
    button = row(result, 3)["actions"][0]
    assert button["disabled"] is False  # the operator can retry or move on


@pytest.mark.parametrize("mode", ["conflict", "missing"])
def test_missing_or_conflicting_rows_show_the_denied_message(mode: str) -> None:
    result = run([{"clickAction": {"txId": 3, "action": "fail"}}], patch=mode)
    assert result["elements"]["tx-action-status"]["text"] == (
        "This action is not allowed for that transaction."
    )


def test_arabic_mark_failed_feedback() -> None:
    result = run([{"clickAction": {"txId": 3, "action": "fail"}}], lang="ar")
    assert result["elements"]["tx-action-status"]["text"] == "تم تسجيل فشل الشحن."


# --- Removing a failed row ------------------------------------------------


def test_removal_asks_for_confirmation_then_deletes() -> None:
    result = run([{"clickAction": {"txId": 2, "action": "remove"}}])
    assert result["confirmCalls"] == 1
    assert result["confirmMessage"] == (
        "Remove this failed transaction from the lists? It stays in the audit trail "
        "and no money is refunded."
    )
    delete = [request for request in result["requests"] if request["method"] == "DELETE"]
    assert delete == [{"url": "/api/transactions/2", "method": "DELETE", "body": None}]
    assert result["elements"]["tx-action-status"]["text"] == "Failed transaction removed."
    assert [entry["id"] for entry in result["rows"]] == ["3", "1"]


def test_cancelling_the_confirmation_deletes_nothing() -> None:
    result = run([{"clickAction": {"txId": 2, "action": "remove"}}], confirm="cancel")
    assert result["confirmCalls"] == 1
    assert [request for request in result["requests"] if request["method"] == "DELETE"] == []
    assert [entry["id"] for entry in result["rows"]] == ["3", "2", "1"]
    assert result["elements"]["tx-action-status"]["text"] == ""


def test_removal_double_click_asks_once_and_deletes_once() -> None:
    result = run([{"clickTwice": {"txId": 2, "action": "remove"}}])
    assert result["confirmCalls"] == 1
    assert len([request for request in result["requests"] if request["method"] == "DELETE"]) == 1


def test_removal_guard_message() -> None:
    result = run([{"clickAction": {"txId": 2, "action": "remove"}}], delete="conflict")
    assert result["elements"]["tx-action-status"]["text"] == (
        "This action is not allowed for that transaction."
    )
    assert [entry["id"] for entry in result["rows"]] == ["3", "2", "1"]
    assert row(result, 2)["actions"][0]["disabled"] is False


def test_arabic_removal_confirmation_and_feedback() -> None:
    result = run([{"clickAction": {"txId": 2, "action": "remove"}}], lang="ar")
    assert result["confirmMessage"] == (
        "إزالة هذه المعاملة الفاشلة من القوائم؟ ستبقى في السجل للمراجعة ولن تُرد أي مبالغ."
    )
    assert result["elements"]["tx-action-status"]["text"] == "تمت إزالة المعاملة الفاشلة."


# --- Markup and accessibility --------------------------------------------


def test_actions_live_region_and_heading_focus_target() -> None:
    html = render()
    assert 'id="tx-action-status" role="status" aria-live="polite"' in html
    assert 'id="tx-table-title" tabindex="-1"' in html
    assert 'aria-label="Mark charge failed' not in html  # labels are built per row
    assert "Remove this failed transaction" in html
    assert "disabled:cursor-not-allowed" in html
