from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from backend.main import app


def test_pos_template_renders_with_base() -> None:
    environment = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
    template = environment.get_template("pos.html")
    english = template.render(lang="en")
    arabic = template.render(lang="ar")
    assert '<html lang="en" dir="ltr">' in english
    assert '<html lang="ar" dir="rtl">' in arabic
    assert "POS Quick Charge" in english
    assert "الشحن السريع" in arabic
    assert 'src="https://cdn.tailwindcss.com"' in english
    assert 'class="grid gap-6 md:grid-cols-2"' in english
    for amount in (10, 20, 50, 100, 200):
        assert f'data-amount="{amount}"' in english
    assert "navigator.clipboard.writeText" in english
    assert "fetch('/api/settings/commission'" in english


def test_pos_route_languages() -> None:
    with TestClient(app) as client:
        english = client.get("/pos")
        arabic = client.get("/pos", params={"lang": "ar"})
        fallback = client.get("/pos", params={"lang": "invalid"})
    assert english.status_code == arabic.status_code == fallback.status_code == 200
    assert "POS Quick Charge" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "الشحن السريع" in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text