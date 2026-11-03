from __future__ import annotations

import importlib.util
import re
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "build_preview", Path(__file__).resolve().parents[1] / "scripts/build_preview.py"
)
build_preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_preview)


def test_public_site_has_no_profile_page_or_form():
    out = build_preview.build()
    assert not (out / "advisor.html").exists()
    assert not (out / "static/app.js").exists()
    for page in out.glob("*.html"):
        text = page.read_text()
        if page.name != "signin.html":
            assert "<form" not in text
            assert "<input" not in text
        assert "<textarea" not in text
        assert "/advisor" not in text
        assert "My profile" not in text
        assert "/api/" not in text


def test_sign_in_form_collects_only_account_details():
    text = (build_preview.build() / "signin.html").read_text()
    assert set(re.findall(r'<input[^>]* type="(\w+)"', text)) == {"email", "password", "text", "checkbox"}
    assert 'type="file"' not in text
    assert 'href="/terms.html"' in text


def test_start_my_list_goes_to_sign_in():
    home = (build_preview.build() / "index.html").read_text()
    assert "<script" not in home
    assert home.count('href="/signin.html"') >= 10
    assert "Start my list" in home


def test_sign_in_pages_load_only_the_auth_script():
    out = build_preview.build()
    for name, kind in (("signin.html", "signin"), ("account.html", "account")):
        text = (out / name).read_text()
        assert '<script src="/static/auth.js" defer></script>' in text
        assert text.count("<script") == 1
        assert f'data-auth-page="{kind}"' in text
    assert (out / "static/auth.js").is_file()


def test_contact_is_rendered_with_no_placeholders():
    out = build_preview.build("hello@example.com")
    for name in ("index.html", "signin.html", "account.html", "privacy.html", "terms.html"):
        page = (out / name).read_text()
        assert "{{" not in page
        assert 'href="/privacy.html"' in page
        assert "mailto:hello@example.com" in page
    assert "Page not found" in (out / "404.html").read_text()
