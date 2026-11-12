"""Slice 1: the mobile POS header and the operator prepaid voucher pickers.

Item 1 hides the explanatory paragraph under the /pos title on phones only.
Item 2 replaces the generic quick-amount buttons with the selected operator's
prepaid voucher list, keeps the custom amount for both services, and proves the
voucher values reach the summary and the transaction payload as exact cents.
"""

import itertools
import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from backend.test_pos import client  # isolated, freshly seeded tmp_path database

ENV = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
HARNESS = Path(__file__).with_name("pos_voucher_harness.cjs")

MTN_VOUCHERS = [
    '10', '12', '15', '20', '25', '30', '35', '40',
    '50', '60', '70', '85', '90', '100', '110', '150',
    '170', '190', '200', '230', '260', '280', '300', '320',
    '340', '360', '400', '420', '440', '460', '480', '500',
    '600', '650', '700', '750', '1000', '1500', '2000', '2500',
    '3000', '3500', '4500', '5000', '5500', '6000',
]
SYRIATEL_VOUCHERS = [
    '1.92', '2.88', '3.84', '4.80', '5.76', '9.61', '20.19', '23.07',
    '24.03', '25.96', '30.76', '40.38', '45.19', '48.07', '52.88', '62.50',
    '68.26', '72.11', '77.88', '81.73', '86.53', '96.15', '100.96', '105.76',
    '115.38', '130.76', '144.23', '160.57', '163.46', '173.07', '183.65', '192.30',
    '211.53', '240.38', '288.46', '317.30', '370.30', '432.69', '480.76', '576.92',
    '721.15', '769.23', '951.92', '1057.69', '1923.07', '2115.38', '2403.84', '3846.15',
]
VOUCHERS = {"mtn": MTN_VOUCHERS, "syriatel": SYRIATEL_VOUCHERS}

GROUP_PATTERN = re.compile(
    r'<div data-voucher-operator="([^"]+)"([^>]*)>([\s\S]*?)</div>\s*</div>'
)
AMOUNT_PATTERN = re.compile(r'data-amount="([^"]*)"')


def render(lang: str = "en") -> str:
    return ENV.get_template("pos.html").render(lang=lang)


def voucher_groups(html: str) -> dict[str, tuple[str, list[str]]]:
    """Operator code -> (group attributes, voucher values in render order)."""
    return {
        code: (attributes, AMOUNT_PATTERN.findall(body))
        for code, attributes, body in GROUP_PATTERN.findall(html)
    }


def page_script(html: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    return next(script for script in scripts if "charge-form" in script)


def run_pos(steps, lang: str = "en"):
    """Run the rendered /pos script in a DOM double and report the end state."""
    if not shutil.which("node"):
        pytest.skip("Node.js required to execute the rendered /pos handlers")
    html = render(lang)
    result = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"html": html, "script": page_script(html), "steps": steps}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def posted_amount_cents(result) -> int:
    return next(request for request in result["requests"] if request["url"] == "/api/transactions/preview")[
        "body"
    ]["amount_cents"]


def visible_vouchers(result, operator: str) -> list[str]:
    return [
        voucher["amount"]
        for voucher in result["vouchers"]
        if voucher["operator"] == operator and voucher["visible"]
    ]


# --- Item 1: mobile spacing ------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_pos_description_is_hidden_on_mobile_only(lang: str) -> None:
    html = render(lang)
    paragraph = re.search(r'<p class="mt-2 hidden[^"]*">([^<]*)</p>', html)
    assert paragraph is not None, "the title paragraph must keep its classes"
    assert "max-w-xl" in paragraph.group(0)
    # Hidden by default, shown from the md breakpoint (the form's two-column
    # desktop layout) so desktop copy is untouched.
    assert "md:block" in paragraph.group(0)
    if lang == "en":
        assert "Enter a customer phone" in paragraph.group(1)
    else:
        assert "أدخل رقم العميل" in paragraph.group(1)


# --- Item 2: exact voucher values -----------------------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_voucher_values_are_exact_per_operator(lang: str) -> None:
    groups = voucher_groups(render(lang))
    assert set(groups) == {"mtn", "syriatel"}
    assert groups["mtn"][1] == MTN_VOUCHERS
    assert groups["syriatel"][1] == SYRIATEL_VOUCHERS


def test_syriatel_decimals_keep_their_exact_strings() -> None:
    html = render()
    assert 'data-amount="62.50"' in html
    assert 'data-amount="4.80"' in html
    # No value is re-rendered through a float (62.50 -> 62.5).
    group = voucher_groups(html)["syriatel"][1]
    assert "62.5" not in group
    # Every voucher is a whole number of cents once read as a decimal string.
    assert Decimal("62.50") * 100 == 6250
    assert Decimal("4.80") * 100 == 480
    for value in SYRIATEL_VOUCHERS:
        cents = Decimal(value) * 100
        assert cents == cents.to_integral_value(), value
        assert len(value.partition(".")[2]) == 2, value
    # Voucher selection reuses the exact-cent parser instead of float math.
    assert "parseAmount(button.dataset.amount)" in html
    assert "BigInt(button.dataset.amount)" not in html
    assert "amount.value = button.dataset.amount;" in html


def test_quick_amount_buttons_are_gone() -> None:
    for lang in ("en", "ar"):
        html = render(lang)
        assert "Quick amount" not in html
        assert "مبلغ سريع" not in html
        assert "amount-button" not in html
        assert "50" in voucher_groups(html)["mtn"][1]  # MTN still offers 50


# --- Item 2: markup, visibility and accessibility -------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_only_the_first_operator_voucher_group_starts_visible(lang: str) -> None:
    groups = voucher_groups(render(lang))
    assert "hidden" not in groups["mtn"][0]
    assert "hidden" in groups["syriatel"][0]


def test_voucher_groups_are_labelled_and_buttons_are_toggle_buttons() -> None:
    english = render("en")
    arabic = render("ar")
    for html in (english, arabic):
        assert 'id="voucher-fieldset"' in html
        assert html.count('role="group"') == 2
        assert html.count('class="voucher-button') == len(MTN_VOUCHERS) + len(SYRIATEL_VOUCHERS)
        assert html.count('aria-pressed="false"') == len(MTN_VOUCHERS) + len(SYRIATEL_VOUCHERS)
        buttons = re.findall(r'<button type="button" data-amount="[^"]*" aria-pressed="[^"]*"', html)
        assert len(buttons) == len(MTN_VOUCHERS) + len(SYRIATEL_VOUCHERS)
    assert 'aria-label="MTN vouchers"' in english
    assert 'aria-label="Syriatel vouchers"' in english
    assert 'aria-label="قسائم إم تي إن"' in arabic
    assert 'aria-label="قسائم سيرياتل"' in arabic
    assert "Voucher amount" in english and "Pick a voucher" in english
    assert "قسيمة الشحن" in arabic and "اختر قسيمة" in arabic


def test_custom_amount_stays_available_outside_the_voucher_fieldset() -> None:
    for lang in ("en", "ar"):
        html = render(lang)
        fieldset_start = html.index('id="voucher-fieldset"')
        fieldset_end = html.index("</fieldset>", fieldset_start)
        amount_start = html.index('id="amount"')
        # The custom amount input is independent of the voucher fieldset, so
        # hiding vouchers for postpaid never hides or disables it.
        assert amount_start > fieldset_end
        amount_input = html[amount_start : html.index(">", amount_start)]
        assert "disabled" not in amount_input
        assert 'value="50"' in amount_input
        label = html[html.rindex("<label", 0, amount_start) : amount_start]
        assert ("Custom amount" in label) if lang == "en" else ("مبلغ مخصص" in label)


def test_postpaid_hides_vouchers_only_through_the_service_toggle() -> None:
    html = render()
    # Visibility is driven by the service select and the operator select.
    assert "function syncVoucherVisibility()" in html
    assert "const prepaid = service.value === 'prepaid';" in html
    assert "voucherFieldset.hidden = !prepaid;" in html
    assert "service.addEventListener('change'" in html
    assert "operator.addEventListener('change'" in html
    assert 'value="postpaid"' in html


# --- Item 2: rendered behaviour -------------------------------------------


def test_operator_switch_updates_the_visible_voucher_list() -> None:
    result = run_pos([])
    assert result["voucherFieldsetHidden"] is False
    assert visible_vouchers(result, "mtn") == MTN_VOUCHERS
    assert visible_vouchers(result, "syriatel") == []

    switched = run_pos([{"values": {"operator": "syriatel"}, "id": "operator", "event": "change"}])
    assert switched["voucherFieldsetHidden"] is False
    assert visible_vouchers(switched, "syriatel") == SYRIATEL_VOUCHERS
    assert visible_vouchers(switched, "mtn") == []
    # Switching operator never rewrites the custom amount.
    assert switched["elements"]["amount"]["value"] == "50"


def test_switching_to_postpaid_neither_clears_nor_disables_the_custom_amount() -> None:
    result = run_pos([{"values": {"service": "postpaid"}, "id": "service", "event": "change"}])
    assert result["voucherFieldsetHidden"] is True
    assert result["elements"]["amount"]["value"] == "50"
    assert result["elements"]["summary-amount"]["text"] == "50"


def test_typing_an_operator_prefix_switches_the_visible_vouchers() -> None:
    result = run_pos([{"values": {"phone": "0933123456"}, "id": "phone", "event": "input"}])
    assert result["elements"]["operator"]["value"] == "syriatel"
    assert visible_vouchers(result, "syriatel") == SYRIATEL_VOUCHERS
    assert visible_vouchers(result, "mtn") == []


def test_voucher_selection_fills_custom_amount_summary_and_payload() -> None:
    result = run_pos(
        [
            {"values": {"operator": "syriatel"}, "id": "operator", "event": "change"},
            {"voucher": {"operator": "syriatel", "amount": "4.80"}},
            {"values": {"phone": "0933123456"}, "id": "charge-form", "event": "submit"},
        ]
    )
    assert result["elements"]["amount"]["value"] == "4.80"
    assert result["elements"]["amount"]["invalid"] == ""
    assert result["elements"]["summary-amount"]["text"] == "4.8"
    assert result["elements"]["summary-net"]["text"] == "5.04"
    pressed = [voucher for voucher in result["vouchers"] if voucher["pressed"] == "true"]
    assert [(voucher["operator"], voucher["amount"]) for voucher in pressed] == [
        ("syriatel", "4.80")
    ]
    assert "bg-blue-950" in pressed[0]["classes"]
    assert "bg-white" not in pressed[0]["classes"]
    payload = next(r for r in result["requests"] if r["url"] == "/api/transactions/preview")["body"]
    assert payload == {
        "customer_phone": "0933123456",
        "operator_code": "syriatel",
        "service_code": "prepaid",
        "amount_cents": 480,
    }


def test_syriatel_62_50_keeps_the_trailing_zero_and_exact_cents() -> None:
    result = run_pos(
        [
            {"values": {"operator": "syriatel"}, "id": "operator", "event": "change"},
            {"voucher": {"operator": "syriatel", "amount": "62.50"}},
            {"values": {"phone": "0933123456"}, "id": "charge-form", "event": "submit"},
        ]
    )
    # The input keeps the voucher string verbatim; the summary uses the shared
    # SYP formatter, which drops only the meaningless trailing zero.
    assert result["elements"]["amount"]["value"] == "62.50"
    assert result["elements"]["summary-amount"]["text"] == "62.5"
    assert posted_amount_cents(result) == 6250


def test_mtn_vouchers_post_exact_cents() -> None:
    result = run_pos(
        [
            {"voucher": {"operator": "mtn", "amount": "3000"}},
            {"values": {"phone": "0944123456"}, "id": "charge-form", "event": "submit"},
        ]
    )
    assert result["elements"]["amount"]["value"] == "3000"
    assert result["elements"]["summary-amount"]["text"] == "3,000"
    assert posted_amount_cents(result) == 300_000


def test_postpaid_hides_vouchers_and_keeps_the_custom_amount_editable() -> None:
    result = run_pos(
        [
            {"values": {"service": "postpaid"}, "id": "service", "event": "change"},
            {"values": {"phone": "0933123456", "operator": "syriatel"}, "id": "operator", "event": "change"},
            {"values": {"amount": "75"}, "id": "amount", "event": "input"},
            {"values": {"phone": "0933123456"}, "id": "charge-form", "event": "submit"},
        ]
    )
    assert result["voucherFieldsetHidden"] is True
    assert [group["hidden"] for group in result["groups"]] == [True, True]
    assert visible_vouchers(result, "mtn") == []
    assert visible_vouchers(result, "syriatel") == []
    # The custom amount is neither cleared nor disabled.
    assert result["elements"]["amount"]["value"] == "75"
    assert result["elements"]["summary-amount"]["text"] == "75"
    payload = next(r for r in result["requests"] if r["url"] == "/api/transactions/preview")["body"]
    assert payload == {
        "customer_phone": "0933123456",
        "operator_code": "syriatel",
        "service_code": "postpaid",
        "amount_cents": 7500,
    }


def test_switching_service_back_to_prepaid_restores_the_operator_vouchers() -> None:
    result = run_pos(
        [
            {"values": {"service": "postpaid"}, "id": "service", "event": "change"},
            {"values": {"service": "prepaid"}, "id": "service", "event": "change"},
        ]
    )
    assert result["voucherFieldsetHidden"] is False
    assert visible_vouchers(result, "mtn") == MTN_VOUCHERS
    assert visible_vouchers(result, "syriatel") == []


# --- Item 2: the API keeps accepting the voucher cents ---------------------


@pytest.mark.parametrize(
    ("operator_code", "phone", "amount_cents", "ussd_suffix"),
    [
        ("syriatel", "0933123456", 480, "*480*0933123456*0933123456#"),
        ("syriatel", "0933123456", 6250, "*6250*0933123456*0933123456#"),
        ("mtn", "0944123456", 300_000, "*3000#"),
        ("mtn", "0944123456", 5000, "*50#"),
    ],
)
def test_voucher_cents_reach_the_transaction_api(
    client, operator_code: str, phone: str, amount_cents: int, ussd_suffix: str
) -> None:
    assert (
        client.post(
            "/api/operators/topup", json={"operator_code": operator_code, "amount_cents": 5_000_000}
        ).status_code
        == 200
    )
    response = client.post(
        "/api/transactions",
        json={
            "customer_phone": phone,
            "operator_code": operator_code,
            "service_code": "prepaid",
            "amount_cents": amount_cents,
            "commit_key": new_commit_key(),
        },
    )
    assert response.status_code == 200
    assert response.json()["amount_cents"] == amount_cents
    assert response.json()["ussd_string"].endswith(ussd_suffix)


def test_postpaid_custom_amount_still_posts(client) -> None:
    assert (
        client.post(
            "/api/operators/topup", json={"operator_code": "syriatel", "amount_cents": 5_000_000}
        ).status_code
        == 200
    )
    response = client.post(
        "/api/transactions",
        json={
            "customer_phone": "0933123456",
            "operator_code": "syriatel",
            "service_code": "postpaid",
            "amount_cents": 12_345,
            "commit_key": new_commit_key(),
        },
    )
    assert response.status_code == 200
    assert response.json()["amount_cents"] == 12_345
    assert response.json()["ussd_string"] == "*150*1*67858*5*12345*0933123456*0933123456#"


_COMMIT_KEYS = itertools.count(1)


def new_commit_key() -> str:
    """A fresh commit key: every call is a deliberate, distinct sale."""
    return f"voucherkey{next(_COMMIT_KEYS):021d}"
