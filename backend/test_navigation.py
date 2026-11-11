"""Rendered shared-navigation contracts (layout/focus behavior needs a browser)."""
from collections import Counter
from html.parser import HTMLParser

import pytest

from backend.test_pos import client  # noqa: F401 -- disposable seeded DB fixture


ROUTES = ("/pos", "/topup", "/transactions", "/customers", "/settlement", "/stats", "/settings")
LABELS = {
    "en": ("POS", "Top-up", "Transactions", "Customers", "Settlement", "Stats", "Settings"),
    "ar": ("نقطة البيع", "تعبئة الرصيد", "المعاملات", "العملاء", "تسوية الديون", "الإحصائيات", "الإعدادات"),
}


class Element:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.text = ""

    def within(self, ancestor):
        parent = self.parent
        while parent is not None:
            if parent is ancestor:
                return True
            parent = parent.parent
        return False


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self.stack[-1] if self.stack else None)
        self.elements.append(element)
        if tag not in self.VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element.text += data

    def by_id(self, element_id):
        matches = [e for e in self.elements if e.attrs.get("id") == element_id]
        assert len(matches) == 1, (element_id, len(matches))
        return matches[0]

    def drawer_links(self):
        drawer = self.by_id("app-drawer")
        return [e for e in self.elements if e.tag == "a" and e.within(drawer)]


def render(client, route, lang):
    response = client.get(route, params={"lang": lang})
    assert response.status_code == 200
    return Document(response.text)


@pytest.mark.parametrize("lang", ("en", "ar"))
@pytest.mark.parametrize("route", ROUTES)
def test_single_shared_navigation_order_labels_and_active_page(client, route, lang):
    doc = render(client, route, lang)
    links = doc.drawer_links()
    page_links = [e for e in links if e.attrs.get("href") in ROUTES]
    assert [e.attrs["href"] for e in page_links] == list(ROUTES)
    assert [" ".join(e.text.split()) for e in page_links] == list(LABELS[lang])
    assert [e.attrs["href"] for e in page_links if e.attrs.get("aria-current") == "page"] == [route]
    assert all("aria-current" not in e.attrs for e in page_links if e.attrs["href"] != route)

    # Page controls can link to these routes too; only navigation landmarks must
    # avoid duplicated desktop/mobile copies of the primary navigation.
    nav_links = [e for e in doc.elements if e.tag == "a" and e.attrs.get("href") in ROUTES
                 and any(e.within(n) for n in doc.elements
                         if n.tag == "nav" or n.attrs.get("role") == "navigation")]
    assert Counter(e.attrs["href"] for e in nav_links) == Counter(ROUTES)
    language_links = [e for e in links if e.attrs.get("href", "").startswith("?lang=")]
    assert [e.attrs["href"] for e in language_links] == ["?lang=en", "?lang=ar"]
    assert [e.attrs.get("lang") for e in language_links] == ["en", "ar"]
    assert [" ".join(e.text.split()) for e in language_links] == ["English", "العربية"]
    assert min(links.index(e) for e in language_links) > max(links.index(e) for e in page_links)
    assert [e.attrs["lang"] for e in language_links if e.attrs.get("aria-current") == "true"] == [lang]


@pytest.mark.parametrize("lang", ("en", "ar"))
def test_shell_landmarks_controls_search_and_install_assets(client, lang):
    doc = render(client, "/settings", lang)
    html = next(e for e in doc.elements if e.tag == "html")
    assert html.attrs["lang"] == lang
    assert html.attrs["dir"] == ("rtl" if lang == "ar" else "ltr")
    ids = [e.attrs["id"] for e in doc.elements if "id" in e.attrs]
    assert len(ids) == len(set(ids)), "Shell must not introduce duplicate DOM IDs"
    drawer = doc.by_id("app-drawer")
    assert drawer.tag == "nav" or drawer.attrs.get("role") == "navigation"
    assert drawer.attrs.get("aria-label") or drawer.attrs.get("aria-labelledby")
    assert "aria-modal" not in drawer.attrs, "A persistent navigation landmark is not a modal dialog"
    toggle = doc.by_id("nav-toggle")
    assert toggle.tag == "button" and toggle.attrs.get("type") == "button"
    assert toggle.attrs.get("aria-controls") == "app-drawer"
    assert toggle.attrs.get("aria-expanded") == "false"
    assert toggle.attrs.get("aria-label")
    close = doc.by_id("app-drawer-close")
    assert close.tag == "button" and close.attrs.get("type") == "button"
    assert close.attrs.get("aria-label") and close.within(drawer)
    doc.by_id("app-drawer-backdrop")
    mains = [e for e in doc.elements if e.tag == "main"]
    assert len(mains) == 1 and not mains[0].within(drawer)

    # Search retains real combobox/listbox relationships after relocation.
    searches = [e for e in doc.elements if e.attrs.get("role") == "combobox"
                and e.attrs.get("type") == "search"]
    assert searches
    for search in searches:
        assert search.attrs.get("aria-label")
        results = doc.by_id(search.attrs["aria-controls"])
        assert results.attrs.get("role") == "listbox"
    doc.by_id("header-timezone")

    scripts = [e.attrs.get("src") for e in doc.elements if e.tag == "script"]
    styles = [e.attrs.get("href") for e in doc.elements if e.tag == "link" and e.attrs.get("rel") == "stylesheet"]
    assert scripts.count("/static/install.js") == 1
    assert scripts.count("/static/navigation.js") == 1
    assert styles.count("/static/navigation.css") == 1
    for path in ("/static/install.js", "/static/navigation.js", "/static/navigation.css"):
        assert client.get(path).status_code == 200
    assert any(e.tag == "link" and e.attrs.get("rel") == "manifest"
               and e.attrs.get("href") == "/manifest.webmanifest" for e in doc.elements)
    install = doc.by_id("install-button")
    assert install.tag == "button" and install.attrs.get("type") == "button"
    assert "hidden" in install.attrs and not install.within(drawer)
    doc.by_id("install-help")
    doc.by_id("install-status")


def test_root_redirect_and_invalid_language_keep_pos_current(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/pos"
    doc = render(client, "/pos", "invalid")
    html = next(e for e in doc.elements if e.tag == "html")
    assert html.attrs["lang"] == "en" and html.attrs["dir"] == "ltr"
    assert [e.attrs["href"] for e in doc.drawer_links() if e.attrs.get("aria-current") == "page"] == ["/pos"]


@pytest.mark.parametrize("lang", ("en", "ar"))
def test_operator_subpage_retains_shared_navigation_without_false_current(client, lang):
    doc = render(client, "/operators/mtn", lang)
    links = [e for e in doc.drawer_links() if e.attrs.get("href") in ROUTES]
    assert [e.attrs["href"] for e in links] == list(ROUTES)
    assert not any(e.attrs.get("aria-current") == "page" for e in links)
    assert not any(e.attrs.get("id") == "install-button" for e in doc.elements)
