"""Slice 3: mobile-only POS compaction.

Everything asserted here is scoped below the POS template's existing ``md``
breakpoint: the page header disappears on phones, the paired fields share a
row, and the summary/result cards lose their explanatory copy. At ``md`` and
above the markup keeps its previous layout utilities, so the desktop/tablet
experience is unchanged.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from backend.test_pos_vouchers import MTN_VOUCHERS, SYRIATEL_VOUCHERS, voucher_groups

ENV = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
HARNESS = Path(__file__).with_name("pos_result_harness.cjs")

GENERATE = [{"values": {"phone": "0944123456"}, "id": "charge-form", "event": "submit"}]


def render(lang: str = "en") -> str:
    return ENV.get_template("pos.html").render(lang=lang)


def classes_of(match_group: str) -> list[str]:
    return match_group.split()


def row_block(html: str, first_for: str) -> tuple[list[str], str]:
    """Classes of the row wrapper around a field, plus the row's markup."""
    label_start = html.index(f'<label for="{first_for}"')
    wrapper_start = html.rindex('<div class="grid', 0, label_start)
    classes = re.search(r'<div class="([^"]*)"', html[wrapper_start:label_start]).group(1)
    # The paired field's own wrapper is an unclassed <div>, so the next
    # "class=grid" wrapper after the label is the following row.
    block_end = html.index('<div class="grid', label_start)
    return classes_of(classes), html[wrapper_start:block_end]


def run_pos(steps, lang: str = "en", **options):
    """Run the rendered /pos script in the DOM double used by Slice 2."""
    if not shutil.which("node"):
        pytest.skip("Node.js required to execute the rendered /pos handlers")
    html = render(lang)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    script = next(item for item in scripts if "charge-form" in item)
    payload = {"html": html, "script": script, "steps": steps}
    payload.update(options)
    result = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


# --- 1. Page header --------------------------------------------------------


@pytest.mark.parametrize(("lang", "title", "badge"), [
    ("en", "POS Quick Charge", "Live transaction"),
    ("ar", "الشحن السريع", "معاملة مباشرة"),
])
def test_page_header_is_mobile_hidden(lang: str, title: str, badge: str) -> None:
    html = render(lang)
    header = re.search(r'<div class="(mb-8 [^"]*)"', html)
    assert header is not None
    classes = classes_of(header.group(1))
    assert "hidden" in classes
    assert "md:flex" in classes
    # A plain "flex" would override the mobile hide.
    assert "flex" not in classes
    # The desktop header markup is untouched for md and up.
    assert {"flex-wrap", "items-end", "justify-between", "gap-4"} <= set(classes)
    assert title in html
    assert badge in html
    # A screen-reader-only copy of the title keeps the outline on phones while
    # the visible header block is what disappears.
    mobile_heading = re.search(r'<h1 class="sr-only md:hidden">([^<]*)</h1>', html)
    assert mobile_heading is not None
    assert mobile_heading.group(1) == title


def test_charge_details_card_is_the_first_content_on_mobile() -> None:
    html = render()
    body_start = html.index('<section class="mx-auto max-w-5xl">')
    form_start = html.index('<form id="charge-form"')
    before_form = html[body_start:form_start]
    # The only block rendered before the form is the mobile-hidden header.
    assert before_form.count('<div class="') == 1
    assert "hidden flex-wrap" in before_form
    assert "POS Quick Charge" in before_form
    # Inside the form, the Charge details card comes first.
    assert 'aria-labelledby="charge-details"' in html[form_start:]
    assert html.index('aria-labelledby="charge-details"') < html.index('aria-labelledby="summary-title"')


# --- 2 + 3. Paired rows ---------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_phone_and_name_share_one_row_on_mobile(lang: str) -> None:
    classes, block = row_block(render(lang), "phone")
    assert "grid-cols-2" in classes
    # No "sm:grid-cols-2": two columns are no longer deferred to a breakpoint,
    # so the pair also shares a row on phones.
    assert "sm:grid-cols-2" not in classes
    assert 'for="customer-name"' in block
    assert 'id="phone"' in block and 'id="customer-name"' in block


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_operator_and_service_share_one_row_on_mobile(lang: str) -> None:
    classes, block = row_block(render(lang), "operator")
    assert "grid-cols-2" in classes
    assert "sm:grid-cols-2" not in classes
    assert 'for="service"' in block
    assert 'id="operator"' in block and 'id="service"' in block


def test_fields_keep_their_labels_help_text_and_targets() -> None:
    html = render()
    assert 'for="phone"' in html and 'aria-describedby="phone-help"' in html
    assert 'for="customer-name"' in html
    assert 'for="operator"' in html and 'for="service"' in html
    assert 'id="phone-help"' in html


# --- 5. Amount summary ----------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_amount_summary_drops_its_subtitle_and_refresh_button_on_mobile(lang: str) -> None:
    html = render(lang)
    # Scope the search to the Amount summary card.
    summary_html = html[html.index('id="summary-title"') : html.index('id="result"')]
    subtitle = re.search(r'<p class="(mt-1 [^"]*)">([^<]*)</p>', summary_html)
    assert subtitle is not None
    subtitle_classes = classes_of(subtitle.group(1))
    assert {"hidden", "md:block", "text-sm", "text-slate-500"} <= set(subtitle_classes)
    assert subtitle.group(2) in {"Amount and estimated commission", "المبلغ والعمولة المقدّرة"}

    refresh = re.search(r'<button id="refresh-rate" type="button" class="([^"]*)"', html)
    assert refresh is not None
    refresh_classes = classes_of(refresh.group(1))
    assert {"hidden", "md:block", "text-xs", "font-semibold"} <= set(refresh_classes)
    assert 'id="refresh-rate"' in html  # kept wired for desktop


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_amount_summary_values_are_unchanged(lang: str) -> None:
    html = render(lang)
    for element_id in ("summary-amount", "summary-rate", "summary-commission", "summary-net"):
        assert f'id="{element_id}"' in html
    assert 'id="summary-title"' in html
    # No explanatory subtext remains inside the definition list beneath the
    # Customer total row.
    assert re.search(r'id="summary-net"[^>]*>—</dd></div>\s*</dl>', html)
    assert ("Customer total" in html) if lang == "en" else ("الإجمالي على العميل" in html)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_rate_line_is_mobile_hidden_but_stays_a_live_region(lang: str) -> None:
    html = render(lang)
    status = re.search(r'<p id="rate-status"[^>]*class="([^"]*)"[^>]*>', html)
    assert status is not None
    assert {"hidden", "md:block"} <= set(classes_of(status.group(1)))
    tag = re.search(r'<p id="rate-status"[^>]*>', html).group(0)
    assert 'role="status"' in tag and 'aria-live="polite"' in tag


# --- 6. Result card -------------------------------------------------------


@pytest.mark.parametrize(("lang", "text"), [
    ("en", "Nothing is recorded yet and no balance has moved."),
    ("ar", "لم تُسجَّل المعاملة بعد ولم يُخصم أي مبلغ."),
])
def test_result_card_explanatory_subtext_is_mobile_hidden(lang: str, text: str) -> None:
    html = render(lang)
    paragraph = re.search(
        r'<p [^>]*class="(mt-1 [^"]*)"[^>]*>([^<]*' + re.escape(text) + r'[^<]*)</p>', html
    )
    assert paragraph is not None
    assert {"hidden", "md:block"} <= set(classes_of(paragraph.group(1)))
    # The code, copy action, commit state and dialer controls are untouched.
    for element_id in (
        "ussd-output",
        "copy-ussd",
        "copy-status",
        "result-badge",
        "commit-status",
        "dial-actions",
    ):
        assert f'id="{element_id}"' in html


# --- Desktop contract -----------------------------------------------------


def test_desktop_layout_contract_is_preserved() -> None:
    html = render()
    # The two-column form and the header layout utilities are unchanged.
    assert 'class="grid gap-6 md:grid-cols-2"' in html
    header = re.search(r'<div class="(mb-8 [^"]*)"', html).group(1)
    assert "md:flex" in header
    # Slice 1's desktop-only description paragraph is untouched.
    assert re.search(r'<p class="mt-2 hidden max-w-xl text-sm leading-6 text-slate-600 md:block">', html)
    badge = re.search(r'<span class="(rounded-full border border-slate-200[^"]*)"', html).group(1)
    assert "text-xs" in badge and "font-semibold" in badge


def test_voucher_picker_and_custom_amount_are_untouched() -> None:
    html = render()
    groups = voucher_groups(html)
    assert groups["mtn"][1] == MTN_VOUCHERS
    assert groups["syriatel"][1] == SYRIATEL_VOUCHERS
    assert html.count('class="voucher-button') == len(MTN_VOUCHERS) + len(SYRIATEL_VOUCHERS)
    fieldset_start = html.index('id="voucher-fieldset"')
    fieldset_end = html.index("</fieldset>", fieldset_start)
    amount_start = html.index('id="amount"')
    assert amount_start > fieldset_end
    amount_input = html[amount_start : html.index(">", amount_start)]
    assert 'value="50"' in amount_input and "disabled" not in amount_input


# --- Arabic / RTL ---------------------------------------------------------


def test_arabic_markup_keeps_rtl_and_labels() -> None:
    html = render("ar")
    assert '<html lang="ar" dir="rtl">' in html
    for label in ("رقم هاتف العميل", "اسم العميل", "المشغّل", "الخدمة", "ملخص المبلغ", "رمز USSD"):
        assert label in html
    # The compaction hides the same elements in Arabic as in English.
    assert render("en").count('class="hidden') == html.count('class="hidden')


# --- Behaviour stays intact ----------------------------------------------


def test_voucher_selection_and_transaction_flow_still_work() -> None:
    result = run_pos(
        [
            {"values": {"operator": "syriatel"}, "id": "operator", "event": "change"},
            {"voucher": {"operator": "syriatel", "amount": "62.50"}},
            {"values": {"phone": "0933123456"}, "id": "charge-form", "event": "submit"},
            {"id": "copy-ussd", "event": "click"},
        ]
    )
    assert result["elements"]["amount"]["value"] == "62.50"
    assert result["elements"]["summary-amount"]["text"] == "62.5"
    post = next(item for item in result["requests"] if item["url"] == "/api/transactions/preview")
    assert post["body"]["amount_cents"] == 6250
    commit = next(item for item in result["commits"])
    assert commit["body"]["amount_cents"] == 6250
    assert commit["body"]["commit_key"] == "previewkey0000000000000000001"
    assert result["badge"]["text"] == "Recorded"


def test_copy_action_still_reports_success() -> None:
    result = run_pos(GENERATE + [{"id": "copy-ussd", "event": "click"}], clipboard="modern")
    assert result["clipboardWrites"] == ["*150*65477*0944123456*50#"]
    assert result["elements"]["copy-status"]["text"] == "Code copied."


def test_rate_line_hidden_after_a_successful_load() -> None:
    result = run_pos([], lang="en")
    rate_line = result["elements"]["rate-status"]
    assert "hidden" in rate_line["classes"] and "md:block" in rate_line["classes"]
    assert rate_line["text"] == "Current commission rate. Refresh to get the latest rate."


@pytest.mark.parametrize(("lang", "message"), [
    ("en", "Could not load the commission rate. Try refreshing."),
    ("ar", "تعذر تحميل نسبة العمولة. حاول التحديث."),
])
def test_rate_failure_is_still_shown_on_mobile(lang: str, message: str) -> None:
    result = run_pos([], lang=lang, commission="error")
    rate_line = result["elements"]["rate-status"]
    assert "hidden" not in rate_line["classes"]
    assert "md:block" in rate_line["classes"]
    assert rate_line["text"] == message
